from datetime import UTC, datetime

from events_mcp.crawl.pipeline import stabilize_keys
from events_mcp.ingest import Item

from .conftest import TZ

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
    stored = [("t:1", "Pub Quiz", D), ("t:2", "Pub Quiz", D)]  # two rows with the same signature: unclear which
    pairs = [item("u:a", "Pub Quiz")]
    assert stabilize_keys(pairs, stored, TZ) == 0
    # Two new items cannot both adopt the same stored row.
    pairs = [item("u:a"), item("u:b")]
    assert stabilize_keys(pairs, [("t:1", "Punk Night", D)], TZ) == 1
    assert [p[0].dedupe_key for p in pairs] == ["t:1", "u:b"]
