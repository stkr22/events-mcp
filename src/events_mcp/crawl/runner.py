"""The scrape loop: sync sources, pick the due ones, process each in isolation."""

import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from events_mcp.crawl.browser import BrowserFetcher
from events_mcp.crawl.clean import apply_ignore_patterns, content_hash, html_to_markdown
from events_mcp.crawl.config import CrawlerSettings, ResolvedSource, load_sources
from events_mcp.crawl.dispatch import NoExtractorError, fetch_pages, structured
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.fetch import Fetcher, FetchError, RobotsBlockedError
from events_mcp.crawl.geocode import Geocoder
from events_mcp.crawl.llm import BadOutputError, ChatClient, NoProviderError, build_chain, extract_with_llm
from events_mcp.crawl.models import Page, VenueRef, canonical_payload
from events_mcp.crawl.pipeline import build_items
from events_mcp.crawl.snapshots import SnapshotStore, snapshot_key
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


@dataclass
class Services:
    """Everything a run needs besides the source: shared across the sources of one invocation."""

    settings: CrawlerSettings
    fetcher: Fetcher
    browser: BrowserFetcher | None
    geocoder: Geocoder | None
    snapshots: SnapshotStore | None
    chain: Sequence[ChatClient]


def pages_to_text(pages: list[Page], ignore_patterns: list[str]) -> str:
    """Join the cleaned markdown of every page, dropping volatile lines before hashing."""
    parts = [html_to_markdown(p.text, p.url) if p.is_html else p.text.strip() for p in pages]
    return apply_ignore_patterns("\n\n---\n\n".join(x for x in parts if x), ignore_patterns).strip()


async def _store(
    conn: AsyncConnection[Any],
    source_id: int,
    run_id: int,
    pairs: list[tuple[Item, VenueRef | None]],
    digest: str,
    geocoder: Geocoder | None,
) -> int:
    """Upsert the items and record the extraction in one transaction. Returns the number of new items."""
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
    return new


async def _llm_path(
    conn: AsyncConnection[Any],
    svc: Services,
    source: ResolvedSource,
    source_id: int,
    run_id: int,
    text: str,
    digest: str,
    now: datetime,
    outcome: dict[str, Any],
) -> None:
    """Extract ``text`` with the provider chain and store the result; fills ``outcome``."""
    tz = ZoneInfo(source.timezone)
    try:
        raw, rejected, usage = await extract_with_llm(
            svc.chain, source, text, source.primary_url, tz, now, svc.settings.llm_chunk_chars
        )
    except NoProviderError as exc:
        outcome.update(status="pending_extraction", error=str(exc))
        return
    outcome.update(llm_calls=usage.calls, llm_input_chars=usage.input_chars, llm_provider=usage.provider)
    pairs, plausibility = build_items(raw, source, tz, now)
    new = await _store(conn, source_id, run_id, pairs, digest, svc.geocoder)
    outcome.update(
        status="success",
        items_seen=len(pairs),
        items_new=new,
        items_updated=len(pairs) - new,
        items_rejected=outcome.get("items_rejected", 0) + rejected + plausibility,
        error="; ".join(usage.notes) or None,
    )


async def _process(  # noqa: PLR0915
    dsn: str, source: ResolvedSource, source_id: int, svc: Services, *, force: bool, extractor: str | None
) -> str:
    """One source, one run. Never raises: every outcome lands in ``scrape_runs``."""
    tz = ZoneInfo(source.timezone)
    async with await connect(dsn) as conn:
        state = await load_state(conn, source_id)
        run_id = await start_run(conn, source_id)
        outcome: dict[str, Any] = {"status": "failed"}
        try:
            now = datetime.now(UTC)
            pages = await fetch_pages(source, svc.fetcher, svc.browser)
            ctx = Context(source, tz, now, svc.settings.horizon_days)
            only = extractor or source.extractor
            result = await structured(source, pages, svc.fetcher, ctx, only=only, preferred=state.last_extractor)
            cur = await conn.execute("SELECT last_extracted_hash FROM sources WHERE id = %s", (source_id,))
            row = await cur.fetchone()
            last_hash = row["last_extracted_hash"] if row else None
            if result is not None:
                pairs, rejected = build_items(result.items, source, tz, now)
                digest = content_hash(canonical_payload(result.items))
                outcome.update(
                    extractor=result.extractor, content_hash=digest, items_rejected=rejected + result.rejected
                )
                if last_hash == digest and not force:
                    touched = await touch_source(conn, source_id)
                    outcome.update(status="success", items_touched=touched, items_seen=len(pairs))
                else:
                    new = await _store(conn, source_id, run_id, pairs, digest, svc.geocoder)
                    outcome.update(
                        status="success", items_seen=len(pairs), items_new=new, items_updated=len(pairs) - new
                    )
            else:
                text = pages_to_text(pages, source.ignore_patterns)
                if not text:
                    raise FetchError("the page has no extractable text")
                digest = content_hash(text)
                outcome.update(extractor="llm", content_hash=digest)
                if last_hash == digest and not force:
                    touched = await touch_source(conn, source_id)
                    outcome.update(status="success", items_touched=touched)
                else:
                    if svc.snapshots is not None:
                        key = snapshot_key(source.name, digest, now.date())
                        try:
                            await svc.snapshots.put(key, text)
                            outcome["snapshot_key"] = key
                        except Exception:
                            log.exception("snapshot upload failed for %s; continuing without", source.name)
                    await _llm_path(conn, svc, source, source_id, run_id, text, digest, now, outcome)
        except RobotsBlockedError as exc:
            outcome.update(status="blocked_robots", error=f"robots.txt disallows {exc}")
        except (FetchError, NoExtractorError, BadOutputError) as exc:
            outcome.update(status="failed", error=str(exc)[:1000])
        except Exception as exc:
            log.exception("source %s crashed", source.name)
            outcome.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        await finish_run_detailed(conn, run_id, **outcome)
        log.info("%s: %s %s", source.name, outcome["status"], {k: v for k, v in outcome.items() if k != "status"})
        return outcome["status"]


async def reextract(settings: CrawlerSettings, name: str, snapshot: str | None) -> int:
    """Run the LLM tier again on a stored snapshot (prompt iteration without refetching)."""
    sources = {s.name: s for s in load_sources(settings.sources_file)}
    store = SnapshotStore.from_settings(settings)
    if name not in sources or store is None or settings.database_url is None:
        print("unknown source, or S3/DATABASE_URL not configured", file=sys.stderr)
        return 2
    key = snapshot or await store.latest(name)
    if key is None:
        print(f"no snapshot for {name}", file=sys.stderr)
        return 2
    text = await store.get(key)
    svc = Services(settings, Fetcher(settings.user_agent), None, None, None, build_chain(settings))
    dsn = settings.database_url.get_secret_value()
    source = sources[name]
    async with await connect(dsn) as conn:
        ids = await sync_sources(conn, list(sources.values()))
        run_id = await start_run(conn, ids[name])
        outcome: dict[str, Any] = {"status": "failed", "extractor": "llm", "snapshot_key": key}
        try:
            await _llm_path(conn, svc, source, ids[name], run_id, text, content_hash(text), datetime.now(UTC), outcome)
        except Exception as exc:
            outcome.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        outcome.setdefault("content_hash", content_hash(text))
        await finish_run_detailed(conn, run_id, **outcome)
    await svc.fetcher.aclose()
    print(f"{key}: {outcome['status']} {outcome.get('error') or ''}")
    return 0 if outcome["status"] == "success" else 1


async def dry_run_source(
    source: ResolvedSource, svc: Services, now: datetime, extractor: str | None
) -> list[dict[str, Any]]:
    """Fetch and extract one source; return the Phase 1 import records. Touches no database and no S3."""
    tz = ZoneInfo(source.timezone)
    pages = await fetch_pages(source, svc.fetcher, svc.browser)
    ctx = Context(source, tz, now)
    result = await structured(source, pages, svc.fetcher, ctx, only=extractor or source.extractor)
    if result is not None:
        raw, rejected, tier = result.items, result.rejected, result.extractor
    else:
        text = pages_to_text(pages, source.ignore_patterns)
        raw, llm_rejected, usage = await extract_with_llm(
            svc.chain, source, text, source.primary_url, tz, now, svc.settings.llm_chunk_chars
        )
        rejected, tier = llm_rejected, f"llm ({usage.provider}, {usage.calls} calls)"
    pairs, plausibility = build_items(raw, source, tz, now)
    log.info("%s: tier %s, %d items, %d rejected", source.name, tier, len(pairs), rejected + plausibility)
    return [_jsonl(i, v) for i, v in pairs]


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

    svc = Services(
        settings,
        Fetcher(settings.user_agent),
        BrowserFetcher(settings.cdp_url, settings.user_agent) if settings.cdp_url else None,
        Geocoder(settings.geocoder_url, settings.user_agent) if settings.geocode and not dry_run else None,
        None if dry_run else SnapshotStore.from_settings(settings),
        build_chain(settings),
    )
    try:
        if dry_run:
            now = datetime.now(UTC)
            for s in selected:
                try:
                    for rec in await dry_run_source(s, svc, now, extractor):
                        print(json.dumps(rec, ensure_ascii=False))
                except (FetchError, RobotsBlockedError, NoExtractorError, NoProviderError, BadOutputError) as exc:
                    log.error("%s: %s", s.name, exc)
            return 0
        return await _scrape_live(svc, sources, selected, force=force, extractor=extractor)
    finally:
        await svc.fetcher.aclose()
        if svc.geocoder:
            await svc.geocoder.aclose()


async def _scrape_live(
    svc: Services, sources: list[ResolvedSource], selected: list[ResolvedSource], *, force: bool, extractor: str | None
) -> int:
    settings = svc.settings
    if settings.database_url is None:
        log.error("DATABASE_URL is not set")
        return 2
    dsn = settings.database_url.get_secret_value()
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
    if svc.snapshots is not None:
        try:
            await svc.snapshots.prune()
        except Exception:
            log.exception("snapshot pruning failed")

    sem = asyncio.Semaphore(settings.max_concurrency)
    host_locks: dict[str, asyncio.Lock] = {}

    async def one(s: ResolvedSource) -> str:
        host = urlparse(s.primary_url).hostname or s.name
        async with sem, host_locks.setdefault(host, asyncio.Lock()):
            limit = s.timeout_sec or settings.source_timeout_sec
            try:
                async with asyncio.timeout(limit):
                    return await _process(dsn, s, ids[s.name], svc, force=force, extractor=extractor)
            except TimeoutError:
                async with await connect(dsn) as conn:
                    await conn.execute(
                        "UPDATE scrape_runs SET status='failed', finished_at=now(), error=%s "
                        "WHERE source_id=%s AND status='running'",
                        (f"timed out after {limit:.0f}s", ids[s.name]),
                    )
                return "failed"

    await asyncio.gather(*(one(s) for s in due))
    return 0
