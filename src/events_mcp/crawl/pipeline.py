"""RawItem to ingest.Item: keys, guards, venue and city defaults."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from events_mcp.crawl.config import ResolvedSource
from events_mcp.crawl.models import RawItem, VenueRef
from events_mcp.crawl.normalize import make_dedupe_key, norm_text
from events_mcp.ingest import Item

log = logging.getLogger(__name__)


def build_items(
    raw: list[RawItem], source: ResolvedSource, tz: ZoneInfo, now: datetime
) -> tuple[list[tuple[Item, VenueRef | None]], int]:
    """Key, guard and de-duplicate. Returns ``(item, venue)`` pairs and the number rejected.

    The venue is returned apart from the item so the caller can resolve it (database lookup,
    geocoding) before the item is written.
    """
    out: dict[str, tuple[Item, VenueRef | None]] = {}
    rejected = 0
    listing = source.fetch_urls
    for r in raw:
        # Plausibility guards in code: a mis-parsed year must not put an item in 1970 or 2099.
        if r.starts_at and not (now - timedelta(days=366) <= r.starts_at <= now + timedelta(days=731)):
            rejected += 1
            continue
        if r.starts_at and r.ends_at and r.ends_at < r.starts_at:
            rejected += 1
            continue
        opens, closes = _registration(r, now)
        venue = source.venue.model_dump() if source.venue else None
        ref = VenueRef.model_validate(venue) if venue else r.venue
        key = make_dedupe_key(
            title=r.title,
            starts_at=r.starts_at,
            venue_name=ref.name if ref else None,
            tz=tz,
            native_id=r.native_id,
            item_url=r.url,
            listing_urls=listing,
        )
        city = (ref.city if ref and ref.city else None) or source.city
        item = Item(
            title=r.title,
            dedupe_key=key,
            kind=r.kind,
            summary=r.summary,
            raw_text=r.raw_text,
            url=r.url,
            starts_at=r.starts_at,
            ends_at=r.ends_at,
            city=city,
            status=r.status,
            registration_opens_at=opens,
            registration_closes_at=closes,
            tickets_url=r.tickets_url,
        )
        out[key] = (item, ref)
    return _drop_linkless_twins(list(out.values())), rejected


_MERGEABLE = (
    "summary",
    "raw_text",
    "ends_at",
    "status",
    "tickets_url",
    "registration_opens_at",
    "registration_closes_at",
)


def _twin_signature(item: Item) -> tuple[str, datetime | None]:
    return norm_text(item.title), item.starts_at


def _drop_linkless_twins(pairs: list[tuple[Item, VenueRef | None]]) -> list[tuple[Item, VenueRef | None]]:
    """Drop a URL-less item when the same run also yields that event (title, start) with a link.

    The LLM sometimes emits one listing twice, once without its URL; the title-hash row then lives beside the
    URL row forever. Two items that both have links are kept: a site may genuinely list a show twice. What the
    dropped item knew and the kept one lacks (summary, venue, ...) is merged into the kept one.
    """
    linked: dict[tuple[str, datetime | None], int] = {}
    for n, (item, _) in enumerate(pairs):
        if not item.dedupe_key.startswith("t:"):
            linked.setdefault(_twin_signature(item), n)
    result = list(pairs)
    dropped: set[int] = set()
    for n, (item, ref) in enumerate(pairs):
        target = linked.get(_twin_signature(item)) if item.dedupe_key.startswith("t:") else None
        if target is None:
            continue
        dropped.add(n)
        winner, winner_ref = result[target]
        for name in _MERGEABLE:
            if getattr(winner, name) is None and getattr(item, name) is not None:
                setattr(winner, name, getattr(item, name))
        result[target] = (winner, winner_ref or ref)
    if dropped:
        log.info("dropped %d URL-less items that the same run lists with a URL", len(dropped))
    return [pair for n, pair in enumerate(result) if n not in dropped]


def stabilize_keys(
    pairs: list[tuple[Item, VenueRef | None]], existing: list[tuple[str, str, datetime | None]], tz: ZoneInfo
) -> int:
    """Keep a row's key when another tier (or a changed URL) would give the same listing a new one.

    ``existing`` is ``(dedupe_key, title, starts_at)`` of the source's stored rows. A new item whose key is
    unknown but whose normalised title and local start date match exactly one stored row takes that row's key,
    so the upsert updates it instead of adding a duplicate. Returns the number of keys adopted.
    """

    def signature(title: str, starts_at: datetime | None) -> tuple[str, str]:
        return norm_text(title), starts_at.astimezone(tz).date().isoformat() if starts_at else ""

    known = {key for key, _, _ in existing}
    by_signature: dict[tuple[str, str], list[str]] = {}
    for key, title, starts_at in existing:
        by_signature.setdefault(signature(title, starts_at), []).append(key)
    taken = {item.dedupe_key for item, _ in pairs if item.dedupe_key in known}
    adopted = 0
    for item, _ in pairs:
        if item.dedupe_key in known:
            continue
        candidates = by_signature.get(signature(item.title, item.starts_at), [])
        if len(candidates) == 1 and candidates[0] not in taken:
            item.dedupe_key = candidates[0]
            taken.add(candidates[0])
            adopted += 1
    return adopted


def _registration(r: RawItem, now: datetime) -> tuple[datetime | None, datetime | None]:
    """Return the registration window, or ``(None, None)`` when it is implausible.

    Only dropped, never a reason to reject the item: the event itself is still good. A pair that is
    out of order, or a date outside the window the event dates are held to, is more likely a
    mis-read than a fact.
    """
    opens, closes = r.registration_opens_at, r.registration_closes_at
    low, high = now - timedelta(days=366), now + timedelta(days=731)
    if any(d is not None and not low <= d <= high for d in (opens, closes)):
        return None, None
    if opens and closes and closes < opens:
        return None, None
    return opens, closes
