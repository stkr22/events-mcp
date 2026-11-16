"""HTML to main-content markdown, and the content hash that gates extraction."""

import hashlib
import re

import trafilatura
from markdownify import markdownify
from selectolax.parser import HTMLParser

_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\u00a0]+")


def _tidy(text: str) -> str:
    lines = [_SPACES.sub(" ", ln).strip() for ln in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


OVERLAY_MAX_CHARS = 3000
HEADING_KEEP = 0.8
_OVERLAY = re.compile(
    r"cookie|consent|gdpr|usercentrics|cmp-|datenschutz-einstell|privacy-(banner|notice|settings)", re.I
)


def _strip_overlays(html: str) -> str:
    """Drop consent dialogs and modals: on a rendered page they can outweigh the content."""
    tree = HTMLParser(html)
    for node in tree.css("[role=dialog], [role=alertdialog], [aria-modal=true], dialog"):
        node.decompose()
    for node in tree.css("[id], [class]"):
        if node.tag in {"html", "body", "main"} or node.parent is None:
            continue
        label = f"{node.attributes.get('id') or ''} {node.attributes.get('class') or ''}"
        # Size cap: a wrapper that merely carries a cookie class (body-level state flags) holds the content.
        if _OVERLAY.search(label) and len(node.text(strip=True)) < OVERLAY_MAX_CHARS:
            node.decompose()
    return tree.html or html


def html_to_markdown(html: str, url: str | None = None) -> str:
    """Convert to main-content markdown: trafilatura first, ``<main>``/``<body>`` via markdownify as fallback."""
    html = _strip_overlays(html)
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        favor_recall=True,
        deduplicate=False,
    )
    fallback = _fallback_markdown(html)
    # Listing pages are mostly cards, which trafilatura drops as boilerplate: it then returns a footer or an
    # intro paragraph. Trust it only when it kept a sizeable share of what the plain body conversion sees.
    if extracted and len(extracted.strip()) > 80 and len(extracted) >= 0.4 * len(fallback):  # noqa: PLR2004
        # ... and every heading must survive: card titles are the items.
        wanted = _headings(html)
        if not wanted or sum(h in extracted for h in wanted) >= HEADING_KEEP * len(wanted):
            return _tidy(extracted)
    return fallback


def _headings(html: str) -> list[str]:
    """Texts of the h1-h4 headings, the part of a listing that must not get lost."""
    return [t for n in HTMLParser(html).css("h1, h2, h3, h4") if 3 <= len(t := n.text(strip=True)) <= 200]  # noqa: PLR2004


def _fallback_markdown(html: str) -> str:
    """Convert ``<main>`` (or ``<body>``) without chrome elements to markdown."""
    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "footer", "header", "noscript", "iframe", "svg", "form"):
        for node in tree.css(sel):
            node.decompose()
    root = tree.css_first("main") or tree.body
    if root is None:
        return ""
    return _tidy(markdownify(root.html or "", heading_style="ATX"))


def apply_ignore_patterns(text: str, patterns: list[str]) -> str:
    """Drop every line matching one of the regexes, before hashing (counters, timestamps, tokens)."""
    if not patterns:
        return text
    compiled = [re.compile(p) for p in patterns]
    return "\n".join(ln for ln in text.splitlines() if not any(c.search(ln) for c in compiled))


def content_hash(text: str) -> str:
    """sha256 of the cleaned text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
