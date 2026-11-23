import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from events_mcp.crawl.browser import goto_with_retry
from events_mcp.crawl.config import DEFAULT_NAV_TIMEOUT_SEC

from .conftest import make_source


class FakePage:
    """Times out on the first ``fails`` navigations."""

    def __init__(self, fails: int, load_state_times_out: bool = False) -> None:
        self.fails, self.calls, self.load_state_times_out = fails, [], load_state_times_out

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:  # noqa: ASYNC109
        self.calls.append((wait_until, timeout))
        if len(self.calls) <= self.fails:
            raise PlaywrightTimeout(f'Page.goto: Timeout {timeout}ms exceeded.\n  waiting until "{wait_until}"')
        return "response"

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:  # noqa: ASYNC109
        if self.load_state_times_out:
            raise PlaywrightTimeout("load state")


async def test_a_fast_page_navigates_once() -> None:
    page = FakePage(fails=0)
    assert await goto_with_retry(page, "https://x.example/", 45_000, pause_sec=0) == "response"
    assert page.calls == [("domcontentloaded", 45_000)]


async def test_a_timeout_is_retried_once_with_commit_and_twice_the_time() -> None:
    page = FakePage(fails=1)
    assert await goto_with_retry(page, "https://x.example/", 30_000, pause_sec=0) == "response"
    assert page.calls == [("domcontentloaded", 30_000), ("commit", 60_000)]


async def test_a_committed_page_that_never_finishes_parsing_is_still_used() -> None:
    page = FakePage(fails=1, load_state_times_out=True)
    assert await goto_with_retry(page, "https://x.example/", 30_000, pause_sec=0) == "response"


async def test_two_timeouts_fail_the_navigation() -> None:
    with pytest.raises(PlaywrightTimeout):
        await goto_with_retry(FakePage(fails=2), "https://x.example/", 30_000, pause_sec=0)


def test_the_navigation_timeout_is_a_source_setting() -> None:
    assert make_source().nav_timeout_sec is None and DEFAULT_NAV_TIMEOUT_SEC == 30
    assert make_source(nav_timeout_sec=90).nav_timeout_sec == 90
    with pytest.raises(ValueError, match="nav_timeout_sec"):
        make_source(nav_timeout_sec=5)
