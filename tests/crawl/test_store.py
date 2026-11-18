"""Crawler configuration in the database: round trips, defaults, history and the guard on deletion."""

from pathlib import Path

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp.crawl import store
from events_mcp.crawl.config import CrawlerSettings, Defaults, Source
from events_mcp.crawl.runner import _load_config
from events_mcp.ingest import Item, upsert_event
from tests.conftest import days

CLUB = {"name": "club", "type": "venue", "url": "https://club.example/events", "intent": "Concerts only."}


@pytest.fixture(autouse=True)
async def _clean(conn: AsyncConnection[DictRow]) -> None:
    await conn.execute("TRUNCATE source_config_history")
    await conn.execute("UPDATE crawler_defaults SET config = '{}'")


async def test_save_and_load_apply_the_defaults(conn: AsyncConnection[DictRow]) -> None:
    await store.save_defaults(conn, Defaults(city="Hamburg", interval="12h"), actor="t")
    await store.save_source(conn, Source.model_validate(CLUB), actor="t")
    sources, problems = await store.load_sources(conn)
    assert problems == {}
    (club,) = sources
    assert (club.city, club.interval, club.intent) == ("Hamburg", "12h", "Concerts only.")
    row = await (await conn.execute("SELECT city, scrape_interval, url FROM sources WHERE name = 'club'")).fetchone()
    assert row is not None and row["city"] == "Hamburg" and row["scrape_interval"].total_seconds() == 12 * 3600


async def test_changing_the_defaults_reaches_sources_that_do_not_override(conn: AsyncConnection[DictRow]) -> None:
    await store.save_source(conn, Source.model_validate(CLUB), actor="t")
    await store.save_source(
        conn, Source.model_validate({**CLUB, "name": "fast", "url": "https://f.example", "interval": "1h"}), actor="t"
    )
    await store.save_defaults(conn, Defaults(interval="6h"), actor="t")
    sources = {s.name: s for s in (await store.load_sources(conn))[0]}
    assert (sources["club"].interval, sources["fast"].interval) == ("6h", "1h")
    cur = await conn.execute("SELECT name, scrape_interval FROM sources ORDER BY name")
    assert {r["name"]: r["scrape_interval"].total_seconds() for r in await cur.fetchall()} == {
        "club": 6 * 3600,
        "fast": 3600,
    }


async def test_every_change_is_logged_with_before_and_after(conn: AsyncConnection[DictRow]) -> None:
    await store.save_source(conn, Source.model_validate(CLUB), actor="alice")
    await store.save_source(conn, Source.model_validate({**CLUB, "intent": "Jazz."}), actor="bob")
    await store.set_enabled(conn, "club", False, actor="bob")
    cur = await conn.execute("SELECT action, actor, before, after FROM source_config_history ORDER BY id")
    rows = await cur.fetchall()
    assert [(r["action"], r["actor"]) for r in rows] == [("create", "alice"), ("update", "bob"), ("disable", "bob")]
    assert rows[1]["before"]["intent"] == "Concerts only." and rows[1]["after"]["intent"] == "Jazz."


async def test_disabled_sources_are_not_crawled(conn: AsyncConnection[DictRow]) -> None:
    await store.save_source(conn, Source.model_validate(CLUB), actor="t")
    await store.set_enabled(conn, "club", False, actor="t")
    assert (await store.load_sources(conn))[0] == []
    assert [s.name for s in (await store.load_sources(conn, include_disabled=True))[0]] == ["club"]
    with pytest.raises(store.UnknownSourceError):
        await store.set_enabled(conn, "nope", True, actor="t")


async def test_one_broken_row_does_not_stop_the_others(conn: AsyncConnection[DictRow]) -> None:
    await store.save_source(conn, Source.model_validate(CLUB), actor="t")
    await store.save_source(conn, Source.model_validate({**CLUB, "name": "bad", "url": "https://b.example"}), actor="t")
    await conn.execute("""UPDATE sources SET config = '{"name":"bad","type":"venue"}' WHERE name = 'bad'""")
    sources, problems = await store.load_sources(conn)
    assert [s.name for s in sources] == ["club"]
    assert "url is required" in problems["bad"]


async def test_a_source_with_items_cannot_be_deleted(conn: AsyncConnection[DictRow]) -> None:
    sid = await store.save_source(conn, Source.model_validate(CLUB), actor="t")
    await store.save_source(
        conn, Source.model_validate({**CLUB, "name": "empty", "url": "https://e.example"}), actor="t"
    )
    await upsert_event(conn, sid, Item(title="Gig", dedupe_key="g", kind="event", starts_at=days(3)))
    with pytest.raises(store.ConfigError, match="disable it instead"):
        await store.delete_source(conn, "club", actor="t")
    await store.delete_source(conn, "empty", actor="t")
    assert [s.name for s in (await store.load_sources(conn, include_disabled=True))[0]] == ["club"]
    with pytest.raises(store.UnknownSourceError):
        await store.delete_source(conn, "empty", actor="t")


async def test_run_requests_are_taken_once(conn: AsyncConnection[DictRow]) -> None:
    await store.save_source(conn, Source.model_validate(CLUB), actor="t")
    await store.request_run(conn, "club", "full")
    assert await store.take_run_requests(conn) == {"club": "full"}
    assert await store.take_run_requests(conn) == {}
    await store.set_enabled(conn, "club", False, actor="t")
    with pytest.raises(store.ConfigError):
        await store.request_run(conn, "club", "due")


async def test_import_a_sources_file_then_the_crawler_reads_it(conn: AsyncConnection[DictRow], tmp_path: Path) -> None:
    f = tmp_path / "sources.yaml"
    f.write_text(
        "defaults: {city: Hamburg, interval: 24h}\n"
        "sources:\n"
        "  - {name: club, type: venue, url: 'https://club.example/events'}\n"
        "  - {name: chan, type: youtube, channel_id: UC123, interval: 6h}\n",
        encoding="utf-8",
    )
    assert await store.import_file(conn, f, actor="import") == 2
    settings = CrawlerSettings.model_construct(sources_file=None, database_url=None)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        await _load_config(settings)
    sources, problems = await store.load_sources(conn)
    assert problems == {} and {s.name: (s.city, s.interval) for s in sources} == {
        "club": ("Hamburg", "24h"),
        "chan": ("Hamburg", "6h"),
    }
    assert {s.name: s.primary_url for s in sources}["chan"].endswith("playlist_id=UULF123")
