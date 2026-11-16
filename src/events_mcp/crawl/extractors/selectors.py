"""CSS-selector tier: a schema the LLM wrote once, replayed for free until the site changes.

The schema is ``{"base_selector": "...", "fields": {"title": "...", "url": "...", "date_text": "..."}}``.
Field selectors are relative to each base node; ``::attr(name)`` reads an attribute instead of the text.
Nothing here calls an LLM: generation lives in ``selector_gen``.
"""

import logging
import re
from datetime import date, datetime, time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from selectolax.parser import HTMLParser, Node

from events_mcp.crawl.extractors import Context
from events_mcp.crawl.models import Extraction, Page, RawItem
from events_mcp.crawl.normalize import same_site

log = logging.getLogger(__name__)

_ATTR = re.compile(r"^(?P<css>.*?)\s*::attr\((?P<attr>[\w:-]+)\)\s*$")
MAX_REJECTED_SHARE = 0.5


class SelectorSchema(BaseModel):
    """What the LLM produces and ``sources.selector_schema`` stores."""

    model_config = ConfigDict(extra="ignore")

    base_selector: str
    fields: dict[str, str]


def _read(node: Node, spec: str | None) -> str | None:
    """Text (or attribute) of the first match of ``spec`` below ``node``; ``None`` if absent."""
    if not spec:
        return None
    attr = _ATTR.match(spec)
    css = attr["css"] if attr else spec
    target = node.css_first(css) if css else node
    if target is None:
        return None
    if attr:
        return target.attributes.get(attr["attr"])
    return " ".join(target.text(separator=" ").split()) or None


_MONTHS = {
    **{
        m: i
        for i, names in enumerate(
            [
                ("jan", "januar", "january"),
                ("feb", "februar", "february"),
                ("mär", "mar", "märz", "maerz", "march"),
                ("apr", "april"),
                ("mai", "may"),
                ("jun", "juni", "june"),
                ("jul", "juli", "july"),
                ("aug", "august"),
                ("sep", "sept", "september"),
                ("okt", "oct", "oktober", "october"),
                ("nov", "november"),
                ("dez", "dec", "dezember", "december"),
            ],
            start=1,
        )
        for m in names
    },
}
_MONTH_WORDS = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE = re.compile(
    rf"(?P<iso>\d{{4}})-(?P<iso_m>\d{{2}})-(?P<iso_d>\d{{2}})"
    rf"|(?P<d>\d{{1,2}})\.\s?(?P<m>\d{{1,2}})\.(?:\s?(?P<y>\d{{4}}|\d{{2}})(?!\d))?"
    rf"|(?P<td>\d{{1,2}})\.?\s*(?P<tm>{_MONTH_WORDS})(?![a-zäöüß])\.?(?:,?\s*(?P<ty>\d{{4}})(?!\d))?",
    re.IGNORECASE,
)
_TIME = re.compile(r"\b(?P<h>[01]?\d|2[0-3])(?::(?P<min>[0-5]\d)|\s*Uhr|\s*h\b)", re.IGNORECASE)
_DATE_LOOKBACK_DAYS = 45


def _scan(text: str, tz: ZoneInfo, now: datetime) -> list[tuple[int, date, bool]]:
    """Every date in ``text`` as ``(position, date, year_was_stated)``, in reading order."""
    today = now.astimezone(tz).date()
    out: list[tuple[int, date, bool]] = []
    for m in _DATE.finditer(text):
        try:
            if m["iso"]:
                out.append((m.start(), date(int(m["iso"]), int(m["iso_m"]), int(m["iso_d"])), True))
                continue
            day = int(m["d"] or m["td"])
            month = int(m["m"]) if m["m"] else _MONTHS[m["tm"].lower()]
            year_text = m["y"] or m["ty"]
            if year_text:
                year = int(year_text) + (2000 if len(year_text) == 2 else 0)  # noqa: PLR2004
                out.append((m.start(), date(year, month, day), True))
            else:
                guess = date(today.year, month, day)
                if (today - guess).days > _DATE_LOOKBACK_DAYS:
                    guess = date(today.year + 1, month, day)  # "21.11." read in December means next year
                out.append((m.start(), guess, False))
        except ValueError:
            continue  # 31.02. and friends
    return out


def parse_dates(text: str, tz: ZoneInfo, now: datetime) -> tuple[datetime | None, datetime | None]:
    """Find the first and last date in free text ("Sa. 21.11.", "20. November - 6. Dezember"), German or English.

    Day-first only (``5.12.2026`` is 5 December). A time directly after the first date sets the start time;
    a single date has no end, several dates make the last one the end (23:59:59).
    """
    found = _scan(text, tz, now)
    if not found:
        return None, None
    start_day = found[0][1]
    time_match = _TIME.search(text[found[0][0] :])
    if time_match:
        start = datetime.combine(start_day, time(int(time_match["h"]), int(time_match["min"] or 0)), tzinfo=tz)
    else:
        start = datetime.combine(start_day, time.min, tzinfo=tz)
    if len(found) < 2:  # noqa: PLR2004
        return start, None
    _, end_day, end_has_year = found[-1]
    if end_day < start_day and not end_has_year:
        end_day = end_day.replace(year=end_day.year + 1)  # "21. Dez. - 3. Jan."
    end = datetime.combine(end_day, time(23, 59, 59), tzinfo=tz)
    return (start, end) if end >= start else (start, None)


def extract_selectors(pages: list[Page], schema: SelectorSchema, ctx: Context) -> Extraction | None:
    """Items from every HTML page; ``None`` when the base selector matches nothing at all."""
    items: list[RawItem] = []
    rejected = 0
    matched = False
    for page in pages:
        if not page.is_html:
            continue
        for node in HTMLParser(page.text).css(schema.base_selector):
            matched = True
            title = _read(node, schema.fields.get("title"))
            if not title:
                rejected += 1
                continue
            url = _read(node, schema.fields.get("url"))
            if url:
                url = urljoin(page.url, url)
                if not url.startswith("http") or not same_site(url, page.url):
                    url = None
            date_text = _read(node, schema.fields.get("date_text"))
            starts, ends = parse_dates(date_text, ctx.tz, ctx.now) if date_text else (None, None)
            text = " ".join(node.text(separator=" ").split())
            items.append(
                RawItem(
                    title=title[:500],
                    kind=ctx.default_kind,  # ty: ignore[invalid-argument-type]
                    summary=text[:500] or None,
                    raw_text=text or None,
                    url=url,
                    starts_at=starts,
                    ends_at=ends,
                )
            )
    if not matched:
        return None
    return Extraction("selectors", items, rejected)


def healthy(result: Extraction | None) -> bool:
    """Tell whether a selector run is usable: at least one item, and at most half of the matches rejected."""
    if result is None or not result.items:
        return False
    return result.rejected <= MAX_REJECTED_SHARE * (len(result.items) + result.rejected)
