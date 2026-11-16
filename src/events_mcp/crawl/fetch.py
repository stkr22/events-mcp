"""HTTP fetching with per-host pacing, robots.txt and challenge-page detection."""

import asyncio
import logging
import time
from urllib.parse import urlparse

import httpx
from protego import Protego

from events_mcp.crawl.models import Page

log = logging.getLogger(__name__)

MAX_BYTES = 8_000_000
_CHALLENGE_MARKERS = (
    "just a moment...",
    "cf-browser-verification",
    "cf-challenge",
    "attention required! | cloudflare",
    "_incapsula_resource",
    "akamai-bot",
    "px-captcha",
    "establishing a secure connection",
    "shield-challenge",
    "captcha-delivery",
)


class RobotsBlockedError(Exception):
    """The URL is disallowed by robots.txt for our user agent."""


class FetchError(Exception):
    """The page could not be fetched (network error, bad status, challenge page, too large)."""


def looks_like_challenge(text: str) -> bool:
    """Obvious bot-protection interstitials; only inspected on short HTML bodies."""
    head = text[:6000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS)


class Fetcher:
    """Shared by one crawl run: one HTTP client, one token bucket and one robots cache per host."""

    def __init__(self, user_agent: str, timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        """Create the client lazily-safe; pass ``client`` in tests to inject a mock transport."""
        self.user_agent = user_agent
        self._client = client or httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept-Language": "de-DE,de;q=0.9,en;q=0.7"},
        )
        self._next_slot: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, Protego | None] = {}

    async def aclose(self) -> None:
        """Close the client."""
        await self._client.aclose()

    async def _pace(self, host: str, rps: float) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait = self._next_slot.get(host, 0.0) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot[host] = max(now, self._next_slot.get(host, 0.0)) + 1.0 / rps

    async def _allowed(self, url: str, rps: float) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            robots_url = f"{origin}/robots.txt"
            await self._pace(p.netloc, rps)
            try:
                resp = await self._client.get(robots_url)
            except httpx.HTTPError as exc:
                # Unreachable robots.txt: treat as allow-all, like crawlers conventionally do.
                log.info("robots.txt unreachable for %s: %s", origin, exc)
                self._robots[origin] = None
            else:
                if resp.status_code in {401, 403}:
                    # Access denied to robots.txt is read as "disallow all" (RFC 9309 section 2.3.1.3).
                    self._robots[origin] = Protego.parse("User-agent: *\nDisallow: /")
                elif resp.status_code >= 400:  # noqa: PLR2004 - no robots.txt means everything is allowed
                    self._robots[origin] = None
                else:
                    self._robots[origin] = Protego.parse(resp.text)
        rules = self._robots[origin]
        return rules is None or rules.can_fetch(url, self.user_agent)

    async def check(self, url: str, *, rps: float, ignore_robots: bool) -> None:
        """Raise ``RobotsBlockedError`` if robots.txt disallows ``url``; pace the request either way."""
        if not ignore_robots and not await self._allowed(url, rps):
            raise RobotsBlockedError(url)
        await self._pace(urlparse(url).netloc, rps)

    async def get(
        self,
        url: str,
        *,
        rps: float = 1.0,
        ignore_robots: bool = False,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Page | None:
        """Fetch ``url``. Returns ``None`` on 304 (conditional request). Raises on every other failure."""
        if not ignore_robots and not await self._allowed(url, rps):
            raise RobotsBlockedError(url)
        await self._pace(urlparse(url).netloc, rps)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 304:  # noqa: PLR2004
            return None
        if resp.status_code in {403, 429}:
            raise FetchError(f"HTTP {resp.status_code} (blocked or rate limited): {url}")
        if resp.status_code >= 400:  # noqa: PLR2004
            raise FetchError(f"HTTP {resp.status_code}: {url}")
        if len(resp.content) > MAX_BYTES:
            raise FetchError(f"response larger than {MAX_BYTES} bytes: {url}")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        text = resp.text
        if "html" in ctype and looks_like_challenge(text) and len(text) < 60_000:  # noqa: PLR2004
            raise FetchError(f"bot-protection challenge page: {url}")
        return Page(
            url=str(resp.url), status=resp.status_code, content_type=ctype, text=text, headers=dict(resp.headers)
        )
