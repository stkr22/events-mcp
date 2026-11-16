"""HTML pages without structured data: clean, hash, snapshot, LLM, store."""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp.crawl.config import CrawlerSettings
from events_mcp.crawl.fetch import Fetcher
from events_mcp.crawl.runner import Services, _process, sync_sources
from events_mcp.crawl.snapshots import SnapshotStore

from .conftest import make_source
from .fakes import FakeChat, FakeS3
from .test_pipeline_db import Web

HTML = """<!doctype html><html><head><title>Programm</title></head><body><nav>Menü Start</nav><main>
<h1>Programm November</h1>
<h2>Konzert Nordlicht</h2><p>Sa. 21.11. um 20 Uhr spielt die Band Nordlicht in der großen Halle. Eintritt frei.</p>
<h2>Ausstellung Salz und Stein</h2><p>Noch bis 14.02.2027 im Foyer, täglich von 10 bis 18 Uhr geöffnet.</p>
</main><footer>Impressum</footer></body></html>"""
ANSWER = {
    "items": [
        {"title": "Konzert Nordlicht", "kind": "event", "starts_at": "2026-11-21T20:00", "raw_text": "spielt die Band"},
        {"title": "Salz und Stein", "kind": "exhibition", "starts_at": "2026-11-01", "ends_at": "2027-02-14"},
        {"title": "Erfundenes Festival der Phantasie", "starts_at": "2026-12-24"},
    ]
}


@pytest.fixture
def web() -> Web:
    w = Web()
    w.routes["/programm"] = ("text/html", HTML)
    return w


@pytest.fixture
def settings(schema: str) -> CrawlerSettings:
    return CrawlerSettings(database_url=schema, geocode=False)


def source(**kw: object):
    return make_source(url="https://venue.example/programm", type="venue", **kw)


async def go(
    web: Web, settings: CrawlerSettings, sid: int, chain: list[FakeChat], s3: FakeS3 | None = None, **kw: object
):
    fetcher = Fetcher("test-agent", client=web.client())
    store = SnapshotStore(CrawlerSettings(database_url="x", s3_bucket="b"), client=s3) if s3 is not None else None
    svc = Services(settings, fetcher, None, None, store, chain)
    try:
        assert settings.database_url is not None
        return await _process(
            settings.database_url.get_secret_value(), source(**kw), sid, svc, force=False, extractor=None
        )
    finally:
        await fetcher.aclose()


async def setup(conn: AsyncConnection[DictRow]) -> int:
    return (await sync_sources(conn, [source()]))["club"]


async def last_run(conn: AsyncConnection[DictRow]) -> dict:
    cur = await conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    assert row is not None
    return row


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 16, 12, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("events_mcp.crawl.runner.datetime", Frozen)


async def test_llm_path_stores_items_rejects_hallucinations_and_snapshots(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid, s3 = await setup(conn), FakeS3()
    mac = FakeChat("mac", [ANSWER])
    assert await go(web, settings, sid, [mac], s3) == "success"
    run = await last_run(conn)
    assert (run["extractor"], run["llm_provider"], run["llm_calls"], run["items_new"], run["items_rejected"]) == (
        "llm",
        "mac",
        1,
        2,
        1,
    )
    assert run["snapshot_key"] in s3.objects and run["snapshot_key"].startswith("snapshots/club/2026-11-16/")
    cur = await conn.execute("SELECT title, kind FROM events ORDER BY title")
    assert [(r["title"], r["kind"]) for r in await cur.fetchall()] == [
        ("Konzert Nordlicht", "event"),
        ("Salz und Stein", "exhibition"),
    ]
    store = SnapshotStore(CrawlerSettings(database_url="x", s3_bucket="b"), client=s3)
    assert "Konzert Nordlicht" in await store.get(run["snapshot_key"])


async def test_unchanged_page_makes_zero_llm_calls_and_no_new_snapshot(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid, s3 = await setup(conn), FakeS3()
    await go(web, settings, sid, [FakeChat("mac", [ANSWER])], s3)
    second = FakeChat("mac", [])
    assert await go(web, settings, sid, [second], s3) == "success"
    run = await last_run(conn)
    assert (run["llm_calls"], run["items_touched"], run["snapshot_key"]) == (0, 2, None)
    assert second.calls == [] and len(s3.objects) == 1


async def test_changed_page_extracts_again_and_snapshots_again(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid, s3 = await setup(conn), FakeS3()
    await go(web, settings, sid, [FakeChat("mac", [ANSWER])], s3)
    web.routes["/programm"] = ("text/html", HTML.replace("Eintritt frei", "Eintritt 5 Euro"))
    await go(web, settings, sid, [FakeChat("mac", [ANSWER])], s3)
    assert len(s3.objects) == 2


async def test_no_provider_is_pending_and_leaves_state_alone(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = await setup(conn)
    chain = [FakeChat("mac", [], up=False), FakeChat("gemini", [], up=False)]
    assert await go(web, settings, sid, chain) == "pending_extraction"
    cur = await conn.execute("SELECT last_extracted_hash, last_extracted_at FROM sources")
    assert await cur.fetchone() == {"last_extracted_hash": None, "last_extracted_at": None}
    # The Mac wakes up: the next run extracts, because the hash was never marked as done.
    assert await go(web, settings, sid, [FakeChat("mac", [ANSWER])]) == "success"


async def test_gemini_used_when_mac_sleeps(conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings) -> None:
    sid = await setup(conn)
    chain = [FakeChat("mac", [], up=False), FakeChat("gemini", [ANSWER])]
    assert await go(web, settings, sid, chain) == "success"
    assert (await last_run(conn))["llm_provider"] == "gemini"


async def test_dropped_item_goes_stale_after_llm_change(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = await setup(conn)
    await go(web, settings, sid, [FakeChat("mac", [ANSWER])])
    web.routes["/programm"] = ("text/html", HTML.replace("Konzert Nordlicht", "Konzert Nordlicht (ausverkauft)"))
    await go(web, settings, sid, [FakeChat("mac", [{"items": [ANSWER["items"][1]]}])])
    cur = await conn.execute("SELECT title FROM events WHERE last_seen < (SELECT last_extracted_at FROM sources)")
    assert [r["title"] for r in await cur.fetchall()] == ["Konzert Nordlicht"]


async def test_snapshot_prune_drops_old_objects() -> None:
    s3 = FakeS3()
    store = SnapshotStore(CrawlerSettings(database_url="x", s3_bucket="b"), client=s3)
    await store.put("snapshots/a/2026-01-01/old.md.zst", "old")
    await store.put("snapshots/a/2026-11-16/new.md.zst", "new")
    body, ts = s3.objects["snapshots/a/2026-01-01/old.md.zst"]
    s3.objects["snapshots/a/2026-01-01/old.md.zst"] = (body, ts - timedelta(days=100))
    assert await store.prune() == 1
    assert list(s3.objects) == ["snapshots/a/2026-11-16/new.md.zst"]
    assert await store.latest("a") == "snapshots/a/2026-11-16/new.md.zst"
