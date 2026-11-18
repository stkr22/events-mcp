"""What the agent told the user about an event: tracking, change detection, hiding and retention."""

from zoneinfo import ZoneInfo

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp import queries
from events_mcp.db import Pool
from events_mcp.ingest import Item, upsert_event, upsert_source
from events_mcp.queries import Filters

from .conftest import days

TZ = ZoneInfo("Europe/Berlin")


def ev(key: str, start: float, **kw: object) -> Item:
    return Item(title=key.title(), dedupe_key=key, kind="event", starts_at=days(start), city="Hamburg", **kw)  # ty: ignore[invalid-argument-type]


@pytest.fixture
async def src(conn: AsyncConnection[DictRow]) -> int:
    return await upsert_source(conn, "Fabrik", "venue", "https://fabrik.example", "Hamburg")


@pytest.fixture
async def ids(conn: AsyncConnection[DictRow], src: int) -> dict[str, int]:
    items = [ev("gala", 10), ev("film", 5), ev("talk", 8), ev("ancient", -101, ends_at=days(-100))]
    return {i.dedupe_key: (await upsert_event(conn, src, i))[0] for i in items}


async def test_track_and_list(pool: Pool, ids: dict[str, int]) -> None:
    await queries.track_event(pool, ids["gala"], "raised")
    await queries.track_event(pool, ids["film"], "planned", "task-17")
    rows = await queries.my_events(pool, None, False, TZ)
    assert {r["title"]: (r["tracked_status"], r.get("task_ref")) for r in rows} == {
        "Gala": ("raised", None),
        "Film": ("planned", "task-17"),
    }
    assert [r["title"] for r in await queries.my_events(pool, "planned", False, TZ)] == ["Film"]
    assert all("changes" not in r for r in rows)  # nothing differs from what the user was told


async def test_every_listing_and_get_event_show_the_status(pool: Pool, ids: dict[str, int]) -> None:
    await queries.track_event(pool, ids["film"], "planned", "task-17")
    out = await queries.search(pool, None, "", Filters(), TZ)
    by_title = {r["title"]: r.get("tracked_status") for r in out["results"]}
    assert by_title["Film"] == "planned" and by_title["Gala"] is None
    got = await queries.get_event(pool, ids["film"], TZ)
    assert got is not None and got["tracked_status"] == "planned" and got["task_ref"] == "task-17"


async def test_hide_handled_leaves_out_dismissed_and_planned(pool: Pool, ids: dict[str, int]) -> None:
    await queries.track_event(pool, ids["film"], "planned", "t1")
    await queries.track_event(pool, ids["talk"], "dismissed")
    await queries.track_event(pool, ids["gala"], "raised")  # told, still a candidate
    shown = await queries.upcoming(pool, Filters(hide_handled=True), 30, TZ)
    assert [r["title"] for r in shown] == ["Gala"]
    everything = await queries.upcoming(pool, Filters(), 30, TZ)
    assert {r["title"] for r in everything} == {"Gala", "Film", "Talk"}  # off by default: a title search still finds it
    searched = await queries.search(pool, None, "film", Filters(), TZ)
    assert [r["title"] for r in searched["results"]] == ["Film"]


async def test_changes_since_the_user_was_told(
    conn: AsyncConnection[DictRow], pool: Pool, src: int, ids: dict[str, int]
) -> None:
    await queries.track_event(pool, ids["gala"], "raised")
    await queries.track_event(pool, ids["talk"], "raised")
    assert await queries.my_events(pool, None, True, TZ) == []
    # The crawler learns a new date, a ticket window and a cancellation.
    await upsert_event(conn, src, ev("gala", 10, registration_opens_at=days(1), registration_closes_at=days(4)))
    await upsert_event(conn, src, ev("talk", 8, status="cancelled"))
    changed = {r["title"]: r["changes"] for r in await queries.my_events(pool, None, True, TZ)}
    assert set(changed) == {"Gala", "Talk"}
    assert set(changed["Gala"]) == {"registration_opens_at", "registration_closes_at"}
    assert changed["Gala"]["registration_opens_at"]["told"] is None
    assert changed["Talk"]["status"] == {"told": None, "now": "cancelled"}
    # Telling the user again refreshes the snapshot.
    await queries.track_event(pool, ids["gala"], "raised")
    assert [r["title"] for r in await queries.my_events(pool, None, True, TZ)] == ["Talk"]


async def test_tracking_again_replaces_the_status_and_drops_the_task(pool: Pool, ids: dict[str, int]) -> None:
    await queries.track_event(pool, ids["film"], "planned", "t1")
    again = await queries.track_event(pool, ids["film"], "raised")  # the task is gone
    assert again == {"event_id": ids["film"], "status": "raised"}
    (row,) = await queries.my_events(pool, None, False, TZ)
    assert row["tracked_status"] == "raised" and "task_ref" not in row


async def test_input_is_checked(pool: Pool, ids: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="status must be one of"):
        await queries.track_event(pool, ids["film"], "booked")
    with pytest.raises(ValueError, match="only goes with status=planned"):
        await queries.track_event(pool, ids["film"], "dismissed", "t1")
    with pytest.raises(ValueError, match="at most 200"):
        await queries.track_event(pool, ids["film"], "planned", "x" * 201)
    with pytest.raises(ValueError, match="no item with id"):
        await queries.track_event(pool, 999_999, "raised")
    with pytest.raises(ValueError, match="status must be one of"):
        await queries.my_events(pool, "attended", False, TZ)


async def test_untrack(pool: Pool, ids: dict[str, int]) -> None:
    await queries.track_event(pool, ids["film"], "raised")
    assert await queries.untrack_event(pool, ids["film"]) is True
    assert await queries.untrack_event(pool, ids["film"]) is False
    assert await queries.my_events(pool, None, False, TZ) == []


async def test_old_events_are_forgotten(conn: AsyncConnection[DictRow], pool: Pool, ids: dict[str, int]) -> None:
    await queries.track_event(pool, ids["ancient"], "planned", "t1")
    assert await queries.my_events(pool, None, False, TZ) == []  # hidden at once
    row = await (await conn.execute("SELECT count(*) AS n FROM event_user_state")).fetchone()
    assert row is not None and row["n"] == 1
    await queries.track_event(pool, ids["film"], "raised")  # the next write purges it
    row = await (await conn.execute("SELECT count(*) AS n FROM event_user_state")).fetchone()
    assert row is not None and row["n"] == 1


async def test_deleting_an_event_removes_its_tracking(
    conn: AsyncConnection[DictRow], pool: Pool, ids: dict[str, int]
) -> None:
    await queries.track_event(pool, ids["film"], "raised")
    await conn.execute("DELETE FROM events WHERE id = %s", (ids["film"],))
    assert await queries.my_events(pool, None, False, TZ) == []
