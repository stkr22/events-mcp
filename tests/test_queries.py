from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp import queries
from events_mcp.db import Pool
from events_mcp.ingest import backfill_embeddings
from events_mcp.queries import Filters, parse_when

from .conftest import FakeEmbedder, days

TZ = ZoneInfo("Europe/Berlin")


def keys(out: dict, seeded: dict[str, int]) -> list[str]:
    by_id = {v: k for k, v in seeded.items()}
    return [by_id[r["id"]] for r in out["results"]]


def test_parse_when_bare_dates_are_local_days() -> None:
    assert parse_when("2026-11-21", TZ) == datetime(2026, 11, 21, tzinfo=TZ)
    assert parse_when("2026-11-21", TZ, end_of_day=True) == datetime.combine(
        datetime(2026, 11, 21).date(), time.max, tzinfo=TZ
    )
    parsed = parse_when("2026-11-21T10:00:00+00:00", TZ)
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parse_when(None, TZ) is None
    with pytest.raises(ValueError, match="ISO"):
        parse_when("next friday", TZ)


async def test_keyword_german_stem(pool: Pool, seeded: dict[str, int]) -> None:
    # "Ausstellung" (singular) must hit "Ausstellungen"; "Gemälde" hits "Landschaftsgemälde" only via semantic,
    # so stick to a real stem pair.
    out = await queries.search(pool, None, "Ausstellung Malerei", Filters(), TZ)
    assert keys(out, seeded)[0] == "ex-romantik"
    assert out["semantic_search"] == "not_configured"


async def test_keyword_english_stem(pool: Pool, seeded: dict[str, int]) -> None:
    out = await queries.search(pool, None, "painting", Filters(), TZ)
    assert keys(out, seeded) == ["ex-north"]


async def test_keyword_loose_match_ranks_full_match_first(pool: Pool, seeded: dict[str, int]) -> None:
    out = await queries.search(pool, None, "nordic paintings concert", Filters(), TZ)
    assert keys(out, seeded)[0] == "ex-north"


async def test_filters_city_kind_and_ended(pool: Pool, seeded: dict[str, int]) -> None:
    out = await queries.search(pool, None, "music", Filters(city="hamburg"), TZ)
    assert keys(out, seeded) == ["ev-punk"]  # the past jazz concert is ended, Berlin food market elsewhere
    out = await queries.search(pool, None, "music", Filters(city="Hamburg", include_ended=True), TZ)
    assert set(keys(out, seeded)) == {"ev-punk", "ev-jazz-past"}
    out = await queries.search(pool, None, "", Filters(kind="video"), TZ)
    assert keys(out, seeded) == ["yt-abc"]


async def test_date_range_overlap(pool: Pool, seeded: dict[str, int]) -> None:
    # A window 1.5..3 days out: the punk night, plus both exhibitions that are running then.
    out = await queries.search(pool, None, "", Filters(date_from=days(1.5), date_to=days(3)), TZ)
    assert set(keys(out, seeded)) == {"ev-punk", "ex-north", "ex-romantik"}
    # 20..30 days out: only the long exhibition.
    out = await queries.search(pool, None, "", Filters(date_from=days(20), date_to=days(30)), TZ)
    assert keys(out, seeded) == ["ex-north"]
    # Videos published in the last week.
    out = await queries.search(pool, None, "", Filters(kind="video", date_from=days(-7)), TZ)
    assert keys(out, seeded) == ["yt-abc"]


async def test_source_and_new_since(pool: Pool, conn: AsyncConnection[DictRow], seeded: dict[str, int]) -> None:
    out = await queries.search(pool, None, "", Filters(source="space"), TZ)
    assert keys(out, seeded) == ["yt-abc"]
    out = await queries.search(pool, None, "", Filters(source="kunsthalle"), TZ)
    assert set(keys(out, seeded)) == {"ex-north", "ex-romantik"}
    await conn.execute("UPDATE events SET first_seen = now() - interval '30 days' WHERE dedupe_key <> 'ex-north'")
    out = await queries.search(pool, None, "", Filters(new_since=days(-7)), TZ)
    assert keys(out, seeded) == ["ex-north"]


async def test_semantic_finds_fuzzy_query(pool: Pool, seeded: dict[str, int]) -> None:
    embedder = FakeEmbedder()
    # No keyword in common with the romantic painting show except via concepts quiet+art.
    out = await queries.search(pool, embedder, "something quiet and arty for a rainy day", Filters(), TZ)
    assert out["semantic_search"] == "used"
    assert keys(out, seeded)[0] == "ex-romantik"
    assert "semantic" in out["results"][0]["matched_by"]


async def test_semantic_degrades_to_keyword(pool: Pool, seeded: dict[str, int]) -> None:
    out = await queries.search(pool, FakeEmbedder(up=False), "painting", Filters(), TZ)
    assert out["semantic_search"] == "unavailable"
    assert "note" in out
    assert keys(out, seeded) == ["ex-north"]
    assert out["results"][0]["matched_by"] == "keyword"


async def test_semantic_ignores_other_models(pool: Pool, conn: AsyncConnection[DictRow], seeded) -> None:
    await conn.execute("UPDATE events SET embedding_model = 'old-model'")
    out = await queries.search(pool, FakeEmbedder(), "quiet art", Filters(), TZ)
    assert all(r["matched_by"] == "keyword" for r in out["results"])


async def test_upcoming_and_ending_soon(pool: Pool, seeded: dict[str, int]) -> None:
    rows = await queries.upcoming(pool, Filters(city="Hamburg"), 7, TZ)
    assert [r["title"] for r in rows] == [
        "Licht und Schatten: Malerei der Romantik",
        "Modern Paintings from the North",
        "Punk Night",
    ]
    assert await queries.upcoming(pool, Filters(city="Berlin"), 7, TZ) == []  # cancelled
    rows = await queries.ending_soon(pool, Filters(), 21, TZ)
    assert [r["title"] for r in rows] == ["Licht und Schatten: Malerei der Romantik"]
    rows = await queries.ending_soon(pool, Filters(kind="exhibition"), 90, TZ)
    assert len(rows) == 2


async def test_get_event_and_sources(pool: Pool, seeded: dict[str, int]) -> None:
    row = await queries.get_event(pool, seeded["ex-romantik"], TZ)
    assert row is not None
    assert row["venue"] == "Kunsthalle"
    assert row["venue_address"] == "Glockengießerwall 5"
    assert row["source_type"] == "museum"
    assert "embedding" not in row
    assert await queries.get_event(pool, 999999, TZ) is None
    sources = await queries.list_sources(pool, TZ)
    assert [s["name"] for s in sources] == ["Fabrik", "Kunsthalle Hamburg", "Space Channel"]


async def test_backfill(conn: AsyncConnection[DictRow], seeded: dict[str, int]) -> None:
    await conn.execute("UPDATE events SET embedding = NULL, embedding_model = NULL WHERE dedupe_key = 'ev-punk'")
    embedder = FakeEmbedder()
    assert await backfill_embeddings(conn, embedder) == 1
    assert await backfill_embeddings(conn, embedder) == 0
    assert await backfill_embeddings(conn, FakeEmbedder(up=False)) == 0


async def test_invalid_kind(pool: Pool, seeded: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="kind"):
        await queries.search(pool, None, "", Filters(kind="concert"), TZ)


async def test_loose_keyword_ignores_cross_language_stopwords(pool: Pool, seeded: dict[str, int]) -> None:
    # "a" and "for" are not German stopwords and "die" is not an English one; none may match on its own.
    out = await queries.search(pool, None, "a quiet evening for die", Filters(), TZ)
    assert set(keys(out, seeded)) == {"ex-romantik", "ev-food-berlin"}  # only "quiet" counts
    out = await queries.search(pool, None, "a for die und the", Filters(), TZ)
    assert out["results"] == []


async def test_semantic_distance_cutoff(pool: Pool, seeded: dict[str, int]) -> None:
    # Only the video carries the "science" concept; every other vector is ~orthogonal (distance ~1).
    out = await queries.search(pool, FakeEmbedder(), "science", Filters(), TZ)
    assert keys(out, seeded) == ["yt-abc"]
    assert out["results"][0]["semantic_distance"] < 0.1
    out = await queries.search(pool, FakeEmbedder(), "science", Filters(), TZ, max_distance=2.0)
    assert len(out["results"]) == 5  # every non-ended item once the cutoff is off
