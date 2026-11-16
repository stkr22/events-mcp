"""Item normalisation: URLs, text, dedupe keys and dates. Pure functions, no I/O."""

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime, time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_[a-z]+|_ga|_gl|yclid|igshid)$")
_SESSION = re.compile(r"^(phpsessid|jsessionid|sessionid|sid|sessid|cfid|cftoken)$")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def norm_text(value: str | None) -> str:
    """Casefold, NFKC, strip punctuation, collapse whitespace."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    return _SPACE.sub(" ", _PUNCT.sub(" ", text)).strip()


def canonical_url(url: str, base: str | None = None) -> str:
    """Lowercase host, drop fragment, tracking and session params; sort the query; trim a trailing slash."""
    absolute = urljoin(base, url) if base else url
    p = urlparse(absolute)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not _TRACKING.match(k.lower()) and not _SESSION.match(k.lower())
    )
    path = p.path.rstrip("/") or "/"
    return urlunparse(
        (
            p.scheme.lower(),
            (p.hostname or "").lower() + (f":{p.port}" if p.port else ""),
            path,
            "",
            urlencode(query),
            "",
        )
    )


def is_item_specific(url: str, listing_urls: list[str]) -> bool:
    """Tell whether ``url`` differs from every listing page and is not just the site root."""
    c = canonical_url(url)
    if urlparse(c).path == "/" and not urlparse(c).query:
        return False
    return c not in {canonical_url(u) for u in listing_urls}


def same_site(url: str, base: str) -> bool:
    """Tell whether both URLs share a host (``www.`` ignored)."""

    def host(u: str) -> str:
        return (urlparse(u).hostname or "").removeprefix("www.")

    return host(url) == host(base)


def title_key(title: str, starts_at: datetime | None, venue_name: str | None, tz: ZoneInfo) -> str:
    """Fallback key: sha1 of normalised title, local start date and venue."""
    day = starts_at.astimezone(tz).date().isoformat() if starts_at else ""
    raw = f"{norm_text(title)}|{day}|{norm_text(venue_name)}"
    return "t:" + hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


def make_dedupe_key(
    *,
    title: str,
    starts_at: datetime | None,
    venue_name: str | None,
    tz: ZoneInfo,
    native_id: str | None = None,
    item_url: str | None = None,
    listing_urls: list[str] | None = None,
) -> str:
    """Key precedence: native id, then item-specific canonical URL, then the title hash."""
    if native_id:
        return f"id:{native_id}"
    if item_url and is_item_specific(item_url, listing_urls or []):
        return "u:" + canonical_url(item_url)
    return title_key(title, starts_at, venue_name, tz)


def to_aware(value: datetime | date, tz: ZoneInfo, *, end: bool = False) -> datetime:
    """Naive datetimes are local time. A bare date is the start of the day, or 23:59:59 for an end."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value
    return datetime.combine(value, time(23, 59, 59) if end else time.min, tzinfo=tz)


def to_utc(value: datetime) -> datetime:
    """UTC, for stable canonical payloads."""
    return value.astimezone(UTC)


def is_date_only(text: str | None) -> bool:
    """Tell whether ``text`` is ``YYYY-MM-DD`` with no time part."""
    return bool(text) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", (text or "").strip()))


def parse_iso(text: str | None, tz: ZoneInfo, *, end: bool = False) -> datetime | None:
    """Parse an ISO date or datetime; date-only values follow ``to_aware``'s rules."""
    if not text:
        return None
    s = text.strip()
    try:
        if is_date_only(s):
            return to_aware(date.fromisoformat(s), tz, end=end)
        return to_aware(datetime.fromisoformat(s.replace("Z", "+00:00")), tz)
    except ValueError:
        return None
