from datetime import UTC, date, datetime

from events_mcp.crawl.normalize import (
    canonical_url,
    is_item_specific,
    make_dedupe_key,
    norm_text,
    parse_iso,
    same_site,
    to_aware,
)

from .conftest import TZ


def test_canonical_url_drops_noise_and_sorts() -> None:
    assert (
        canonical_url("HTTPS://Club.Example/e/punk/?b=2&utm_source=x&a=1&fbclid=zz&PHPSESSID=9#top")
        == "https://club.example/e/punk?a=1&b=2"
    )
    assert canonical_url("/e/1", base="https://club.example/list") == "https://club.example/e/1"


def test_listing_urls_are_not_item_specific() -> None:
    listing = ["https://club.example/events"]
    assert not is_item_specific("https://club.example/events/?utm_medium=a", listing)
    assert not is_item_specific("https://club.example/", listing)
    assert is_item_specific("https://club.example/events/1", listing)


def test_norm_text_folds_case_width_and_punctuation() -> None:
    assert norm_text("  Monet &  Freunde!! ") == "monet freunde"
    assert norm_text("\uff2d\uff2f\uff2e\uff25\uff34") == "monet"
    assert norm_text(None) == ""


def test_key_precedence() -> None:
    start = datetime(2026, 11, 21, 19, tzinfo=UTC)

    def key(**more: str | list[str]) -> str:
        return make_dedupe_key(title="Punk Night", starts_at=start, venue_name="Fabrik", tz=TZ, **more)  # ty: ignore[invalid-argument-type]

    assert key(native_id="uid@x") == "id:uid@x"
    assert key(item_url="https://c.example/e/1?utm_x=1") == "u:https://c.example/e/1"
    # A listing URL is not an identity: falls through to the title hash.
    fallback = key(item_url="https://c.example/events", listing_urls=["https://c.example/events"])
    assert fallback.startswith("t:")


def test_title_key_is_stable_across_formatting_and_uses_local_date() -> None:
    a = make_dedupe_key(
        title="Punk  Night!", starts_at=datetime(2026, 11, 21, 23, 30, tzinfo=UTC), venue_name="FABRIK", tz=TZ
    )
    b = make_dedupe_key(
        title="punk night", starts_at=datetime(2026, 11, 22, 0, 30, tzinfo=TZ), venue_name="Fabrik", tz=TZ
    )
    # 23:30 UTC on the 21st is already the 22nd in Hamburg, same as b.
    assert a == b


def test_date_only_ranges_cover_whole_days() -> None:
    assert to_aware(date(2026, 10, 1), TZ) == datetime(2026, 10, 1, 0, 0, tzinfo=TZ)
    assert to_aware(date(2027, 2, 28), TZ, end=True) == datetime(2027, 2, 28, 23, 59, 59, tzinfo=TZ)
    assert parse_iso("2026-11-21T20:00:00", TZ) == datetime(2026, 11, 21, 20, 0, tzinfo=TZ)
    assert parse_iso("2026-11-21T19:00:00Z", TZ) == datetime(2026, 11, 21, 19, 0, tzinfo=UTC)
    assert parse_iso("not a date", TZ) is None


def test_same_site_ignores_www() -> None:
    assert same_site("https://www.a.example/x", "https://a.example/y")
    assert not same_site("https://b.example/x", "https://a.example/y")
