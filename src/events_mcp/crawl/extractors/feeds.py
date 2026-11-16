"""RSS/Atom feeds and YouTube channel feeds, both through feedparser."""

import feedparser

from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.common import plain_text, struct_to_dt
from events_mcp.crawl.models import Extraction, Page, RawItem

_FEED_TYPES = ("rss", "atom", "xml")


def looks_like_feed(page: Page) -> bool:
    """RSS/Atom content type, or an XML body whose root is ``rss``/``feed``/``rdf``."""
    if any(t in page.content_type for t in _FEED_TYPES) and not page.is_html:
        return True
    head = page.text.lstrip()[:400].lower()
    return head.startswith(("<?xml", "<rss", "<feed", "<rdf")) and any(t in head for t in ("<rss", "<feed", "<rdf"))


def extract_youtube(pages: list[Page], ctx: Context) -> Extraction | None:  # noqa: ARG001 - uniform tier signature
    """Videos from the channel's public feed: no API key, ``dedupe`` on the video id."""
    items: list[RawItem] = []
    for page in pages:
        parsed = feedparser.parse(page.text)
        for e in parsed.entries:
            video_id = e.get("yt_videoid")
            title = plain_text(e.get("title"))
            published = struct_to_dt(e.get("published_parsed"))
            if not video_id or not title or published is None:
                continue
            description = plain_text(e.get("summary") or e.get("media_description"))
            items.append(
                RawItem(
                    title=title,
                    kind="video",
                    summary=description[:500] if description else None,
                    raw_text=description,
                    url=e.get("link") or f"https://www.youtube.com/watch?v={video_id}",
                    starts_at=published,
                    native_id=video_id,
                )
            )
    return Extraction("youtube", items) if items or pages else None


def extract_rss(pages: list[Page], ctx: Context) -> Extraction | None:
    """Feed entries. Without dates a non-``feed`` source cannot be placed on a calendar, so it is not handled here."""
    feeds = [p for p in pages if looks_like_feed(p)]
    if not feeds:
        return None
    items: list[RawItem] = []
    rejected = 0
    for page in feeds:
        parsed = feedparser.parse(page.text)
        for e in parsed.entries:
            title = plain_text(e.get("title"))
            published = struct_to_dt(e.get("published_parsed") or e.get("updated_parsed"))
            if not title:
                rejected += 1
                continue
            if published is None and ctx.source.type != "feed":
                rejected += 1  # escalate to the LLM tier (2b) rather than guess dates
                continue
            body = plain_text(e.get("summary") or (e.get("content") or [{}])[0].get("value"))
            items.append(
                RawItem(
                    title=title,
                    kind=ctx.default_kind,  # ty: ignore[invalid-argument-type]
                    summary=body[:500] if body else None,
                    raw_text=body,
                    url=e.get("link"),
                    starts_at=published,
                    native_id=e.get("id") or e.get("guid"),
                )
            )
    if not items and rejected:
        return None
    return Extraction("rss", items, rejected)
