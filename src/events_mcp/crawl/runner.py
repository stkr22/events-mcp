"""The scrape loop: sync sources, pick the due ones, process each in isolation."""

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from events_mcp.crawl.clean import content_hash
from events_mcp.crawl.config import CrawlerSettings, ResolvedSource, load_sources
from events_mcp.crawl.dispatch import NoExtractorError, extract, fetch_pages
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.fetch import Fetcher, FetchError, RobotsBlockedError
from events_mcp.crawl.geocode import Geocoder
from events_mcp.crawl.models import VenueRef, canonical_payload
from events_mcp.crawl.pipeline import build_items
from events_mcp.db import connect
from events_mcp.ingest import (
    Item,
    disable_missing,
    finish_run_detailed,
    mark_extracted,
    start_run,
    sync_source,
    touch_source,
    upsert_event,
    upsert_venue,
)

log = logging.getLogger(__name__)

RETRY_BACKOFF = (timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1))
DUE_SLACK = timedelta(minutes=2)
STUCK_AFTER = timedelta(hours=2)
FAILURE_STATUSES = ("failed", "pending_extraction", "blocked_robots")


@dataclass
class SourceState:
    """What the due check needs to know about a source's history."""

    last_success: datetime | None
    last_finished: datetime | None
    failures_since_success: int
    last_extractor: str | None


def is_due(interval: timedelta, state: SourceState, now: datetime) -> bool:
    """Interval elapsed since the last success, or the retry backoff elapsed after failures."""
    if state.last_finished is None:
        return True
    if state.failures_since_success:
        n = state.failures_since_success
        wait = RETRY_BACKOFF[n - 1] if n <= len(RETRY_BACKOFF) else interval
        return now - state.last_finished >= min(wait, interval) - DUE_SLACK
    assert state.last_success is not None  # noqa: S101 - no failures and a finished run means a success
    return now - state.last_success >= interval - DUE_SLACK


async def load_state(conn: AsyncConnection[Any], source_id: int) -> SourceState:
    """History of one source from ``scrape_runs``."""
    cur = await conn.execute(
        """
        SELECT
          max(finished_at) FILTER (WHERE status = 'success') AS last_success,
          max(finished_at) FILTER (WHERE status <> 'running') AS last_finished,
          (SELECT count(*) FROM scrape_runs f WHERE f.source_id = %(id)s AND f.status = ANY(%(bad)s)
             AND f.started_at > coalesce(
               (SELECT max(started_at) FROM scrape_runs WHERE source_id = %(id)s AND status = 'success'),
               '-infinity')) AS failures,
          (SELECT extractor FROM scrape_runs WHERE source_id = %(id)s AND status = 'success'
             AND extractor IS NOT NULL ORDER BY started_at DESC LIMIT 1) AS extractor
        FROM scrape_runs WHERE source_id = %(id)s
        """,
        {"id": source_id, "bad": list(FAILURE_STATUSES)},
    )
    r = await cur.fetchone()
    assert r is not None  # noqa: S101 - aggregate query always returns a row
    return SourceState(r["last_success"], r["last_finished"], r["failures"], r["extractor"])


async def sync_sources(conn: AsyncConnection[Any], sources: list[ResolvedSource]) -> dict[str, int]:
    """Upsert every source from the file, disable the rest. Returns ``name -> id``."""
    ids = {}
    for s in sources:
        ids[s.name] = await sync_source(
            conn,
            s.name,
            s.type,
            s.primary_url,
            s.city,
            f"{int(s.interval_delta.total_seconds())} seconds",
            s.model_dump(mode="json", exclude_none=True),
        )
    for name in await disable_missing(conn, list(ids)):
        log.info("source %s is no longer in the file: disabled", name)
    return ids


async def close_stuck_runs(conn: AsyncConnection[Any]) -> None:
    """Close old ``running`` rows as failed: a pod killed mid-run leaves one behind."""
    await conn.execute(
        "UPDATE scrape_runs SET status = 'failed', finished_at = now(), error = 'run did not finish (pod killed?)' "
        "WHERE status = 'running' AND started_at < now() - %s",
        (STUCK_AFTER,),
    )


async def _resolve_venue(
    conn: AsyncConnection[Any],
    ref: VenueRef | None,
    geocoder: Geocoder | None,
    cache: dict[tuple[str, str | None], int],
) -> int | None:
    """Venue id for ``ref``: a known (name, city) is reused as is; only a new one is geocoded."""
    if ref is None:
        return None
    key = (ref.name, ref.city)
    if key in cache:
        return cache[key]
    cur = await conn.execute(
        "SELECT id FROM venues WHERE name = %s AND city IS NOT DISTINCT FROM %s", (ref.name, ref.city)
    )
    row = await cur.fetchone()
    if row is not None and ref.lat is None:
        cache[key] = row["id"]
        return row["id"]
    lat, lon = ref.lat, ref.lon
    if (
        row is None
        and lat is None
        and geocoder is not None
        and (hit := await geocoder.lookup(ref.name, ref.address, ref.city))
    ):
        lat, lon = hit
    venue_id = await upsert_venue(
        conn, {"name": ref.name, "city": ref.city, "address": ref.address, "lat": lat, "lon": lon}
    )
    cache[key] = venue_id
    return venue_id


def _jsonl(item: Item, venue: VenueRef | None) -> dict[str, Any]:
    out = {
        "title": item.title,
        "dedupe_key": item.dedupe_key,
        "kind": item.kind,
        "summary": item.summary,
        "raw_text": item.raw_text,
        "url": item.url,
        "starts_at": item.starts_at.isoformat() if item.starts_at else None,
        "ends_at": item.ends_at.isoformat() if item.ends_at else None,
        "city": item.city,
        "status": item.status,
        "venue": venue.model_dump(exclude_none=True) if venue else None,
    }
    return {k: v for k, v in out.items() if v is not None}


async def dry_run_source(
    source: ResolvedSource, fetcher: Fetcher, now: datetime, extractor: str | None
) -> list[dict[str, Any]]:
    """Fetch and extract one source; return the Phase 1 import records. Touches no database."""
    tz = ZoneInfo(source.timezone)
    pages = await fetch_pages(source, fetcher)
    ctx = Context(source, tz, now)
    result = await extract(source, pages, fetcher, ctx, only=extractor or source.extractor)
    pairs, rejected = build_items(result.items, source, tz, now)
    log.info(
        "%s: tier %s, %d items, %d rejected", source.name, result.extractor, len(pairs), rejected + result.rejected
    )
    return [_jsonl(i, v) for i, v in pairs]


async def _process(
    dsn: str,
    source: ResolvedSource,
    source_id: int,
    fetcher: Fetcher,
    geocoder: Geocoder | None,
    settings: CrawlerSettings,
    *,
    force: bool,
    extractor: str | None,
) -> str:
    """One source, one run. Never raises: every outcome lands in ``scrape_runs``."""
    tz = ZoneInfo(source.timezone)
    async with await connect(dsn) as conn:
        state = await load_state(conn, source_id)
        run_id = await start_run(conn, source_id)
        outcome: dict[str, Any] = {"status": "failed"}
        try:
            now = datetime.now(UTC)
            pages = await fetch_pages(source, fetcher)
            ctx = Context(source, tz, now, settings.horizon_days)
            result = await extract(
                source, pages, fetcher, ctx, only=extractor or source.extractor, preferred=state.last_extractor
            )
            pairs, rejected = build_items(result.items, source, tz, now)
            digest = content_hash(canonical_payload(result.items))
            outcome.update(extractor=result.extractor, content_hash=digest, items_rejected=rejected + result.rejected)
            cur = await conn.execute("SELECT last_extracted_hash FROM sources WHERE id = %s", (source_id,))
            row = await cur.fetchone()
            if row and row["last_extracted_hash"] == digest and not force:
                touched = await touch_source(conn, source_id)
                outcome.update(status="success", items_touched=touched, items_seen=len(pairs))
            else:
                new = 0
                cache: dict[tuple[str, str | None], int] = {}
                async with conn.transaction():
                    for item, ref in pairs:
                        venue_id = await _resolve_venue(conn, ref, geocoder, cache)
                        item_id, inserted = await upsert_event(conn, source_id, item)
                        if venue_id is not None:
                            await conn.execute("UPDATE events SET venue_id = %s WHERE id = %s", (venue_id, item_id))
                        new += int(inserted)
                    await mark_extracted(conn, source_id, digest, run_id)
                outcome.update(status="success", items_seen=len(pairs), items_new=new, items_updated=len(pairs) - new)
        except RobotsBlockedError as exc:
            outcome.update(status="blocked_robots", error=f"robots.txt disallows {exc}")
        except (FetchError, NoExtractorError) as exc:
            outcome.update(status="failed", error=str(exc)[:1000])
        except Exception as exc:
            log.exception("source %s crashed", source.name)
            outcome.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        await finish_run_detailed(conn, run_id, **outcome)
        log.info("%s: %s %s", source.name, outcome["status"], {k: v for k, v in outcome.items() if k != "status"})
        return outcome["status"]


async def scrape(
    settings: CrawlerSettings,
    *,
    names: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    extractor: str | None = None,
) -> int:
    """Entry point of ``events-mcp scrape``. Returns the process exit code."""
    try:
        sources = load_sources(settings.sources_file)
    except (OSError, ValueError) as exc:
        print(f"invalid sources file {settings.sources_file}: {exc}", file=sys.stderr)
        return 2
    selected = [s for s in sources if not names or s.name in names]
    if names and (missing := set(names) - {s.name for s in sources}):
        print(f"unknown source(s): {', '.join(sorted(missing))}", file=sys.stderr)
        return 2

    fetcher = Fetcher(settings.user_agent)
    try:
        if dry_run:
            now = datetime.now(UTC)
            for s in selected:
                try:
                    for rec in await dry_run_source(s, fetcher, now, extractor):
                        print(json.dumps(rec, ensure_ascii=False))
                except (FetchError, RobotsBlockedError, NoExtractorError) as exc:
                    log.error("%s: %s", s.name, exc)
            return 0
        return await _scrape_live(settings, sources, selected, fetcher, force=force, extractor=extractor)
    finally:
        await fetcher.aclose()


async def _scrape_live(
    settings: CrawlerSettings,
    sources: list[ResolvedSource],
    selected: list[ResolvedSource],
    fetcher: Fetcher,
    *,
    force: bool,
    extractor: str | None,
) -> int:
    if settings.database_url is None:
        log.error("DATABASE_URL is not set")
        return 2
    dsn = settings.database_url.get_secret_value()
    geocoder = Geocoder(settings.geocoder_url, settings.user_agent) if settings.geocode else None
    try:
        async with await connect(dsn) as conn:
            ids = await sync_sources(conn, sources)
            await close_stuck_runs(conn)
            now = datetime.now(UTC)
            due = []
            for s in selected:
                if force or is_due(s.interval_delta, await load_state(conn, ids[s.name]), now):
                    due.append(s)
    except Exception:
        log.exception("database unavailable or schema not migrated")
        return 1
    log.info("%d of %d sources due", len(due), len(selected))

    sem = asyncio.Semaphore(settings.max_concurrency)
    host_locks: dict[str, asyncio.Lock] = {}

    async def one(s: ResolvedSource) -> str:
        host = urlparse(s.primary_url).hostname or s.name
        async with sem, host_locks.setdefault(host, asyncio.Lock()):
            try:
                async with asyncio.timeout(settings.source_timeout_sec):
                    return await _process(
                        dsn, s, ids[s.name], fetcher, geocoder, settings, force=force, extractor=extractor
                    )
            except TimeoutError:
                async with await connect(dsn) as conn:
                    await conn.execute(
                        "UPDATE scrape_runs SET status='failed', finished_at=now(), error=%s "
                        "WHERE source_id=%s AND status='running'",
                        (f"timed out after {settings.source_timeout_sec:.0f}s", ids[s.name]),
                    )
                return "failed"

    try:
        await asyncio.gather(*(one(s) for s in due))
    finally:
        if geocoder:
            await geocoder.aclose()
    return 0
