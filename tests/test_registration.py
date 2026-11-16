"""Two-phase events: the registration window before the event, its state and the queries on it."""

from zoneinfo import ZoneInfo

import pytest
from psycopg import AsyncConnection, errors
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
async def reg(conn: AsyncConnection[DictRow]) -> dict[str, int]:
    source = await upsert_source(conn, "Fabrik", "venue", "https://fabrik.example", "Hamburg")
    items = [
        ev(
            "gala",
            30,
            registration_opens_at=days(-5),
            registration_closes_at=days(3),
            tickets_url="https://t.example/g",
        ),
        ev("workshop", 40, registration_opens_at=days(5), registration_closes_at=days(20)),
        ev("late", 10, registration_opens_at=days(-20), registration_closes_at=days(-2)),
        ev("plain", 12),
        ev("linkonly", 7, tickets_url="https://t.example/l"),
        ev("cancelled", 15, registration_closes_at=days(2), status="cancelled"),
        ev("noclose", 9, registration_opens_at=days(-1)),
    ]
    return {i.dedupe_key: (await upsert_event(conn, source, i))[0] for i in items}


def names(rows: list[dict], ids: dict[str, int]) -> list[str]:
    by_id = {v: k for k, v in ids.items()}
    return [by_id[r["id"]] for r in rows]


async def test_state_is_derived_from_the_dates(pool: Pool, reg: dict[str, int]) -> None:
    out = await queries.search(pool, None, "", Filters(), TZ)
    states = {k: r.get("registration_state") for r in out["results"] for k, v in reg.items() if v == r["id"]}
    assert states == {
        "gala": "open",
        "workshop": "not_yet_open",
        "late": "closed",
        "plain": None,  # says nothing: not "free", not "open"
        "linkonly": "open",
        "cancelled": "open",
        "noclose": "open",  # no stated close: open until the event is over
    }


async def test_registration_filter(pool: Pool, reg: dict[str, int]) -> None:
    open_now = await queries.search(pool, None, "", Filters(registration="open"), TZ)
    assert set(names(open_now["results"], reg)) == {"gala", "linkonly", "cancelled", "noclose"}
    soon = await queries.upcoming(pool, Filters(registration="not_yet_open"), 60, TZ)
    assert names(soon, reg) == ["workshop"]
    with pytest.raises(ValueError, match="registration must be one of"):
        await queries.search(pool, None, "", Filters(registration="maybe"), TZ)


async def test_deadlines(pool: Pool, reg: dict[str, int]) -> None:
    closing = await queries.registration_deadlines(pool, Filters(), 7, "closing", TZ)
    assert names(closing, reg) == ["gala"]  # the cancelled one is left out, workshop closes later
    assert closing[0]["tickets_url"] == "https://t.example/g"
    wider = await queries.registration_deadlines(pool, Filters(), 30, "closing", TZ)
    assert names(wider, reg) == ["gala", "workshop"]  # soonest first
    opening = await queries.registration_deadlines(pool, Filters(), 7, "opening", TZ)
    assert names(opening, reg) == ["workshop"]


async def test_get_event_shows_the_registration_fields(pool: Pool, reg: dict[str, int]) -> None:
    got = await queries.get_event(pool, reg["gala"], TZ)
    assert got is not None
    assert got["registration_state"] == "open"
    assert got["tickets_url"] == "https://t.example/g"
    assert "registration_opens_at" in got and "registration_closes_at" in got
    plain = await queries.get_event(pool, reg["plain"], TZ)
    assert plain is not None and "registration_state" not in plain  # None values are left out


async def test_a_tier_without_registration_keeps_what_was_found(
    conn: AsyncConnection[DictRow], pool: Pool, reg: dict[str, int]
) -> None:
    source = (await (await conn.execute("SELECT id FROM sources")).fetchone())["id"]  # ty: ignore[not-subscriptable]
    again = ev("gala", 30)  # same row, a selector tier that does not read registration
    await upsert_event(conn, source, again)
    kept = await queries.get_event(pool, reg["gala"], TZ)
    assert kept is not None and kept["tickets_url"] == "https://t.example/g"
    assert kept["registration_state"] == "open"
    # A new window replaces the old pair as a unit, so the order check cannot be tripped by a mix.
    moved = ev("gala", 30, registration_opens_at=days(10), registration_closes_at=days(12))
    await upsert_event(conn, source, moved)
    after = await queries.get_event(pool, reg["gala"], TZ)
    assert after is not None and after["registration_state"] == "not_yet_open"
    # A new open date alone does not combine with the old close either.
    only_open = ev("gala", 30, registration_opens_at=days(20))
    await upsert_event(conn, source, only_open)
    row = await (
        await conn.execute(
            "SELECT registration_opens_at, registration_closes_at FROM events WHERE id = %s", (reg["gala"],)
        )
    ).fetchone()
    assert row is not None and row["registration_closes_at"] is None


async def test_database_rejects_a_reversed_window(conn: AsyncConnection[DictRow]) -> None:
    source = await upsert_source(conn, "S", "venue", "https://s.example", "Hamburg")
    with pytest.raises(errors.CheckViolation):
        await upsert_event(conn, source, ev("bad", 5, registration_opens_at=days(3), registration_closes_at=days(1)))
