from events_mcp.crawl.dispatch import discover_alternates
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.feeds import extract_rss, extract_youtube
from events_mcp.crawl.extractors.ical import extract_ical
from events_mcp.crawl.extractors.jsonld import extract_jsonld
from events_mcp.crawl.models import canonical_payload

from .conftest import NOW, TZ, make_source, page


def test_ical_expands_recurrences_and_maps_fields(ctx: Context) -> None:
    result = extract_ical([page("club.ics", "text/calendar")], ctx)
    assert result is not None
    assert result.rejected == 1  # the VEVENT without DTSTART
    by_title = {}
    for i in result.items:
        by_title.setdefault(i.title, []).append(i)

    punk = by_title["Punk Night"][0]
    assert punk.starts_at is not None
    assert punk.starts_at.isoformat() == "2026-11-21T20:00:00+01:00"
    assert punk.native_id == "punk-1@club.example@2026-11-21T20:00:00+01:00"
    assert punk.summary == "Three bands loud."
    assert punk.venue is not None and punk.venue.name == "Fabrik"
    assert punk.status == "scheduled"
    assert punk.url == "https://club.example/e/punk"

    quizzes = by_title["Pub Quiz"]
    assert len(quizzes) == 3
    assert len({q.native_id for q in quizzes}) == 3  # one key per occurrence
    assert all(q.status == "cancelled" for q in quizzes)

    fair = by_title["Weekend Fair"][0]  # all-day, DTEND exclusive: runs Sat and Sun
    assert fair.starts_at is not None
    assert fair.starts_at.isoformat() == "2026-11-28T00:00:00+01:00"
    assert fair.ends_at is not None
    assert fair.ends_at.isoformat() == "2026-11-29T23:59:59+01:00"


def test_ical_respects_the_horizon() -> None:
    short = Context(make_source(), TZ, NOW, horizon_days=10)
    result = extract_ical([page("club.ics", "text/calendar")], short)
    assert result is not None
    assert {i.title for i in result.items} == {"Pub Quiz", "Punk Night"}  # the 28th fair is outside 10 days


def test_ical_ignores_non_calendars(ctx: Context) -> None:
    assert extract_ical([page("plain.html", "text/html")], ctx) is None


def test_youtube_feed(ctx: Context) -> None:
    result = extract_youtube([page("channel.xml", "application/atom+xml")], ctx)
    assert result is not None
    (v,) = result.items
    assert (v.kind, v.native_id, v.title) == ("video", "abc123XYZ_-", "Studio Tour 2026")
    assert v.starts_at is not None
    assert v.starts_at.isoformat() == "2026-11-10T09:30:00+00:00"
    assert v.summary == "A tour of the studio."
    assert v.url == "https://www.youtube.com/watch?v=abc123XYZ_-"


def test_rss_feed_source_keeps_dated_items_only_as_feed_entries() -> None:
    feed_ctx = Context(make_source(type="feed", default_kind="other"), TZ, NOW)
    result = extract_rss([page("news.xml", "application/rss+xml")], feed_ctx)
    assert result is not None
    assert [i.title for i in result.items] == ["New season announced", "No date here"]
    first = result.items[0]
    assert (first.kind, first.native_id, first.summary) == ("other", "news-1", "Doors open.")


def test_rss_on_a_non_feed_source_rejects_undated_entries(ctx: Context) -> None:
    result = extract_rss([page("news.xml", "application/rss+xml")], ctx)
    assert result is not None
    assert [i.title for i in result.items] == ["New season announced"]
    assert result.rejected == 1


def test_jsonld_events(ctx: Context) -> None:
    result = extract_jsonld([page("museum.html", "text/html", "https://museum.example/ausstellungen")], ctx)
    assert result is not None
    assert result.rejected == 1  # "No dates"
    exhibit = next(i for i in result.items if i.kind == "exhibition")
    assert exhibit.title == "Monet & Freunde"
    assert exhibit.starts_at is not None
    assert exhibit.starts_at.isoformat() == "2026-10-01T00:00:00+02:00"
    assert exhibit.ends_at is not None
    assert exhibit.ends_at.isoformat() == "2027-02-28T23:59:59+01:00"
    assert exhibit.url == "https://museum.example/ausstellungen/monet"
    assert exhibit.native_id == "https://museum.example/a/monet"
    assert exhibit.venue is not None
    assert (exhibit.venue.name, exhibit.venue.city) == ("Kunsthalle", "Hamburg")
    assert exhibit.venue.address == "Glockengiesserwall 5, 20095, Hamburg"

    jazz = next(i for i in result.items if i.title == "Late Night Jazz")
    assert jazz.status == "cancelled"
    assert jazz.url is None  # other site: dropped


def test_jsonld_none_without_events(ctx: Context) -> None:
    assert extract_jsonld([page("plain.html", "text/html")], ctx) is None


def test_discover_alternates_stays_on_site() -> None:
    p = page("alt.html", "text/html", "https://club.example/events")
    assert discover_alternates(p) == ["https://club.example/events.ics"]


def test_canonical_payload_ignores_item_order(ctx: Context) -> None:
    result = extract_ical([page("club.ics", "text/calendar")], ctx)
    assert result is not None
    items = result.items
    assert canonical_payload(items) == canonical_payload(list(reversed(items)))


def test_jsonld_offers_give_the_registration_window_and_link(ctx: Context) -> None:
    result = extract_jsonld([page("offers.html", "text/html")], ctx)
    assert result is not None
    by_title = {i.title: i for i in result.items}
    fest = by_title["Sommerfest"]
    assert fest.registration_opens_at is not None and fest.registration_closes_at is not None
    assert fest.registration_opens_at.isoformat() == "2026-11-10T10:00:00+01:00"
    assert fest.registration_closes_at.isoformat() == "2026-12-04T23:59:59+01:00"  # date-only: end of that day
    assert fest.tickets_url == "https://tickets.example/sommerfest"  # another site, stated by the page itself
    backwards = by_title["Backwards"]
    assert (backwards.registration_opens_at, backwards.registration_closes_at) == (None, None)
    plain = by_title["No offer"]
    assert (plain.registration_opens_at, plain.registration_closes_at, plain.tickets_url) == (None, None, None)
