"""Fetch a source's pages and pick the extraction tier."""

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from events_mcp.crawl.config import ResolvedSource
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.feeds import extract_rss, extract_youtube
from events_mcp.crawl.extractors.ical import extract_ical
from events_mcp.crawl.extractors.jsonld import extract_jsonld
from events_mcp.crawl.fetch import Fetcher, FetchError
from events_mcp.crawl.models import Extraction, Page

if TYPE_CHECKING:
    from events_mcp.crawl.browser import BrowserFetcher
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


async def fetch_pages(
    source: ResolvedSource, fetcher: Fetcher, browser: BrowserFetcher | None = None, *, etag: str | None = None
) -> list[Page]:
    """Fetch the listing pages, politely (robots and pacing apply to browser renders too)."""
    pages: list[Page] = []
    for n, url in enumerate(source.fetch_urls):
        if source.render != "http":
            if browser is None:
                raise FetchError("render: browser needs CDP_URL")
            await fetcher.check(url, rps=source.politeness.rps, ignore_robots=source.politeness.ignore_robots)
            pages.append(
                await browser.get(
                    url, stealth=source.render == "browser_stealth", wait_for_selector=source.wait_for_selector
                )
            )
            continue
        page = await fetcher.get(
            url,
            rps=source.politeness.rps,
            ignore_robots=source.politeness.ignore_robots,
            etag=etag if n == 0 and len(source.fetch_urls) == 1 else None,
        )
        if page is not None:
            pages.append(page)
    return pages


async def structured(
    source: ResolvedSource,
    pages: list[Page],
    fetcher: Fetcher,
    now_ctx: Context,
    *,
    only: str | None = None,
    preferred: str | None = None,
) -> Extraction | None:
    """Run the structured tiers in order (``only`` forces one, ``preferred`` goes first); ``None`` if none hits."""
    if only in {"selectors", "llm"}:
        return None  # not a structured tier: the caller goes straight to the text path
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
    if only in _EXTRACTORS:
        raise NoExtractorError(f"the {only} tier found nothing on this page")
    return None
