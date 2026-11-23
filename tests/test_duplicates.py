"""Rows an upstream lists twice collapse in every listing; an empty dated search explains itself."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp import queries
from events_mcp.db import Pool
from events_mcp.ingest import Item, upsert_event, upsert_source
from events_mcp.queries import Filters, parse_when

from .conftest import days

TZ = ZoneInfo("Europe/Berlin")
VENUE = {"name": "Comedy Club", "city": "Hamburg"}


def item(key: str, title: str, start: datetime | None, **kw: Any) -> Item:
    return Item(title=title, dedupe_key=key, kind="event", starts_at=start, city="Hamburg", venue=VENUE, **kw)


@pytest.fixture
async def twins(conn: AsyncConnection[DictRow]) -> dict[str, int]:
    """GET UP twice at the same start (no link / link), once with a different link, and a lone show."""
    src = await upsert_source(conn, "Rausgegangen", "venue", "https://rg.example", "Hamburg")
    start = days(3).replace(minute=0, second=0, microsecond=0)
    rows = {
        "nolink": item("t:abc", "GET UP Comedy", start, summary="Weekly open mic comedy."),
        "link": item("u:get-up-205", "get-up  comedy!", start, url="https://rg.example/get-up-205"),
        "tickets": item(
            "u:get-up-223",
            "GET UP Comedy",
            start,
            url="https://rg.example/get-up-223",
            tickets_url="https://t.example/1",
        ),
        "other-day": item("u:get-up-300", "GET UP Comedy", days(10), url="https://rg.example/get-up-300"),
        "lone": item("u:quiz", "Quiz Night", days(4), url="https://rg.example/quiz"),
    }
    ids = {}
    for name, it in rows.items():
        ids[name], _ = await upsert_event(conn, src, it)
    return ids


async def test_upcoming_collapses_to_the_row_with_tickets(pool: Pool, twins: dict[str, int]) -> None:
    rows = await queries.upcoming(pool, Filters(), 30, TZ)
    titles = [(r["id"], r["title"]) for r in rows]
    assert len(rows) == 3  # the 3-fold show, the other-day show, the quiz
    first = rows[0]
    assert first["id"] == twins["tickets"]  # tickets_url beats url beats no url
    assert first["also_listed_as"] == sorted([twins["nolink"], twins["link"]])
    assert "dup_ids" not in first and "dup_rank" not in first
    assert "also_listed_as" not in next(r for r in rows if r["id"] == twins["lone"])
    assert twins["other-day"] in {i for i, _ in titles}  # a different start is a different event


async def test_limit_counts_collapsed_rows(pool: Pool, twins: dict[str, int]) -> None:
    rows = await queries.upcoming(pool, Filters(), 30, TZ, limit=2)
    assert [r["id"] for r in rows] == [twins["tickets"], twins["lone"]]


async def test_search_collapses_and_reports_the_others(pool: Pool, twins: dict[str, int]) -> None:
    out = await queries.search(pool, None, "get up comedy", Filters(), TZ)
    assert out["count"] == 2
    first = out["results"][0]
    assert first["id"] == twins["tickets"] and len(first["also_listed_as"]) == 2
    listed = await queries.search(pool, None, "", Filters(), TZ)
    assert [r["id"] for r in listed["results"]].count(twins["nolink"]) == 0


async def test_ending_soon_and_registration_deadlines_collapse(conn: AsyncConnection[DictRow], pool: Pool) -> None:
    src = await upsert_source(conn, "S", "venue", "https://s.example", "Hamburg")
    kw = {"ends_at": days(5), "registration_closes_at": days(2)}
    start = days(1)
    for key, url in (("t:x", None), ("u:x", "https://s.example/x")):
        await upsert_event(conn, src, item(key, "Workshop", start, url=url, **kw))
    ending = await queries.ending_soon(pool, Filters(), 10, TZ)
    closing = await queries.registration_deadlines(pool, Filters(), 10, "closing", TZ)
    for rows in (ending, closing):
        assert len(rows) == 1 and rows[0]["url"] == "https://s.example/x" and len(rows[0]["also_listed_as"]) == 1


async def test_same_title_at_another_venue_is_not_a_duplicate(conn: AsyncConnection[DictRow], pool: Pool) -> None:
    src = await upsert_source(conn, "S", "venue", "https://s.example", "Hamburg")
    start = days(2)
    await upsert_event(conn, src, item("u:a", "Quiz", start, url="https://s.example/a"))
    other = Item(
        title="Quiz",
        dedupe_key="u:b",
        kind="event",
        starts_at=start,
        city="Hamburg",
        url="https://s.example/b",
        venue={"name": "Elsewhere", "city": "Hamburg"},
    )
    await upsert_event(conn, src, other)
    assert len(await queries.upcoming(pool, Filters(), 10, TZ)) == 2


def test_compact_row_keeps_the_agent_fields() -> None:
    full = {"id": 1, "title": "t", "starts_at": "s", "venue": "v", "summary": "long", "tickets_url": "https://t"}
    assert queries.compact_row(full) == {"id": 1, "title": "t", "starts_at": "s", "venue": "v", "url": "https://t"}
    assert queries.compacted([full], False) == [full]


async def test_empty_dated_search_names_the_closest_match(pool: Pool, twins: dict[str, int]) -> None:
    window = Filters(date_from=parse_when("2026-01-01", TZ), date_to=parse_when("2026-01-07", TZ, end_of_day=True))
    out = await queries.search(pool, None, "get up", window, TZ)
    assert out["count"] == 0
    assert "Nothing matches within 2026-01-01 to 2026-01-07" in out["note"]
    assert "'GET UP Comedy'" in out["note"] and "2 match(es)" in out["note"]  # collapsed: 3-fold show + other day
    assert "recurring series" in out["note"].lower()
    closest = out["closest_without_date_filter"]
    assert closest["count"] == 2 and closest["nearest"]["id"] == twins["tickets"]
    assert "No undated items were excluded" in out["note"]


async def test_empty_dated_search_says_when_undated_items_were_excluded(
    conn: AsyncConnection[DictRow], pool: Pool
) -> None:
    src = await upsert_source(conn, "S", "museum", "https://m.example", "Hamburg")
    await upsert_event(
        conn, src, Item(title="Permanent collection", dedupe_key="u:perm", kind="exhibition", city="Hamburg")
    )
    window = Filters(date_from=days(1), date_to=days(2))
    out = await queries.search(pool, None, "permanent collection", window, TZ)
    assert out["count"] == 0 and "1 undated match(es) were excluded" in out["note"]
    assert out["closest_without_date_filter"] == {"count": 1, "undated_excluded_by_window": 1}


async def test_no_note_without_a_date_filter_or_with_results(pool: Pool, twins: dict[str, int]) -> None:
    assert "note" not in await queries.search(pool, None, "nothing like this xyz", Filters(), TZ)
    hit = await queries.search(pool, None, "get up", Filters(date_from=days(0), date_to=days(20)), TZ)
    assert hit["count"] == 2 and "note" not in hit


async def test_cleanup_removes_linkless_twins_and_spares_tracked_ones(
    conn: AsyncConnection[DictRow], pool: Pool, twins: dict[str, int]
) -> None:
    from events_mcp.ingest import remove_linkless_twins  # noqa: PLC0415

    src = await upsert_source(conn, "Rausgegangen", "venue", "https://rg.example", "Hamburg")
    lonely, _ = await upsert_event(conn, src, item("t:lonely", "Only Linkless", days(6)))
    assert await remove_linkless_twins(conn, apply=False) == (1, 0)  # dry run: the "nolink" twin only
    assert len(await (await conn.execute("SELECT id FROM events")).fetchall()) == 6
    await queries.track_event(pool, twins["nolink"], "raised")
    assert await remove_linkless_twins(conn, apply=True) == (0, 1)
    await queries.untrack_event(pool, twins["nolink"])
    assert await remove_linkless_twins(conn, apply=True) == (1, 0)
    left = {r["id"] for r in await (await conn.execute("SELECT id FROM events")).fetchall()}
    assert twins["nolink"] not in left and lonely in left and twins["link"] in left
