from zoneinfo import ZoneInfo

import pytest
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation
from psycopg.rows import DictRow

from events_mcp.db import migrate, migration_files
from events_mcp.ingest import Item, finish_run, start_run, upsert_event, upsert_source

from .conftest import FakeEmbedder, days

TZ = ZoneInfo("Europe/Berlin")


def test_migrations_are_numbered() -> None:
    files = migration_files()
    assert files[0][0] == 1
    assert [v for v, _, _ in files] == sorted({v for v, _, _ in files})


async def test_migrate_is_idempotent(conn: AsyncConnection[DictRow]) -> None:
    assert await migrate(conn) == []


async def test_upsert_twice_does_not_duplicate(conn: AsyncConnection[DictRow]) -> None:
    src = await upsert_source(conn, "Fabrik", "venue", "https://fabrik.example")
    item = Item(title="Punk Night", dedupe_key="punk", kind="event", starts_at=days(2), city="Hamburg")
    first_id, inserted = await upsert_event(conn, src, item)
    second_id, inserted_again = await upsert_event(conn, src, item)
    assert (inserted, inserted_again) == (True, False)
    assert first_id == second_id
    cur = await conn.execute("SELECT count(*) AS n, min(first_seen) < max(last_seen) AS moved FROM events")
    row = await cur.fetchone()
    assert row == {"n": 1, "moved": True}


async def test_same_dedupe_key_in_other_source_is_separate(conn: AsyncConnection[DictRow]) -> None:
    a = await upsert_source(conn, "A", "venue", "https://a.example")
    b = await upsert_source(conn, "B", "venue", "https://b.example")
    item = Item(title="Gig", dedupe_key="k", kind="event")
    assert (await upsert_event(conn, a, item))[0] != (await upsert_event(conn, b, item))[0]


async def test_embedding_kept_when_text_unchanged_and_cleared_when_changed(conn: AsyncConnection[DictRow]) -> None:
    src = await upsert_source(conn, "A", "venue", "https://a.example")
    item = Item(title="Gig", dedupe_key="k", kind="event", summary="loud")
    await upsert_event(conn, src, item, FakeEmbedder.vector("loud"), "m")
    await upsert_event(conn, src, item)  # re-seen without a vector: keep it
    cur = await conn.execute("SELECT embedding IS NOT NULL AS has, embedding_model FROM events")
    assert await cur.fetchone() == {"has": True, "embedding_model": "m"}

    item.summary = "now quiet"
    await upsert_event(conn, src, item)  # text changed: stale vector must go
    cur = await conn.execute("SELECT embedding IS NULL AS cleared, embedding_model FROM events")
    assert await cur.fetchone() == {"cleared": True, "embedding_model": None}


async def test_raw_text_is_capped(conn: AsyncConnection[DictRow]) -> None:
    src = await upsert_source(conn, "A", "venue", "https://a.example")
    await upsert_event(conn, src, Item(title="T", dedupe_key="k", kind="event", raw_text="x" * 50000))
    cur = await conn.execute("SELECT char_length(raw_text) AS n FROM events")
    assert await cur.fetchone() == {"n": 20000}


async def test_end_before_start_is_rejected(conn: AsyncConnection[DictRow]) -> None:
    src = await upsert_source(conn, "A", "venue", "https://a.example")
    with pytest.raises(CheckViolation):
        await upsert_event(conn, src, Item(title="T", dedupe_key="k", kind="event", starts_at=days(2), ends_at=days(1)))


async def test_lifecycle_views(conn: AsyncConnection[DictRow], seeded: dict[str, int]) -> None:
    cur = await conn.execute("SELECT dedupe_key, lifecycle FROM event_lifecycle ORDER BY dedupe_key")
    lifecycle = {r["dedupe_key"]: r["lifecycle"] for r in await cur.fetchall()}
    assert lifecycle == {
        "ev-food-berlin": "upcoming",
        "ev-jazz-past": "ended",
        "ev-punk": "upcoming",
        "ex-north": "ongoing",
        "ex-romantik": "ongoing",
        "yt-abc": "published",
    }
    cur = await conn.execute("SELECT dedupe_key FROM upcoming ORDER BY dedupe_key")
    # Cancelled street food is not upcoming; the video is not a calendar item.
    assert [r["dedupe_key"] for r in await cur.fetchall()] == ["ev-punk", "ex-north", "ex-romantik"]
    cur = await conn.execute("SELECT dedupe_key FROM ended")
    assert [r["dedupe_key"] for r in await cur.fetchall()] == ["ev-jazz-past"]

    await conn.execute("UPDATE events SET last_seen = now() - interval '10 days' WHERE dedupe_key = 'ex-north'")
    cur = await conn.execute("SELECT dedupe_key FROM stale")
    assert [r["dedupe_key"] for r in await cur.fetchall()] == ["ex-north"]


async def test_scrape_run_bookkeeping(conn: AsyncConnection[DictRow]) -> None:
    src = await upsert_source(conn, "A", "venue", "https://a.example")
    run = await start_run(conn, src)
    await finish_run(conn, run, "success", 7)
    cur = await conn.execute(
        "SELECT r.status, r.items_seen, s.last_run = r.finished_at AS stamped FROM scrape_runs r JOIN sources s ON true"
    )
    assert await cur.fetchone() == {"status": "success", "items_seen": 7, "stamped": True}
