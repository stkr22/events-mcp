"""Ask the LLM for a CSS schema once, and keep it only if it reproduces what the LLM itself extracted."""

import logging
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz
from selectolax.parser import HTMLParser

from events_mcp.crawl.config import ResolvedSource
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.selectors import SelectorSchema, extract_selectors
from events_mcp.crawl.llm import BadOutputError, ChatClient, NoProviderError, ask_model
from events_mcp.crawl.models import Page, RawItem
from events_mcp.crawl.normalize import norm_text

log = logging.getLogger(__name__)

MIN_ITEMS = 3
HTML_LIMIT = 40_000
MIN_REPRODUCED = 0.8
KEEP_ATTRS = {"class", "id", "href", "datetime", "itemprop", "role"}

SYSTEM = (
    "You write CSS selectors that extract listings from one kind of web page. Answer with one JSON object "
    '{"base_selector": "...", "fields": {"title": "...", "url": "...", "date_text": "..."}}. '
    "base_selector matches ONE element per listing (a card or row). Each field selector is relative to that "
    "element; append ::attr(href) to read an attribute (use it for url). title and date_text read element text. "
    "Use plain CSS3 selectors (tag, class, descendant, child, :nth-child). Prefer stable class names over positions. "
    "Omit url or date_text if the page has none."
)


def trim_html(html: str, limit: int = HTML_LIMIT) -> str:
    """Strip everything that is not structure: scripts, styles, head, svg and most attributes."""
    tree = HTMLParser(html)
    for sel in ("script", "style", "noscript", "svg", "iframe", "head", "link", "meta", "img", "picture", "form"):
        for node in tree.css(sel):
            node.decompose()
    for node in tree.css("*"):
        for name in [a for a in node.attributes if a not in KEEP_ATTRS]:
            del node.attrs[name]
    body = tree.body.html if tree.body else tree.html
    return " ".join((body or "").split())[:limit]


async def generate_selectors(
    chain: Sequence[ChatClient],
    source: ResolvedSource,
    pages: list[Page],
    reference: list[RawItem],
    tz: ZoneInfo,
    now: datetime,
) -> tuple[SelectorSchema | None, str | None]:
    """Return ``(schema, provider)``; ``schema`` is ``None`` unless it reproduces >= 80% of ``reference``."""
    html_pages = [p for p in pages if p.is_html]
    if len(reference) < MIN_ITEMS or not html_pages:
        return None, None
    examples = "\n".join(f"- {i.title}" for i in reference[:6])
    user = (
        f"Source: {source.name}. These listings are on the page:\n{examples}\n\n"
        f"Write the selectors that find all listings like these.\n\nHTML:\n{trim_html(html_pages[0].text)}"
    )
    try:
        schema, provider = await ask_model(chain, SYSTEM, user, SelectorSchema)
    except (NoProviderError, BadOutputError) as exc:
        log.info("%s: no selector schema (%s)", source.name, exc)
        return None, None
    try:
        replay = extract_selectors(pages, schema, Context(source, tz, now))
    except Exception as exc:  # selectolax raises on selectors it cannot parse
        log.info("%s: generated selectors do not run: %s", source.name, exc)
        return None, provider
    found = [norm_text(i.title) for i in replay.items] if replay else []
    hits = sum(1 for r in reference if any(fuzz.ratio(norm_text(r.title), f) >= 90 for f in found))  # noqa: PLR2004
    ratio = hits / len(reference)
    if ratio < MIN_REPRODUCED:
        log.info("%s: generated selectors reproduce only %.0f%% of the LLM items; discarded", source.name, ratio * 100)
        return None, provider
    return schema, provider
