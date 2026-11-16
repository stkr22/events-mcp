"""iCalendar feeds: recurrences expanded within the horizon."""

from datetime import date, datetime, timedelta

import icalendar
import recurring_ical_events

from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.common import map_status, plain_text, venue_from
from events_mcp.crawl.models import Extraction, Page, RawItem
from events_mcp.crawl.normalize import to_aware


def looks_like_ical(page: Page) -> bool:
    """``text/calendar`` or a body starting with ``BEGIN:VCALENDAR``."""
    return "calendar" in page.content_type or page.text.lstrip().startswith("BEGIN:VCALENDAR")


def extract_ical(pages: list[Page], ctx: Context) -> Extraction | None:
    """Items from every calendar page; ``None`` if none of the pages is a calendar."""
    calendars = [p for p in pages if looks_like_ical(p)]
    if not calendars:
        return None
    items: list[RawItem] = []
    rejected = 0
    window_start = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(days=ctx.horizon_days)
    for page in calendars:
        cal = icalendar.Calendar.from_ical(page.text)
        # A VEVENT without DTSTART is invalid and would abort the whole expansion.
        broken = [c for c in cal.subcomponents if c.name == "VEVENT" and "DTSTART" not in c]
        for c in broken:
            cal.subcomponents.remove(c)
        rejected += len(broken)
        for ev in recurring_ical_events.of(cal).between(window_start, window_end):
            item = _item(ev, ctx)
            if item is None:
                rejected += 1
            else:
                items.append(item)
    return Extraction("ical", items, rejected)


def _item(ev: icalendar.Event, ctx: Context) -> RawItem | None:
    title = plain_text(str(ev.get("SUMMARY", "")))
    start_raw = ev.decoded("DTSTART", None)
    if not title or start_raw is None:
        return None
    all_day = isinstance(start_raw, date) and not isinstance(start_raw, datetime)
    starts = to_aware(start_raw, ctx.tz)
    end_raw = ev.decoded("DTEND", None)
    ends = None
    if end_raw is not None:
        # An all-day DTEND is exclusive: the event runs through the day before.
        end_value = end_raw - timedelta(days=1) if all_day and isinstance(end_raw, date) else end_raw
        ends = to_aware(end_value, ctx.tz, end=all_day)
        if ends < starts:
            ends = None
    uid = str(ev.get("UID", "")) or None
    location = plain_text(str(ev.get("LOCATION", ""))) if ev.get("LOCATION") else None
    description = plain_text(str(ev.get("DESCRIPTION", ""))) if ev.get("DESCRIPTION") else None
    url = str(ev["URL"]) if ev.get("URL") else None
    return RawItem(
        title=title,
        kind=ctx.default_kind,  # ty: ignore[invalid-argument-type]
        summary=description[:500] if description else None,
        raw_text=description,
        url=url,
        starts_at=starts,
        ends_at=ends,
        status=map_status(str(ev.get("STATUS", ""))),
        venue=venue_from(location.split(",")[0].strip(), location, None) if location else None,
        native_id=f"{uid}@{starts.isoformat()}" if uid else None,
    )
