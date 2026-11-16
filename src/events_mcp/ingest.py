"""Write path: idempotent upserts, scrape-run bookkeeping and the embedding backfill.

The scraper (a separate project) is expected to write through the same SQL; the ``import`` CLI
command uses it directly, which is also how test rows get in.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from events_mcp.embeddings import Embedder, document_text, vector_literal

log = logging.getLogger(__name__)

RAW_TEXT_LIMIT = 20000


@dataclass
class Item:
    """One scraped listing, as the scraper hands it over."""

    title: str
    dedupe_key: str
    kind: str
    summary: str | None = None
    raw_text: str | None = None
    url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    city: str | None = None
    status: str | None = None
    registration_opens_at: datetime | None = None
    registration_closes_at: datetime | None = None
    tickets_url: str | None = None
    venue: dict[str, Any] | None = None


async def upsert_source(
    conn: AsyncConnection[Any], name: str, type_: str, url: str, city: str | None = None, scrape_interval: str = "1 day"
) -> int:
    """Insert or update a source by name; returns its id."""
    cur = await conn.execute(
        """
        INSERT INTO sources (name, type, url, city, scrape_interval)
        VALUES (%s, %s, %s, %s, %s::interval)
        ON CONFLICT (name) DO UPDATE
            SET type = EXCLUDED.type, url = EXCLUDED.url, city = EXCLUDED.city,
                scrape_interval = EXCLUDED.scrape_interval
        RETURNING id
        """,
        (name, type_, url, city, scrape_interval),
    )
    row = await cur.fetchone()
    assert row is not None  # noqa: S101 - RETURNING always yields a row
    return _first(row)


async def upsert_venue(conn: AsyncConnection[Any], venue: dict[str, Any]) -> int:
    """Insert or update a venue keyed on (name, city); returns its id."""
    cur = await conn.execute(
        """
        INSERT INTO venues (name, city, address, lat, lon)
        VALUES (%(name)s, %(city)s, %(address)s, %(lat)s, %(lon)s)
        ON CONFLICT (name, city) DO UPDATE
            SET address = coalesce(EXCLUDED.address, venues.address),
                lat = coalesce(EXCLUDED.lat, venues.lat),
                lon = coalesce(EXCLUDED.lon, venues.lon)
        RETURNING id
        """,
        {
            "name": venue["name"],
            "city": venue.get("city"),
            "address": venue.get("address"),
            "lat": venue.get("lat"),
            "lon": venue.get("lon"),
        },
    )
    row = await cur.fetchone()
    assert row is not None  # noqa: S101
    return _first(row)


def _first(row: Any) -> Any:
    return row["id"] if isinstance(row, dict) else row[0]


async def upsert_event(
    conn: AsyncConnection[Any],
    source_id: int,
    item: Item,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
) -> tuple[int, bool]:
    """Insert or update one item keyed on ``(source_id, dedupe_key)``. Returns ``(id, inserted)``.

    ``first_seen`` is kept, ``last_seen`` moves to now. An existing embedding survives an update
    that leaves title, summary and raw_text alone; otherwise it is replaced or cleared, so the
    backfill re-embeds changed text.
    """
    venue_id = await upsert_venue(conn, item.venue) if item.venue else None
    raw = item.raw_text[:RAW_TEXT_LIMIT] if item.raw_text else None
    vec = vector_literal(embedding) if embedding is not None else None
    cur = await conn.execute(
        """
        INSERT INTO events AS e (source_id, venue_id, title, summary, raw_text, url, starts_at, ends_at,
                                 city, kind, dedupe_key, status, registration_opens_at,
                                 registration_closes_at, tickets_url, embedding, embedding_model)
        VALUES (%(source_id)s, %(venue_id)s, %(title)s, %(summary)s, %(raw)s, %(url)s, %(starts_at)s,
                %(ends_at)s, %(city)s, %(kind)s, %(dedupe_key)s, %(status)s, %(reg_opens)s, %(reg_closes)s,
                %(tickets_url)s, %(vec)s::vector,
                CASE WHEN %(vec)s::text IS NULL THEN NULL ELSE %(model)s::text END)
        ON CONFLICT ON CONSTRAINT events_source_dedupe_key DO UPDATE SET
            venue_id = EXCLUDED.venue_id,
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            raw_text = EXCLUDED.raw_text,
            url = EXCLUDED.url,
            starts_at = EXCLUDED.starts_at,
            ends_at = EXCLUDED.ends_at,
            city = EXCLUDED.city,
            kind = EXCLUDED.kind,
            status = EXCLUDED.status,
            -- A cheaper tier that does not read registration must not erase what the LLM found. The two
            -- dates move together (new pair, or the old pair), so the order check cannot be broken by a mix.
            registration_opens_at = CASE
                WHEN EXCLUDED.registration_opens_at IS NOT NULL OR EXCLUDED.registration_closes_at IS NOT NULL
                THEN EXCLUDED.registration_opens_at ELSE e.registration_opens_at END,
            registration_closes_at = CASE
                WHEN EXCLUDED.registration_opens_at IS NOT NULL OR EXCLUDED.registration_closes_at IS NOT NULL
                THEN EXCLUDED.registration_closes_at ELSE e.registration_closes_at END,
            tickets_url = coalesce(EXCLUDED.tickets_url, e.tickets_url),
            last_seen = now(),
            embedding = CASE
                WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding
                WHEN (e.title, e.summary, e.raw_text) IS NOT DISTINCT FROM
                     (EXCLUDED.title, EXCLUDED.summary, EXCLUDED.raw_text) THEN e.embedding
            END,
            embedding_model = CASE
                WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding_model
                WHEN (e.title, e.summary, e.raw_text) IS NOT DISTINCT FROM
                     (EXCLUDED.title, EXCLUDED.summary, EXCLUDED.raw_text) THEN e.embedding_model
            END
        RETURNING id, (xmax = 0) AS inserted
        """,
        {
            "source_id": source_id,
            "venue_id": venue_id,
            "title": item.title,
            "summary": item.summary,
            "raw": raw,
            "url": item.url,
            "starts_at": item.starts_at,
            "ends_at": item.ends_at,
            "city": item.city,
            "kind": item.kind,
            "dedupe_key": item.dedupe_key,
            "status": item.status,
            "reg_opens": item.registration_opens_at,
            "reg_closes": item.registration_closes_at,
            "tickets_url": item.tickets_url,
            "vec": vec,
            "model": embedding_model,
        },
    )
    row = await cur.fetchone()
    assert row is not None  # noqa: S101
    if isinstance(row, dict):
        return row["id"], bool(row["inserted"])
    return row[0], bool(row[1])


async def start_run(conn: AsyncConnection[Any], source_id: int) -> int:
    """Open a ``scrape_runs`` row in state ``running``."""
    cur = await conn.execute(
        "INSERT INTO scrape_runs (source_id) VALUES (%s) RETURNING id",
        (source_id,),
    )
    row = await cur.fetchone()
    assert row is not None  # noqa: S101
    return _first(row)


async def finish_run(
    conn: AsyncConnection[Any], run_id: int, status: str, items_seen: int, error: str | None = None
) -> None:
    """Close a run and stamp ``sources.last_run``."""
    await conn.execute(
        """
        WITH r AS (
            UPDATE scrape_runs SET finished_at = now(), status = %s, items_seen = %s, error = %s
            WHERE id = %s RETURNING source_id, finished_at
        )
        UPDATE sources s SET last_run = r.finished_at FROM r WHERE s.id = r.source_id
        """,
        (status, items_seen, error, run_id),
    )


async def backfill_embeddings(conn: AsyncConnection[Any], embedder: Embedder, batch_size: int = 16) -> int:
    """Embed every item that has no embedding or one from another model. Returns the number embedded.

    Stops early, without error, when the endpoint becomes unavailable; rerun it later.
    """
    total = 0
    while True:
        cur = await conn.execute(
            """
            SELECT e.id, e.title, e.summary, e.raw_text, e.kind, e.city, v.name AS venue
            FROM events e LEFT JOIN venues v ON v.id = e.venue_id
            WHERE e.embedding IS NULL OR e.embedding_model IS DISTINCT FROM %s
            ORDER BY e.id
            LIMIT %s
            """,
            (embedder.model, batch_size),
        )
        rows = await cur.fetchall()
        if not rows:
            return total
        texts = [document_text(r["title"], r["summary"], r["raw_text"], r["kind"], r["city"], r["venue"]) for r in rows]
        vectors = await embedder.embed(texts)
        if vectors is None:
            log.warning("embedding endpoint unavailable; stopping after %d", total)
            return total
        for r, vec in zip(rows, vectors, strict=True):
            await conn.execute(
                "UPDATE events SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
                (vector_literal(vec), embedder.model, r["id"]),
            )
        total += len(rows)
        log.info("embedded %d items", total)


async def touch_source(conn: AsyncConnection[Any], source_id: int) -> int:
    """Bump ``last_seen`` for the rows present in the last successful extraction. Returns the row count.

    Used when the page is unchanged: nothing is re-extracted, but the items are still on the page.
    Rows that dropped out of an earlier extraction have ``last_seen`` before ``last_extracted_at``
    and are left alone, so they go stale on schedule.
    """
    cur = await conn.execute(
        """
        UPDATE events e SET last_seen = now()
        FROM sources s
        WHERE s.id = %s AND e.source_id = s.id AND s.last_extracted_at IS NOT NULL
          AND e.last_seen >= s.last_extracted_at
        """,
        (source_id,),
    )
    return cur.rowcount


async def mark_extracted(conn: AsyncConnection[Any], source_id: int, content_hash: str, run_id: int) -> None:
    """Record that ``content_hash`` was fully extracted by ``run_id``.

    ``last_extracted_at`` is the run's *start*, not now: upserts stamp ``last_seen`` inside the run,
    so a later timestamp would exclude every row they wrote from the next ``touch_source``.
    """
    await conn.execute(
        """
        UPDATE sources SET last_extracted_hash = %(h)s, last_content_hash = %(h)s,
            last_extracted_at = (SELECT started_at FROM scrape_runs WHERE id = %(run)s)
        WHERE id = %(src)s
        """,
        {"h": content_hash, "run": run_id, "src": source_id},
    )


async def finish_run_detailed(
    conn: AsyncConnection[Any],
    run_id: int,
    status: str,
    *,
    items_seen: int = 0,
    error: str | None = None,
    extractor: str | None = None,
    content_hash: str | None = None,
    snapshot_key: str | None = None,
    llm_provider: str | None = None,
    llm_calls: int = 0,
    llm_input_chars: int = 0,
    items_new: int = 0,
    items_updated: int = 0,
    items_touched: int = 0,
    items_rejected: int = 0,
) -> None:
    """Close a run with the crawler's full accounting, and stamp ``sources.last_run``."""
    await conn.execute(
        """
        WITH r AS (
            UPDATE scrape_runs SET finished_at = now(), status = %(status)s, items_seen = %(seen)s, error = %(error)s,
                extractor = %(extractor)s, content_hash = %(hash)s, snapshot_key = %(snap)s,
                llm_provider = %(provider)s, llm_calls = %(calls)s, llm_input_chars = %(chars)s,
                items_new = %(new)s, items_updated = %(upd)s, items_touched = %(touched)s,
                items_rejected = %(rej)s
            WHERE id = %(run)s RETURNING source_id, finished_at
        )
        UPDATE sources s SET last_run = r.finished_at FROM r WHERE s.id = r.source_id
        """,
        {
            "run": run_id,
            "status": status,
            "seen": items_seen,
            "error": error,
            "extractor": extractor,
            "hash": content_hash,
            "snap": snapshot_key,
            "provider": llm_provider,
            "calls": llm_calls,
            "chars": llm_input_chars,
            "new": items_new,
            "upd": items_updated,
            "touched": items_touched,
            "rej": items_rejected,
        },
    )


async def sync_source(
    conn: AsyncConnection[Any], name: str, type_: str, url: str, city: str | None, interval: str, config: dict[str, Any]
) -> int:
    """Upsert a source from ``sources.yaml`` and (re-)enable it. Runtime state columns are left alone."""
    cur = await conn.execute(
        """
        INSERT INTO sources (name, type, url, city, scrape_interval, config, enabled)
        VALUES (%s, %s, %s, %s, %s::interval, %s::jsonb, true)
        ON CONFLICT (name) DO UPDATE
            SET type = EXCLUDED.type, url = EXCLUDED.url, city = EXCLUDED.city,
                scrape_interval = EXCLUDED.scrape_interval, config = EXCLUDED.config, enabled = true
        RETURNING id
        """,
        (name, type_, url, city, interval, json.dumps(config)),
    )
    row = await cur.fetchone()
    assert row is not None  # noqa: S101
    return _first(row)


async def disable_missing(conn: AsyncConnection[Any], names: list[str]) -> list[str]:
    """Disable every source not in ``names`` (rows and history stay). Returns the names newly disabled."""
    cur = await conn.execute(
        "UPDATE sources SET enabled = false WHERE enabled AND NOT (name = ANY(%s)) RETURNING name", (names,)
    )
    return [r["name"] if isinstance(r, dict) else r[0] for r in await cur.fetchall()]
