"""JS and bot-protected pages through a shared Chrome over CDP, one isolated context per fetch.

Chrome's launch flags belong to its own deployment and are never touched here: the stealth is
per context (user agent, locale, timezone, init scripts).
"""

import asyncio
import logging
from typing import Any

from events_mcp.crawl.fetch import FetchError, looks_like_challenge
from events_mcp.crawl.models import Page

log = logging.getLogger(__name__)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['de-DE', 'de', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5].map(() => ({name: 'Chrome PDF Plugin'}))});
window.chrome = window.chrome || {runtime: {}};
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, p);
};
"""

NETWORK_IDLE_CAP_MS = 10_000
CHALLENGE_WAIT_SEC = 20


class BrowserFetcher:
    """At most one context at a time from this process: the Chrome is shared and small."""

    def __init__(self, cdp_url: str, user_agent: str | None = None, timezone: str = "Europe/Berlin") -> None:
        """``cdp_url`` is ``http://<ip>:9222``; Chrome refuses hostnames in the Host header."""
        self.cdp_url = cdp_url
        self.user_agent = user_agent
        self.timezone = timezone
        self._gate = asyncio.Semaphore(1)

    async def get(self, url: str, *, stealth: bool, wait_for_selector: str | None = None) -> Page:  # noqa: PLR0912
        """Render ``url`` and return the resulting DOM as a ``Page``."""
        from playwright.async_api import Error as PlaywrightError  # noqa: PLC0415
        from playwright.async_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415
        from playwright.async_api import async_playwright  # noqa: PLC0415

        async with self._gate, async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(self.cdp_url, timeout=15_000)
            except PlaywrightError as exc:
                raise FetchError(f"cannot reach the browser at {self.cdp_url}: {exc}") from exc
            context: Any = None
            try:
                context = await browser.new_context(
                    locale="de-DE",
                    timezone_id=self.timezone,
                    user_agent=self.user_agent,
                    viewport={"width": 1440, "height": 900},
                )
                if stealth:
                    await context.add_init_script(STEALTH_JS)
                page = await context.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    with_idle = page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_CAP_MS)
                    try:
                        await with_idle
                    except PlaywrightTimeout:
                        log.info("%s: network never went idle, using what is there", url)
                    if wait_for_selector:
                        await page.wait_for_selector(wait_for_selector, timeout=10_000)
                    # Proof-of-work interstitials resolve themselves in a real browser: wait for the real page.
                    for _ in range(CHALLENGE_WAIT_SEC):
                        html = await page.content()
                        if not looks_like_challenge(html):
                            break
                        await asyncio.sleep(1)
                    else:
                        raise FetchError(f"bot-protection challenge page did not resolve: {url}")
                    status = resp.status if resp else 200
                    if status in {403, 429}:
                        raise FetchError(f"HTTP {status} (blocked or rate limited): {url}")
                    if status >= 400:  # noqa: PLR2004
                        raise FetchError(f"HTTP {status}: {url}")
                    return Page(url=page.url, status=status, content_type="text/html", text=html)
                except PlaywrightTimeout as exc:
                    raise FetchError(f"timeout rendering {url}: {exc}") from exc
                except PlaywrightError as exc:
                    raise FetchError(f"browser error on {url}: {exc}") from exc
            finally:
                if context is not None:
                    await context.close()
                await browser.close()
