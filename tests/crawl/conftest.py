from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from events_mcp.crawl.config import ResolvedSource, SourcesFile
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.models import Page

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Berlin")


def make_source(**kw: object) -> ResolvedSource:
    base: dict[str, object] = {"name": "club", "type": "venue", "url": "https://club.example/events"}
    base.update(kw)
    return SourcesFile.model_validate({"defaults": {"city": "Hamburg"}, "sources": [base]}).resolve()[0]


def page(name: str, content_type: str, url: str = "https://club.example/events") -> Page:
    return Page(url=url, status=200, content_type=content_type, text=(FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ctx() -> Context:
    return Context(make_source(), TZ, NOW)
