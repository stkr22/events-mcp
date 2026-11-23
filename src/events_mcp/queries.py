"""Read queries behind the MCP tools. All SQL is built from fixed fragments; values go through parameters."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, LiteralString
from zoneinfo import ZoneInfo

from events_mcp.db import Pool
from events_mcp.embeddings import Embedder, vector_literal

Kind = Literal["event", "exhibition", "video", "sale", "other"]
KINDS: tuple[str, ...] = ("event", "exhibition", "video", "sale", "other")
RegistrationState = Literal["not_yet_open", "open", "closed"]
REGISTRATION_STATES: tuple[str, ...] = ("not_yet_open", "open", "closed")
TrackStatus = Literal["raised", "dismissed", "planned"]
TRACK_STATUSES: tuple[str, ...] = ("raised", "dismissed", "planned")
MAX_TASK_REF = 200

# Reciprocal-rank-fusion constant; 60 is the usual choice and is insensitive in practice.
RRF_K = 60
CANDIDATES = 50
SUMMARY_CHARS = 300

_LIST_COLUMNS = """
    l.id, l.title, l.kind, l.starts_at, l.ends_at, l.city, l.url, l.status, l.lifecycle, l.is_stale,
    l.registration_state, l.registration_opens_at, l.registration_closes_at, l.tickets_url,
    l.first_seen, l.last_seen, left(l.summary, %(summary_chars)s::int) AS summary,
    v.name AS venue, v.address AS venue_address, s.name AS source, u.status AS tracked_status
"""
# One user for now: the owner column exists so a second person needs no migration, but no tool takes it.
_LIST_FROM = """
    FROM event_lifecycle l
    JOIN sources s ON s.id = l.source_id
    LEFT JOIN venues v ON v.id = l.venue_id
    LEFT JOIN event_user_state u ON u.event_id = l.id AND u.owner = 'default'
"""
# Rows an upstream lists twice (same normalised title, start and venue, e.g. one with and one without its link)
# collapse to one: the row with a ticket link, then one with a URL, then the oldest id. The others stay in the
# database and show up as `also_listed_as`.
_DUP_PARTITION = (
    "PARTITION BY lower(btrim(regexp_replace(l.title, '[^[:alnum:]]+', ' ', 'g'))), l.starts_at, l.venue_id"
)
_DUP_HELPERS = ("dup_rank", "dup_ids", "dup_window_start")
COMPACT_FIELDS = (
    "id",
    "title",
    "starts_at",
    "venue",
    "url",
    "also_listed_as",
    # what ending_soon and registration_deadlines are about; absent (and so omitted) on other items
    "ends_at",
    "registration_opens_at",
    "registration_closes_at",
)
# Tracked events whose data differs from what the user was told.
_CHANGED = """(u.snapshot_starts_at IS DISTINCT FROM l.starts_at
    OR u.snapshot_registration_opens_at IS DISTINCT FROM l.registration_opens_at
    OR u.snapshot_registration_closes_at IS DISTINCT FROM l.registration_closes_at
    OR u.snapshot_event_status IS DISTINCT FROM l.status
    OR u.snapshot_title IS DISTINCT FROM l.title)"""
_CHANGE_FIELDS = (
    ("title", "snapshot_title"),
    ("starts_at", "snapshot_starts_at"),
    ("registration_opens_at", "snapshot_registration_opens_at"),
    ("registration_closes_at", "snapshot_registration_closes_at"),
    ("status", "snapshot_event_status"),
)


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
    registration: str | None = None
    """not_yet_open, open or closed; items that say nothing about registration never match."""
    hide_handled: bool = False
    """Leave out events the user dismissed or that are already planned."""
    date_from: datetime | None = None
    date_to: datetime | None = None
    source: str | None = None
    new_since: datetime | None = None
    include_ended: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    def _kind_clause(self) -> LiteralString | None:
        p = self.params
        if self.kind:
            if self.kind not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            p["kind"] = self.kind
            return "l.kind = %(kind)s"
        if self.kinds:
            if bad := [k for k in self.kinds if k not in KINDS]:
                raise ValueError(f"kinds must be among {', '.join(KINDS)}; got {', '.join(bad)}")
            p["kinds"] = self.kinds
            return "l.kind = ANY(%(kinds)s)"
        return "l.kind <> 'sale'" if self.exclude_sales else None

    def where(self) -> LiteralString:
        """Build the WHERE clause and fill ``self.params``."""
        clauses: list[LiteralString] = []
        p = self.params
        if kind_clause := self._kind_clause():
            clauses.append(kind_clause)
        if self.registration:
            if self.registration not in REGISTRATION_STATES:
                raise ValueError(f"registration must be one of {', '.join(REGISTRATION_STATES)}")
            clauses.append("l.registration_state = %(registration)s")
            p["registration"] = self.registration
        if self.hide_handled:
            clauses.append("(u.status IS NULL OR u.status = 'raised')")
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


def _collapsed(where: LiteralString, order: LiteralString, limit: LiteralString = "LIMIT %(limit)s") -> LiteralString:
    """Build the list query with duplicate rows collapsed before ``order`` and ``limit`` apply.

    ``order`` may only name output columns (``ends_at``, ``dup_window_start``, ...).
    """
    return f"""
        SELECT * FROM (
            SELECT {_LIST_COLUMNS},
                   row_number() OVER (dup ORDER BY (l.tickets_url IS NOT NULL) DESC, (l.url IS NOT NULL) DESC, l.id)
                       AS dup_rank,
                   array_agg(l.id) OVER dup AS dup_ids,
                   l.window_start AS dup_window_start
            {_LIST_FROM}
            WHERE {where}
            WINDOW dup AS ({_DUP_PARTITION})
        ) d
        WHERE dup_rank = 1
        ORDER BY {order}
        {limit}
    """


def _row(r: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in r.items():
        if v is None or k in {"embedding", "search", *_DUP_HELPERS}:
            continue
        out[k] = v.astimezone(tz).isoformat() if isinstance(v, datetime) else v
    if others := sorted(i for i in r.get("dup_ids") or [] if i != r["id"]):
        out["also_listed_as"] = others
    return out


def compact_row(item: dict[str, Any]) -> dict[str, Any]:
    """Reduce a result to the few fields an agent needs to name an event (``compact=true``)."""
    out = {k: item[k] for k in COMPACT_FIELDS if k in item}
    if "url" not in out and "tickets_url" in item:
        out["url"] = item["tickets_url"]
    return out


def compacted(rows: list[dict[str, Any]], compact: bool) -> list[dict[str, Any]]:
    """``rows`` as they are, or reduced to ``compact_row`` form."""
    return [compact_row(r) for r in rows] if compact else rows


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
        rows = await _fetch(pool, _collapsed(where, "dup_window_start NULLS LAST, id"), {**base, "limit": limit})
        results = [_row(r, tz) for r in rows]
        out: dict[str, Any] = {"semantic_search": "not_requested", "count": len(results), "results": results}
        return await _explain_empty(pool, embedder, query, filters, tz, max_distance, out)

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
    ordered = sorted(scores, key=lambda i: (-scores[i], i))
    rows = []
    if ordered:
        # Collapse among the candidates, then rank and cut: a duplicate must not take a slot of the limit.
        sql = _collapsed("l.id = ANY(%(ids)s)", "id", "")
        by_id = {r["id"]: r for r in await _fetch(pool, sql, {**base, "ids": ordered})}
        rows = [by_id[i] for i in ordered if i in by_id][:limit]

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
    return await _explain_empty(pool, embedder, query, filters, tz, max_distance, out)


async def _explain_empty(
    pool: Pool, embedder: Embedder | None, query: str, filters: Filters, tz: ZoneInfo, max_distance: float, out: dict
) -> dict[str, Any]:
    """Add a ``note`` to an empty dated search: what the same search finds without the date window.

    An agent that only sees "0 results" keeps rephrasing; this tells it whether the event exists at other
    dates, how far off the nearest one is, and whether undated items were left out by the window.
    """
    if out["results"] or not (filters.date_from or filters.date_to):
        return out
    relaxed = await search(
        pool, embedder, query, replace(filters, date_from=None, date_to=None, params={}), tz, 50, max_distance
    )
    found = relaxed["results"]
    window = " to ".join(
        d.astimezone(tz).strftime("%Y-%m-%d") for d in (filters.date_from, filters.date_to) if d is not None
    )
    undated = [r for r in found if "starts_at" not in r and "ends_at" not in r]
    dated = [r for r in found if r not in undated]
    parts = [f"Nothing matches within {window or 'the date range'}."]
    closest: dict[str, Any] = {"count": len(found), "undated_excluded_by_window": len(undated)}
    if dated:
        anchor = filters.date_from or filters.date_to
        assert anchor is not None  # noqa: S101 - the guard above
        nearest = min(dated, key=lambda r: abs(datetime.fromisoformat(r.get("starts_at") or r["ends_at"]) - anchor))
        closest["nearest"] = {k: nearest[k] for k in ("id", "title", "starts_at") if k in nearest}
        when = nearest.get("starts_at") or nearest["ends_at"]
        parts.append(
            f"Without the date filter there are {len(found)} match(es); the nearest is "
            f"'{nearest['title']}' (id {nearest['id']}) on {when}. A recurring series may list only its next "
            "occurrences, so a later date being absent does not mean the event does not exist."
        )
    else:
        parts.append(f"Without the date filter there are {len(found)} match(es).")
    parts.append(
        f"{len(undated)} undated match(es) were excluded because a date window skips undated items."
        if undated
        else "No undated items were excluded by the window."
    )
    out["closest_without_date_filter"] = closest
    out["note"] = " ".join([out["note"], *parts]) if out.get("note") else " ".join(parts)
    return out


async def upcoming(pool: Pool, filters: Filters, days: int, tz: ZoneInfo, limit: int = 50) -> list[dict[str, Any]]:
    """Not-yet-ended, not-cancelled items whose window overlaps the next ``days`` days."""
    where = filters.where()
    sql = _collapsed(
        f"""{where}
          AND l.lifecycle IN ('upcoming', 'ongoing')
          AND l.status IS DISTINCT FROM 'cancelled'
          AND (l.window_start IS NULL OR l.window_start <= now() + make_interval(days => %(days)s::int))""",
        "dup_window_start NULLS FIRST, id",
    )
    params = {**filters.params, "summary_chars": SUMMARY_CHARS, "days": days, "limit": limit}
    return [_row(r, tz) for r in await _fetch(pool, sql, params)]


async def ending_soon(pool: Pool, filters: Filters, days: int, tz: ZoneInfo, limit: int = 50) -> list[dict[str, Any]]:
    """Items with an explicit end date in the next ``days`` days, soonest first."""
    where = filters.where()
    sql = _collapsed(
        f"""{where}
          AND l.kind <> 'video'
          AND l.ends_at >= now() AND l.ends_at <= now() + make_interval(days => %(days)s::int)
          AND l.status IS DISTINCT FROM 'cancelled'""",
        "ends_at, id",
    )
    params = {**filters.params, "summary_chars": SUMMARY_CHARS, "days": days, "limit": limit}
    return [_row(r, tz) for r in await _fetch(pool, sql, params)]


async def get_event(pool: Pool, event_id: int, tz: ZoneInfo) -> dict[str, Any] | None:
    """One item with its full text, venue and source."""
    sql = """
        SELECT l.id, l.title, l.kind, l.starts_at, l.ends_at, l.city, l.url, l.status, l.lifecycle,
               l.is_stale, l.first_seen, l.last_seen, l.summary, l.raw_text, l.embedding_model,
               l.registration_state, l.registration_opens_at, l.registration_closes_at, l.tickets_url,
               u.status AS tracked_status, u.task_ref,
               v.name AS venue, v.address AS venue_address, v.city AS venue_city, v.lat AS venue_lat,
               v.lon AS venue_lon, s.name AS source, s.type AS source_type, s.url AS source_url
        FROM event_lifecycle l
        JOIN sources s ON s.id = l.source_id
        LEFT JOIN venues v ON v.id = l.venue_id
        LEFT JOIN event_user_state u ON u.event_id = l.id AND u.owner = 'default'
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


async def registration_deadlines(
    pool: Pool, filters: Filters, days: int, which: Literal["closing", "opening"], tz: ZoneInfo, limit: int = 50
) -> list[dict[str, Any]]:
    """Not-ended, not-cancelled items whose registration closes (or opens) within the next ``days`` days."""
    column: LiteralString = "l.registration_closes_at" if which == "closing" else "l.registration_opens_at"
    order: LiteralString = "registration_closes_at, id" if which == "closing" else "registration_opens_at, id"
    where = filters.where()
    sql = _collapsed(
        f"""{where}
          AND {column} >= now() AND {column} <= now() + make_interval(days => %(days)s::int)
          AND l.lifecycle <> 'ended'
          AND l.status IS DISTINCT FROM 'cancelled'""",
        order,
    )
    params = {**filters.params, "summary_chars": SUMMARY_CHARS, "days": days, "limit": limit}
    return [_row(r, tz) for r in await _fetch(pool, sql, params)]


async def track_event(pool: Pool, event_id: int, status: str, task_ref: str | None = None) -> dict[str, Any]:
    """Record what the user was told (raised), declined (dismissed) or planned, with a snapshot of the event.

    Calling it again replaces the status and refreshes the snapshot, which clears "changed". Rows for events
    that ended more than 60 days ago are purged on the way.
    """
    if status not in TRACK_STATUSES:
        raise ValueError(f"status must be one of {', '.join(TRACK_STATUSES)}")
    ref = task_ref.strip() if task_ref else None
    if ref and status != "planned":
        raise ValueError("task_ref only goes with status=planned")
    if ref and len(ref) > MAX_TASK_REF:
        raise ValueError(f"task_ref is a pointer, at most {MAX_TASK_REF} characters; notes belong in the task")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """DELETE FROM event_user_state u USING event_lifecycle l
               WHERE l.id = u.event_id AND l.window_end < now() - interval '60 days'"""
        )
        cur = await conn.execute(
            """
            INSERT INTO event_user_state AS u (
                event_id, owner, status, task_ref, snapshot_title, snapshot_starts_at,
                snapshot_registration_opens_at, snapshot_registration_closes_at, snapshot_event_status)
            SELECT e.id, 'default', %(status)s, %(ref)s, e.title, e.starts_at,
                   e.registration_opens_at, e.registration_closes_at, e.status
            FROM events e WHERE e.id = %(id)s
            ON CONFLICT (event_id, owner) DO UPDATE SET
                status = EXCLUDED.status, task_ref = EXCLUDED.task_ref,
                snapshot_title = EXCLUDED.snapshot_title, snapshot_starts_at = EXCLUDED.snapshot_starts_at,
                snapshot_registration_opens_at = EXCLUDED.snapshot_registration_opens_at,
                snapshot_registration_closes_at = EXCLUDED.snapshot_registration_closes_at,
                snapshot_event_status = EXCLUDED.snapshot_event_status,
                updated_at = now()
            RETURNING u.event_id, u.status, u.task_ref
            """,
            {"id": event_id, "status": status, "ref": ref},
        )
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"no item with id {event_id}")
    return {k: v for k, v in row.items() if v is not None}


async def untrack_event(pool: Pool, event_id: int) -> bool:
    """Forget an event entirely. Returns whether there was anything to forget."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM event_user_state WHERE event_id = %(id)s AND owner = 'default'", {"id": event_id}
        )
        return cur.rowcount > 0


def _fmt(value: Any, tz: ZoneInfo) -> Any:
    return value.astimezone(tz).isoformat() if isinstance(value, datetime) else value


async def my_events(
    pool: Pool, status: str | None, changed: bool, tz: ZoneInfo, limit: int = 50
) -> list[dict[str, Any]]:
    """Tracked events with their current data; ``changes`` lists what differs from what the user was told."""
    if status is not None and status not in TRACK_STATUSES:
        raise ValueError(f"status must be one of {', '.join(TRACK_STATUSES)}")
    sql = f"""
        SELECT {_LIST_COLUMNS}, u.task_ref, u.updated_at AS tracked_at, u.snapshot_title,
               u.snapshot_starts_at, u.snapshot_registration_opens_at, u.snapshot_registration_closes_at,
               u.snapshot_event_status
        {_LIST_FROM}
        WHERE u.event_id IS NOT NULL
          AND (%(status)s::text IS NULL OR u.status = %(status)s)
          AND (NOT %(changed)s OR {_CHANGED})
          AND (l.window_end IS NULL OR l.window_end >= now() - interval '60 days')
        ORDER BY l.window_start NULLS LAST, l.id
        LIMIT %(limit)s
    """
    params = {"summary_chars": SUMMARY_CHARS, "status": status, "changed": changed, "limit": limit}
    out = []
    for r in await _fetch(pool, sql, params):
        changes = {
            now_key: {"told": _fmt(r[snap_key], tz), "now": _fmt(r[now_key], tz)}
            for now_key, snap_key in _CHANGE_FIELDS
            if r[now_key] != r[snap_key]
        }
        item = _row({k: v for k, v in r.items() if not k.startswith("snapshot_")}, tz)
        if changes:
            item["changes"] = changes
        out.append(item)
    return out
