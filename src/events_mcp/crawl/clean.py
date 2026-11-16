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


def html_to_markdown(html: str, url: str | None = None) -> str:
    """Convert to main-content markdown: trafilatura first, ``<main>``/``<body>`` via markdownify as fallback."""
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        favor_recall=True,
        deduplicate=False,
    )
    if extracted and len(extracted.strip()) > 80:  # noqa: PLR2004 - below this it found a cookie banner, not content
        return _tidy(extracted)
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
