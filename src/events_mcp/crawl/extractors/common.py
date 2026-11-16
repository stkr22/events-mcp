"""Helpers shared by the structured tiers."""

import html
import re
from datetime import UTC, datetime
from time import struct_time
from typing import Any

from selectolax.parser import HTMLParser

from events_mcp.crawl.models import Status, VenueRef

_SPACES = re.compile(r"\s+")
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|tr)>|<br\s*/?>", re.IGNORECASE)


def plain_text(value: Any) -> str | None:
    """Strip tags and collapse whitespace; ``None`` for empty or non-string input."""
    if not isinstance(value, str) or not value.strip():
        return None
    if "<" in value:
        value = _BLOCK_END.sub(" ", value)
        value = HTMLParser(f"<div>{value}</div>").text(separator="")
    text = _SPACES.sub(" ", html.unescape(value)).strip()
    return text or None


def struct_to_dt(value: struct_time | None) -> datetime | None:
    """Feedparser's UTC ``struct_time`` as an aware datetime."""
    if value is None:
        return None
    return datetime(*value[:6], tzinfo=UTC)


_STATUS_WORDS: dict[str, Status] = {
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "postponed": "postponed",
    "rescheduled": "rescheduled",
    "scheduled": "scheduled",
    "confirmed": "scheduled",
    "tentative": "scheduled",
}


def map_status(value: Any) -> Status | None:
    """schema.org ``eventStatus`` (``https://schema.org/EventCancelled``) or iCal ``STATUS`` to our vocabulary."""
    if not isinstance(value, str):
        return None
    word = value.rsplit("/", 1)[-1].lower().removeprefix("event")
    return _STATUS_WORDS.get(word)


def venue_from(name: str | None, address: str | None, city: str | None) -> VenueRef | None:
    """Build a venue, or ``None`` when there is no name."""
    if not name:
        return None
    return VenueRef(name=name, address=address, city=city)
