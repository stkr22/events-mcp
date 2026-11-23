from datetime import UTC, datetime, timedelta
from typing import Any

from events_mcp.crawl.models import RawItem
from events_mcp.crawl.pipeline import build_items, stabilize_keys
from events_mcp.ingest import Item

from .conftest import NOW, TZ, make_source

D = datetime(2026, 11, 21, 19, 0, tzinfo=UTC)


def item(key: str, title: str = "Punk Night", starts: datetime | None = D) -> tuple[Item, None]:
    return Item(title=title, dedupe_key=key, kind="event", starts_at=starts), None


def test_unknown_key_adopts_the_single_matching_stored_key() -> None:
    pairs = [item("u:https://x/e/1", "Punk  Night!")]
    assert stabilize_keys(pairs, [("t:abc", "punk night", D)], TZ) == 1
    assert pairs[0][0].dedupe_key == "t:abc"


def test_known_keys_and_different_days_are_left_alone() -> None:
    pairs = [item("t:abc"), item("u:other", starts=datetime(2026, 11, 22, 19, tzinfo=UTC))]
    assert stabilize_keys(pairs, [("t:abc", "Punk Night", D)], TZ) == 0
    assert [p[0].dedupe_key for p in pairs] == ["t:abc", "u:other"]


def test_ambiguous_or_already_taken_matches_are_not_adopted() -> None:
    stored: list[tuple[str, str, datetime | None]] = [
        ("t:1", "Pub Quiz", D),
        ("t:2", "Pub Quiz", D),
    ]  # two rows with the same signature: unclear which
    pairs = [item("u:a", "Pub Quiz")]
    assert stabilize_keys(pairs, stored, TZ) == 0
    # Two new items cannot both adopt the same stored row.
    pairs = [item("u:a"), item("u:b")]
    assert stabilize_keys(pairs, [("t:1", "Punk Night", D)], TZ) == 1
    assert [p[0].dedupe_key for p in pairs] == ["t:1", "u:b"]


def test_build_items_carries_registration_and_drops_implausible_windows() -> None:
    source = make_source()

    def raw(title: str, opens: datetime | None, closes: datetime | None) -> RawItem:
        return RawItem(
            title=title,
            kind="event",
            starts_at=NOW + timedelta(days=30),
            registration_opens_at=opens,
            registration_closes_at=closes,
            tickets_url="https://tickets.example/x",
        )

    pairs, rejected = build_items(
        [
            raw("Good", NOW - timedelta(days=2), NOW + timedelta(days=5)),
            raw("Reversed", NOW + timedelta(days=9), NOW + timedelta(days=5)),
            raw("Year 2099", None, datetime(2099, 1, 1, tzinfo=UTC)),
            raw("Closing only", None, NOW + timedelta(days=5)),
        ],
        source,
        TZ,
        NOW,
    )
    assert rejected == 0  # the events themselves are fine
    by_title = {i.title: i for i, _ in pairs}
    assert by_title["Good"].registration_opens_at == NOW - timedelta(days=2)
    assert by_title["Closing only"].registration_closes_at == NOW + timedelta(days=5)
    for title in ("Reversed", "Year 2099"):
        got = by_title[title]
        assert (got.registration_opens_at, got.registration_closes_at) == (None, None)
        assert got.tickets_url == "https://tickets.example/x"  # the link stays


def _twin_raw(title: str, url: str | None, **kw: Any) -> RawItem:
    return RawItem(title=title, kind="event", starts_at=NOW + timedelta(days=3), url=url, **kw)


def test_a_linkless_item_is_dropped_when_the_same_run_has_it_with_a_link() -> None:
    source = make_source()
    raw = [
        _twin_raw("GET UP Comedy", None, summary="Weekly open mic."),
        _twin_raw("get up  comedy!", "https://club.example/e/get-up-205"),
        _twin_raw("Quiz", None),  # no twin: stays
    ]
    pairs, _ = build_items(raw, source, TZ, NOW)
    assert [p[0].dedupe_key[:2] for p in pairs] == ["u:", "t:"]
    assert pairs[0][0].summary == "Weekly open mic."  # what the dropped twin knew is kept


def test_two_linked_items_with_the_same_title_and_start_are_both_kept() -> None:
    raw = [
        _twin_raw("GET UP", "https://club.example/e/get-up-205"),
        _twin_raw("GET UP", "https://club.example/e/get-up-223"),
    ]
    pairs, _ = build_items(raw, make_source(), TZ, NOW)
    assert len(pairs) == 2


def test_a_linkless_item_at_another_start_is_not_a_twin() -> None:
    other = RawItem(title="GET UP", kind="event", starts_at=NOW + timedelta(days=10))
    raw = [_twin_raw("GET UP", "https://club.example/e/get-up-205"), other]
    pairs, _ = build_items(raw, make_source(), TZ, NOW)
    assert len(pairs) == 2
