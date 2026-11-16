"""Read queries behind the MCP tools. All SQL is built from fixed fragments; values go through parameters."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, LiteralString
from zoneinfo import ZoneInfo

from events_mcp.db import Pool
from events_mcp.embeddings import Embedder, vector_literal

Kind = Literal["event", "exhibition", "video", "sale", "other"]
KINDS: tuple[str, ...] = ("event", "exhibition", "video", "sale", "other")

# Reciprocal-rank-fusion constant; 60 is the usual choice and is insensitive in practice.
RRF_K = 60
CANDIDATES = 50
SUMMARY_CHARS = 300

_LIST_COLUMNS = """
    l.id, l.title, l.kind, l.starts_at, l.ends_at, l.city, l.url, l.status, l.lifecycle, l.is_stale,
    l.first_seen, l.last_seen, left(l.summary, %(summary_chars)s::int) AS summary,
    v.name AS venue, v.address AS venue_address, s.name AS source
"""
_LIST_FROM = """
    FROM event_lifecycle l
    JOIN sources s ON s.id = l.source_id
    LEFT JOIN venues v ON v.id = l.venue_id
"""


def parse_when(value: str | None, tz: ZoneInfo, *, end_of_day: bool = False) -> datetime | None:
    """Parse an ISO date or datetime. Bare dates are local days: start of day, or end of day for ``to`` bounds."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:  # noqa: PLR2004 - YYYY-MM-DD
            d = date.fromisoformat(text)
            t = time.max if end_of_day else time.min
            return datetime.combine(d, t, tzinfo=tz)
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an ISO date or datetime: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


@dataclass
class Filters:
    """Optional constraints shared by every listing tool."""

    city: str | None = None
    kind: str | None = None
    kinds: list[str] | None = None
    exclude_sales: bool = False
    """Leave out kind=sale unless a kind filter names it: "what is on" is about events, not discounts."""
    date_from: datetime | None = None
    date_to: datetime | None = None
    source: str | None = None
    new_since: datetime | None = None
    include_ended: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    def where(self) -> LiteralString:
        """Build the WHERE clause and fill ``self.params``."""
        clauses: list[LiteralString] = []
        p = self.params
        if self.kind:
            if self.kind not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            clauses.append("l.kind = %(kind)s")
            p["kind"] = self.kind
        elif self.kinds:
            if bad := [k for k in self.kinds if k not in KINDS]:
                raise ValueError(f"kinds must be among {', '.join(KINDS)}; got {', '.join(bad)}")
            clauses.append("l.kind = ANY(%(kinds)s)")
            p["kinds"] = self.kinds
        elif self.exclude_sales:
            clauses.append("l.kind <> 'sale'")
        if self.city:
            clauses.append("lower(l.city) = lower(%(city)s)")
            p["city"] = self.city.strip()
        if self.source:
            # A source or a venue name: "anything new from <channel/venue>".
            clauses.append("(s.name ILIKE %(source)s OR v.name ILIKE %(source)s)")
            p["source"] = f"%{self.source.strip()}%"
        if self.new_since:
            clauses.append("l.first_seen >= %(new_since)s")
            p["new_since"] = self.new_since
        if self.date_from or self.date_to:
            # Overlap with the item's calendar window; undated items never match a window.
            clauses.append("(l.window_start IS NOT NULL OR l.window_end IS NOT NULL)")
            if self.date_from:
                clauses.append("(l.window_end IS NULL OR l.window_end >= %(date_from)s)")
                p["date_from"] = self.date_from
            if self.date_to:
                clauses.append("(l.window_start IS NULL OR l.window_start <= %(date_to)s)")
                p["date_to"] = self.date_to
        elif not self.include_ended:
            clauses.append("l.lifecycle <> 'ended'")
        return " AND ".join(clauses) if clauses else "TRUE"


def _row(r: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in r.items():
        if v is None or k in {"embedding", "search"}:
            continue
        out[k] = v.astimezone(tz).isoformat() if isinstance(v, datetime) else v
    return out


async def _fetch(pool: Pool, sql: LiteralString, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return list(await cur.fetchall())


async def _keyword_ids(pool: Pool, where: LiteralString, params: dict[str, Any], query: str) -> list[int]:
    # Every term ORed so fuzzy questions still hit; listings matching ALL terms (websearch
    # semantics) rank first. Both language configs, so German and English stems both match.
    sql = f"""
        SELECT l.id
        {_LIST_FROM}
        CROSS JOIN LATERAL (
            SELECT websearch_to_tsquery('german', %(q)s) || websearch_to_tsquery('english', %(q)s) AS strict,
                   coalesce(string_agg(
                       '(' || plainto_tsquery('german', w)::text || ') | ('
                       || plainto_tsquery('english', w)::text || ')', ' | '
                   ), '')::tsquery AS loose
            -- A word is only ORed in if neither language treats it as a stopword; otherwise
            -- "a", "for" (not German stopwords) or "die", "und" (not English) match everything.
            FROM regexp_split_to_table(%(q)s, '[[:space:]]+') w
            WHERE numnode(plainto_tsquery('german', w)) > 0 AND numnode(plainto_tsquery('english', w)) > 0
        ) q
        WHERE {where} AND l.search @@ q.loose
        ORDER BY (l.search @@ q.strict) DESC, ts_rank_cd(l.search, q.loose) DESC, l.id
        LIMIT %(cand)s
    """
    return [r["id"] for r in await _fetch(pool, sql, {**params, "q": query})]


async def _semantic_hits(
    pool: Pool, where: LiteralString, params: dict[str, Any], vector: list[float], model: str, max_distance: float
) -> dict[int, float]:
    """Vector hits within ``max_distance``, nearest first (dicts keep insertion order)."""
    sql = f"""
        SELECT id, distance FROM (
            SELECT l.id, l.embedding <=> %(vec)s::vector AS distance
            {_LIST_FROM}
            WHERE {where} AND l.embedding IS NOT NULL AND l.embedding_model = %(model)s
        ) d
        WHERE distance <= %(max_distance)s
        ORDER BY distance, id
        LIMIT %(cand)s
    """
    params = {**params, "vec": vector_literal(vector), "model": model, "max_distance": max_distance}
    return {r["id"]: round(float(r["distance"]), 3) for r in await _fetch(pool, sql, params)}


async def search(
    pool: Pool,
    embedder: Embedder | None,
    query: str,
    filters: Filters,
    tz: ZoneInfo,
    limit: int = 20,
    max_distance: float = 0.75,
) -> dict[str, Any]:
    """Hybrid search: full-text always, pgvector when the embedding endpoint answers, fused by rank.

    Semantic hits farther than ``max_distance`` (cosine) are dropped: without a cutoff every row with
    an embedding is a "match". Each semantic hit reports its distance so the caller can judge.
    """
    where = filters.where()
    base = {**filters.params, "summary_chars": SUMMARY_CHARS, "cand": CANDIDATES}
    query = query.strip()
    if not query:
        sql = f"""
            SELECT {_LIST_COLUMNS} {_LIST_FROM} WHERE {where}
            ORDER BY l.window_start NULLS LAST, l.id LIMIT %(limit)s
        """
        rows = await _fetch(pool, sql, {**base, "limit": limit})
        results = [_row(r, tz) for r in rows]
        return {"semantic_search": "not_requested", "count": len(results), "results": results}

    keyword = await _keyword_ids(pool, where, base, query)
    semantic = "not_configured" if embedder is None else "unavailable"
    distances: dict[int, float] = {}
    if embedder is not None and (vectors := await embedder.embed_query(query)):
        semantic = "used"
        distances = await _semantic_hits(pool, where, base, vectors[0], embedder.model, max_distance)

    # Reciprocal rank fusion of the two candidate lists.
    scores: dict[int, float] = {}
    for ranked in (keyword, list(distances)):
        for rank, event_id in enumerate(ranked):
            scores[event_id] = scores.get(event_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    ordered = sorted(scores, key=lambda i: (-scores[i], i))[:limit]
    rows = []
    if ordered:
        sql = f"SELECT {_LIST_COLUMNS} {_LIST_FROM} WHERE l.id = ANY(%(ids)s)"
        by_id = {r["id"]: r for r in await _fetch(pool, sql, {**base, "ids": ordered})}
        rows = [by_id[i] for i in ordered if i in by_id]

    kw = set(keyword)
    results = []
    for r in rows:
        item = _row(r, tz)
        hits = (("keyword", r["id"] in kw), ("semantic", r["id"] in distances))
        item["matched_by"] = "+".join(name for name, hit in hits if hit)
        if r["id"] in distances:
            item["semantic_distance"] = distances[r["id"]]
        results.append(item)
    out: dict[str, Any] = {"semantic_search": semantic, "count": len(results), "results": results}
    if semantic == "unavailable":
        out["note"] = "The embedding endpoint did not answer; these are keyword matches only."
    return out


async def upcoming(pool: Pool, filters: Filters, days: int, tz: ZoneInfo, limit: int = 50) -> list[dict[str, Any]]:
    """Not-yet-ended, not-cancelled items whose window overlaps the next ``days`` days."""
    where = filters.where()
    sql = f"""
        SELECT {_LIST_COLUMNS} {_LIST_FROM}
        WHERE {where}
          AND l.lifecycle IN ('upcoming', 'ongoing')
          AND l.status IS DISTINCT FROM 'cancelled'
          AND (l.window_start IS NULL OR l.window_start <= now() + make_interval(days => %(days)s::int))
        ORDER BY l.window_start NULLS FIRST, l.id
        LIMIT %(limit)s
    """
    params = {**filters.params, "summary_chars": SUMMARY_CHARS, "days": days, "limit": limit}
    return [_row(r, tz) for r in await _fetch(pool, sql, params)]


async def ending_soon(pool: Pool, filters: Filters, days: int, tz: ZoneInfo, limit: int = 50) -> list[dict[str, Any]]:
    """Items with an explicit end date in the next ``days`` days, soonest first."""
    where = filters.where()
    sql = f"""
        SELECT {_LIST_COLUMNS} {_LIST_FROM}
        WHERE {where}
          AND l.kind <> 'video'
          AND l.ends_at >= now() AND l.ends_at <= now() + make_interval(days => %(days)s::int)
          AND l.status IS DISTINCT FROM 'cancelled'
        ORDER BY l.ends_at, l.id
        LIMIT %(limit)s
    """
    params = {**filters.params, "summary_chars": SUMMARY_CHARS, "days": days, "limit": limit}
    return [_row(r, tz) for r in await _fetch(pool, sql, params)]


async def get_event(pool: Pool, event_id: int, tz: ZoneInfo) -> dict[str, Any] | None:
    """One item with its full text, venue and source."""
    sql = """
        SELECT l.id, l.title, l.kind, l.starts_at, l.ends_at, l.city, l.url, l.status, l.lifecycle,
               l.is_stale, l.first_seen, l.last_seen, l.summary, l.raw_text, l.embedding_model,
               v.name AS venue, v.address AS venue_address, v.city AS venue_city, v.lat AS venue_lat,
               v.lon AS venue_lon, s.name AS source, s.type AS source_type, s.url AS source_url
        FROM event_lifecycle l
        JOIN sources s ON s.id = l.source_id
        LEFT JOIN venues v ON v.id = l.venue_id
        WHERE l.id = %(id)s
    """
    rows = await _fetch(pool, sql, {"id": event_id})
    return _row(rows[0], tz) if rows else None


async def list_sources(pool: Pool, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Every source with its item count and last successful scrape."""
    sql = """
        SELECT s.id, s.name, s.type, s.url, s.city, s.last_run,
               (SELECT count(*) FROM events e WHERE e.source_id = s.id) AS items,
               (SELECT max(r.finished_at) FROM scrape_runs r
                 WHERE r.source_id = s.id AND r.status = 'success') AS last_success
        FROM sources s ORDER BY s.name
    """
    return [_row(r, tz) for r in await _fetch(pool, sql, {})]


def window_for_days(days: int) -> timedelta:
    """Validate a ``days`` argument."""
    if days < 1 or days > 366:  # noqa: PLR2004
        raise ValueError("days must be between 1 and 366")
    return timedelta(days=days)
