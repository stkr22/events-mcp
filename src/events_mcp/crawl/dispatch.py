"""Fetch a source's pages and pick the extraction tier."""

import logging
import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from events_mcp.crawl.config import ResolvedSource
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.feeds import extract_rss, extract_youtube
from events_mcp.crawl.extractors.ical import extract_ical
from events_mcp.crawl.extractors.jsonld import extract_jsonld
from events_mcp.crawl.fetch import Fetcher
from events_mcp.crawl.models import Extraction, Page
from events_mcp.crawl.normalize import same_site

log = logging.getLogger(__name__)

# Cheapest first. 2b/2c add `selectors` and `llm` after these.
TIERS = ("ical", "rss", "youtube", "jsonld")
_EXTRACTORS = {"ical": extract_ical, "rss": extract_rss, "youtube": extract_youtube, "jsonld": extract_jsonld}
_ALTERNATE = re.compile(r"(text/calendar|application/(rss|atom)\+xml)", re.IGNORECASE)


class NoExtractorError(Exception):
    """No tier produced items for these pages."""


def discover_alternates(page: Page) -> list[str]:
    """Same-site ``<link rel="alternate">`` calendar and feed URLs on an HTML page."""
    if not page.is_html:
        return []
    found = []
    for link in HTMLParser(page.text).css('link[rel="alternate"]'):
        if _ALTERNATE.search(link.attributes.get("type") or "") and (href := link.attributes.get("href")):
            url = urljoin(page.url, href)
            if same_site(url, page.url):
                found.append(url)
    return found


async def fetch_pages(source: ResolvedSource, fetcher: Fetcher, *, etag: str | None = None) -> list[Page]:
    """Fetch the listing pages, politely. A conditional 304 on a single-page source returns ``[]``."""
    pages: list[Page] = []
    for n, url in enumerate(source.fetch_urls):
        page = await fetcher.get(
            url,
            rps=source.politeness.rps,
            ignore_robots=source.politeness.ignore_robots,
            etag=etag if n == 0 and len(source.fetch_urls) == 1 else None,
        )
        if page is not None:
            pages.append(page)
    return pages


async def extract(
    source: ResolvedSource,
    pages: list[Page],
    fetcher: Fetcher,
    now_ctx: Context,
    *,
    only: str | None = None,
    preferred: str | None = None,
) -> Extraction:
    """Run the tiers in order (``only`` forces one, ``preferred`` goes first) and return the first hit."""
    if only and only != "auto" and only not in _EXTRACTORS:
        raise NoExtractorError(f"extractor {only!r} is not available in this version")
    order = [only] if only and only != "auto" else list(TIERS)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    if source.type == "youtube":
        order = ["youtube"]
    for name in order:
        result = _EXTRACTORS[name](pages, now_ctx)
        if result is not None and result.items:
            return result
    if only in {None, "auto"} and source.type != "youtube":
        # An HTML page may advertise a calendar or feed of its own.
        for page in pages:
            for alt in discover_alternates(page)[:2]:
                extra = await fetcher.get(alt, rps=source.politeness.rps, ignore_robots=source.politeness.ignore_robots)
                if extra is None:
                    continue
                for name in ("ical", "rss"):
                    result = _EXTRACTORS[name]([extra], now_ctx)
                    if result is not None and result.items:
                        return result
    raise NoExtractorError(
        "no structured data (iCal, RSS/Atom, YouTube, schema.org Event) found; "
        "the selector and LLM tiers are not enabled yet"
    )
