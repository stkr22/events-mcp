"""schema.org Event data (JSON-LD and microdata) through extruct."""

from collections import Counter
from typing import Any
from urllib.parse import urljoin

import extruct

from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.common import map_status, plain_text, venue_from
from events_mcp.crawl.models import Extraction, Page, RawItem
from events_mcp.crawl.normalize import parse_iso, same_site


def _types(node: dict[str, Any]) -> list[str]:
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else [str(x) for x in t]


def _is_event(node: dict[str, Any]) -> bool:
    return any(t.rsplit("/", 1)[-1].endswith(("Event", "Festival")) for t in _types(node))


def _walk(node: Any) -> list[dict[str, Any]]:
    """Every Event node anywhere in the structure (``@graph``, ``subEvent``, nested ``itemListElement``)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for n in node:
            found.extend(_walk(n))
    elif isinstance(node, dict):
        if _is_event(node):
            found.append(node)
        for key, value in node.items():
            if key != "location" and isinstance(value, dict | list):
                found.extend(_walk(value))
    return found


def _text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name")
    return plain_text(value)


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _venue(location: Any, fallback_city: str | None) -> Any:
    location = _first(location)
    if isinstance(location, str):
        return venue_from(plain_text(location), None, fallback_city)
    if not isinstance(location, dict):
        return None
    address = _first(location.get("address"))
    street = city = None
    if isinstance(address, dict):
        city = _text(address.get("addressLocality"))
        parts = [_text(address.get("streetAddress")), _text(address.get("postalCode")), city]
        street = ", ".join(p for p in parts if p) or None
    elif isinstance(address, str):
        street = plain_text(address)
    return venue_from(_text(location.get("name")), street, city or fallback_city)


def extract_jsonld(pages: list[Page], ctx: Context) -> Extraction | None:
    """Events from every HTML page; ``None`` unless at least one valid item came out."""
    items: list[RawItem] = []
    rejected = 0
    for page in pages:
        if not page.is_html:
            continue
        data = extruct.extract(page.text, base_url=page.url, syntaxes=["json-ld", "microdata"], uniform=True)
        for node in _walk(data.get("json-ld", []) + data.get("microdata", [])):
            item = _item(node, page, ctx)
            if item is None:
                rejected += 1
            else:
                items.append(item)
    if not items:
        return None
    # An @id shared by several nodes (the listing page itself) identifies nothing.
    shared = {i for i, n in Counter(it.native_id for it in items).items() if i and n > 1}
    for it in items:
        if it.native_id in shared:
            it.native_id = None
    return Extraction("jsonld", items, rejected)


def _item(node: dict[str, Any], page: Page, ctx: Context) -> RawItem | None:
    title = _text(node.get("name"))
    starts = parse_iso(_text(node.get("startDate")), ctx.tz)
    ends = parse_iso(_text(node.get("endDate")), ctx.tz, end=True)
    if not title or (starts is None and ends is None):
        return None
    if starts and ends and ends < starts:
        ends = None
    url = _text(_first(node.get("url")))
    if url and not url.startswith("http"):
        url = urljoin(page.url, url)
    if url and not same_site(url, page.url):
        url = None
    description = _text(node.get("description"))
    kinds = _types(node)
    kind = "exhibition" if any(t.endswith("ExhibitionEvent") for t in kinds) else ctx.default_kind
    native = node.get("@id")
    return RawItem(
        title=title,
        kind=kind,  # ty: ignore[invalid-argument-type]
        summary=description[:500] if description else None,
        raw_text=description,
        url=url,
        starts_at=starts,
        ends_at=ends,
        status=map_status(_text(node.get("eventStatus"))),
        venue=_venue(node.get("location"), ctx.source.city),
        native_id=native if isinstance(native, str) and native and not native.startswith("_:") else None,
    )
