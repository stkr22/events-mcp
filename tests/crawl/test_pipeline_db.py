"""Run loop against a real Postgres and a mocked web."""

from datetime import UTC, datetime

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation
from psycopg.rows import DictRow

from events_mcp.crawl.config import CrawlerSettings
from events_mcp.crawl.fetch import Fetcher
from events_mcp.crawl.runner import _process, close_stuck_runs, sync_sources
from events_mcp.db import ensure_crawler_grants
from events_mcp.ingest import touch_source
from events_mcp.metrics import Metrics

from .conftest import FIXTURES, make_source

ICS = (FIXTURES / "club.ics").read_text(encoding="utf-8")
ROBOTS_OK = "User-agent: *\nAllow: /\n"


class Web:
    """A tiny fake internet: path -> (content type, body)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[str, str]] = {
            "/robots.txt": ("text/plain", ROBOTS_OK),
            "/events.ics": ("text/calendar", ICS),
        }
        self.hits: list[str] = []

    def client(self) -> httpx.AsyncClient:
        def handler(req: httpx.Request) -> httpx.Response:
            self.hits.append(req.url.path)
            if req.url.path in self.routes:
                ctype, body = self.routes[req.url.path]
                return httpx.Response(200, headers={"content-type": ctype}, text=body)
            return httpx.Response(404)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture
def web() -> Web:
    return Web()


@pytest.fixture
def settings(schema: str) -> CrawlerSettings:
    return CrawlerSettings(database_url=schema, geocode=False)


async def run(web: Web, settings: CrawlerSettings, source_id: int, **kw: object) -> str:
    fetcher = Fetcher("test-agent", client=web.client())
    assert settings.database_url is not None
    # The ICS fixture is dated Nov 2026; the loop reads the real clock, so pin it far enough back.
    try:
        return await _process(
            settings.database_url.get_secret_value(),
            make_source(url="https://club.example/events.ics", extractor="ical", **kw),
            source_id,
            fetcher,
            None,
            settings,
            force=False,
            extractor=None,
        )
    finally:
        await fetcher.aclose()


async def setup_source(conn: AsyncConnection[DictRow]) -> int:
    src = make_source(url="https://club.example/events.ics", extractor="ical")
    return (await sync_sources(conn, [src]))["club"]


async def runs(conn: AsyncConnection[DictRow]) -> list[dict]:
    cur = await conn.execute("SELECT * FROM scrape_runs ORDER BY id")
    return list(await cur.fetchall())


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 16, 12, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("events_mcp.crawl.runner.datetime", FrozenDatetime)


async def test_first_run_extracts_and_second_run_only_touches(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = await setup_source(conn)
    assert await run(web, settings, sid) == "success"
    first = (await runs(conn))[0]
    assert (first["extractor"], first["items_new"], first["items_updated"], first["items_rejected"]) == (
        "ical",
        5,
        0,
        1,
    )
    assert first["llm_calls"] == 0
    cur = await conn.execute("SELECT count(*) AS n, count(venue_id) AS with_venue FROM events")
    assert await cur.fetchone() == {"n": 5, "with_venue": 1}

    await conn.execute("UPDATE events SET last_seen = last_seen - interval '2 days'")
    await conn.execute("UPDATE sources SET last_extracted_at = last_extracted_at - interval '2 days'")
    assert await run(web, settings, sid) == "success"
    second = (await runs(conn))[1]
    assert (second["items_touched"], second["items_new"], second["content_hash"]) == (5, 0, first["content_hash"])
    cur = await conn.execute("SELECT count(*) AS n FROM events WHERE last_seen > now() - interval '1 hour'")
    assert await cur.fetchone() == {"n": 5}


async def test_dropped_items_are_not_touched_and_go_stale(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = await setup_source(conn)
    await run(web, settings, sid)
    # The fair disappears from the calendar; everything else stays.
    web.routes["/events.ics"] = (
        "text/calendar",
        ICS.replace("SUMMARY:Weekend Fair", "SUMMARY:Weekend Fair 2").replace("UID:fair@", "UID:fair2@"),
    )
    assert await run(web, settings, sid) == "success"
    cur = await conn.execute("SELECT title FROM events WHERE last_seen < (SELECT last_extracted_at FROM sources)")
    assert [r["title"] for r in await cur.fetchall()] == ["Weekend Fair"]
    # Unchanged page now: only the five present rows move, the dropped one stays behind.
    assert await touch_source(conn, sid) == 5


async def test_pending_or_failed_runs_do_not_touch_or_mark(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = await setup_source(conn)
    web.routes["/events.ics"] = ("text/html", "<html><body>nothing structured</body></html>")
    assert await run(web, settings, sid) == "failed"
    cur = await conn.execute("SELECT last_extracted_hash, last_extracted_at FROM sources")
    assert await cur.fetchone() == {"last_extracted_hash": None, "last_extracted_at": None}
    assert "no structured data" in (await runs(conn))[0]["error"]


async def test_robots_block_is_its_own_status(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = await setup_source(conn)
    web.routes["/robots.txt"] = ("text/plain", "User-agent: *\nDisallow: /\n")
    assert await run(web, settings, sid) == "blocked_robots"
    assert "/events.ics" not in web.hits


async def test_ignore_robots_escape_hatch(conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings) -> None:
    sid = await setup_source(conn)
    web.routes["/robots.txt"] = ("text/plain", "User-agent: *\nDisallow: /\n")
    assert await run(web, settings, sid, politeness={"ignore_robots": True}) == "success"


async def test_challenge_page_is_a_failure(conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings) -> None:
    sid = await setup_source(conn)
    web.routes["/events.ics"] = ("text/html", "<html><title>Just a moment...</title></html>")
    assert await run(web, settings, sid) == "failed"
    assert "challenge" in (await runs(conn))[0]["error"]


async def test_removed_source_is_disabled_not_deleted(conn: AsyncConnection[DictRow]) -> None:
    await setup_source(conn)
    await sync_sources(conn, [make_source(name="other", url="https://other.example/x.ics")])
    cur = await conn.execute("SELECT name, enabled FROM sources ORDER BY name")
    assert [(r["name"], r["enabled"]) for r in await cur.fetchall()] == [("club", False), ("other", True)]
    # Coming back re-enables it.
    await sync_sources(conn, [make_source(url="https://club.example/events.ics")])
    cur = await conn.execute("SELECT enabled FROM sources WHERE name = 'club'")
    assert await cur.fetchone() == {"enabled": True}


async def test_stuck_runs_are_closed(conn: AsyncConnection[DictRow]) -> None:
    sid = await setup_source(conn)
    await conn.execute(
        "INSERT INTO scrape_runs (source_id, started_at) VALUES (%s, now() - interval '3 hours')", (sid,)
    )
    await conn.execute("INSERT INTO scrape_runs (source_id) VALUES (%s)", (sid,))
    await close_stuck_runs(conn)
    cur = await conn.execute("SELECT status FROM scrape_runs ORDER BY id")
    assert [r["status"] for r in await cur.fetchall()] == ["failed", "running"]


async def test_grants_are_a_noop_without_the_role(conn: AsyncConnection[DictRow]) -> None:
    cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'events_crawler'")
    if await cur.fetchone() is None:
        assert await ensure_crawler_grants(conn) is False


async def test_migration_0002_columns_and_vocabularies(conn: AsyncConnection[DictRow]) -> None:
    sid = await setup_source(conn)
    for kind in ("sale", "other"):
        await conn.execute(
            "INSERT INTO events (source_id, title, kind, dedupe_key) VALUES (%s, 't', %s, %s)", (sid, kind, kind)
        )
    with pytest.raises(CheckViolation):
        await conn.execute(
            "INSERT INTO events (source_id, title, kind, dedupe_key) VALUES (%s, 't', 'party', 'x')", (sid,)
        )
    with pytest.raises(CheckViolation):
        await conn.execute(
            "INSERT INTO events (source_id, title, kind, dedupe_key, status) "
            "VALUES (%s, 't', 'event', 'y', 'sleeping')",
            (sid,),
        )


async def test_metrics_cover_new_gauges(conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings) -> None:
    sid = await setup_source(conn)
    await run(web, settings, sid)
    m = Metrics()
    await m._refresh_crawl(conn)
    assert m.interval.labels(source="club")._value.get() == 86400
    assert m.last_status.labels(source="club", status="success")._value.get() == 1
    assert m.source_items.labels(source="club", lifecycle="upcoming")._value.get() > 0
    assert m.source_items.labels(source="club", lifecycle="ended")._value.get() == 0
