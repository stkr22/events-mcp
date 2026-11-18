from datetime import timedelta
from pathlib import Path

import pytest

from events_mcp.crawl.config import load_sources, parse_interval

from .conftest import make_source


def test_interval_parsing() -> None:
    assert parse_interval("15m") == timedelta(minutes=15)
    assert parse_interval("6h") == timedelta(hours=6)
    assert parse_interval("2d") == timedelta(days=2)
    for bad in ("0h", "soon", "5", "1w"):
        with pytest.raises(ValueError, match="interval"):
            parse_interval(bad)


def test_defaults_applied_and_youtube_resolves_to_feed() -> None:
    s = make_source(type="youtube", url=None, channel_id="UC123")
    assert s.primary_url == "https://www.youtube.com/feeds/videos.xml?playlist_id=UULF123"  # uploads, no Shorts
    assert (s.city, s.interval, s.render, s.extractor) == ("Hamburg", "24h", "http", "youtube")


def test_hosts_default_to_url_hosts_and_egress_overrides() -> None:
    s = make_source(urls=["https://tickets.example/list"])
    assert s.hosts == ["club.example", "tickets.example"]
    assert make_source(egress=["cdn.example"]).hosts == ["cdn.example"]


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "Bad Name"},
        {"channel_id": "UC1"},
        {"type": "youtube", "url": None},
        {"type": "youtube", "url": None, "channel_id": "PLabc"},
        {"type": "youtube", "channel_id": "UC1"},
        {"url": "ftp://x.example"},
        {"surprise": 1},
        {"interval": "daily"},
    ],
)
def test_invalid_entries_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        make_source(**bad)


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text(
        "sources:\n  - {name: a, type: feed, url: 'https://a.example/x'}\n  - {name: a, type: feed, url: 'https://b.example/x'}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_sources(f)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text("sorces: []\n")
    with pytest.raises(ValueError, match="sorces"):
        load_sources(f)
