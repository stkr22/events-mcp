"""``sources.yaml`` model and crawler environment settings."""

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlparse

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SourceType = Literal["venue", "museum", "youtube", "feed", "shop"]
Render = Literal["http", "browser", "browser_stealth"]
ExtractorName = Literal["auto", "ical", "rss", "youtube", "jsonld", "selectors", "llm"]
KindName = Literal["event", "exhibition", "video", "sale", "other"]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_INTERVAL = re.compile(r"^(\d+)\s*([mhd])$")
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def parse_interval(value: str) -> timedelta:
    """``15m``, ``6h`` or ``2d`` as a timedelta."""
    m = _INTERVAL.match(value.strip().lower())
    if not m:
        raise ValueError(f"interval must look like 30m, 6h or 2d, got {value!r}")
    n, unit = int(m.group(1)), m.group(2)
    if n < 1:
        raise ValueError("interval must be positive")
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Politeness(_Strict):
    """Per-host request pacing and robots handling."""

    rps: Annotated[float, Field(gt=0, le=10)] = 1.0
    ignore_robots: bool = False


class Venue(_Strict):
    """A fixed venue for single-venue sources (no geocoding needed)."""

    name: str
    city: str | None = None
    address: str | None = None
    lat: Annotated[float, Field(ge=-90, le=90)] | None = None
    lon: Annotated[float, Field(ge=-180, le=180)] | None = None

    @model_validator(mode="after")
    def _both_coordinates(self) -> Self:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon go together")
        return self


class Defaults(_Strict):
    """Values every source inherits unless it sets its own."""

    city: str | None = None
    timezone: str = "Europe/Berlin"
    interval: str = "24h"
    render: Render = "http"
    extractor: ExtractorName = "auto"
    politeness: Politeness = Politeness()

    @field_validator("interval")
    @classmethod
    def _interval(cls, v: str) -> str:
        parse_interval(v)
        return v


class Source(_Strict):
    """One entry of ``sources.yaml`` with defaults applied."""

    name: str
    type: SourceType
    url: str | None = None
    urls: list[str] = []
    channel_id: str | None = None
    max_pages: Annotated[int, Field(ge=1, le=20)] = 1
    interval: str | None = None
    city: str | None = None
    timezone: str | None = None
    render: Render | None = None
    extractor: ExtractorName | None = None
    politeness: Politeness | None = None
    default_kind: KindName | None = None
    intent: str | None = None
    venue: Venue | None = None
    egress: list[str] = []
    ignore_patterns: list[str] = []
    wait_for_selector: str | None = None
    max_llm_calls: int = 10
    max_input_chars: int = 150_000
    timeout_sec: Annotated[int, Field(ge=30, le=3000)] | None = None
    """Hard limit for one run of this source; default is the crawler-wide 300 s."""

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("name must be a lowercase slug (a-z, 0-9, -)")
        return v

    @field_validator("interval")
    @classmethod
    def _interval(cls, v: str | None) -> str | None:
        if v is not None:
            parse_interval(v)
        return v

    @field_validator("url", "urls")
    @classmethod
    def _http(cls, v: str | list[str] | None) -> str | list[str] | None:
        for u in [v] if isinstance(v, str) else (v or []):
            if urlparse(u).scheme not in {"http", "https"} or not urlparse(u).hostname:
                raise ValueError(f"not an http(s) URL: {u!r}")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.type == "youtube") != (self.channel_id is not None):
            raise ValueError("channel_id is required for, and only valid on, type: youtube")
        if self.type == "youtube" and self.url:
            raise ValueError("a youtube source takes channel_id, not url")
        if self.type != "youtube" and not self.url:
            raise ValueError("url is required")
        return self


class ResolvedSource(Source):
    """A source with every default filled in."""

    interval: str  # ty: ignore[incompatible-mutable-override]
    render: Render  # ty: ignore[incompatible-mutable-override]
    extractor: ExtractorName  # ty: ignore[incompatible-mutable-override]
    politeness: Politeness  # ty: ignore[incompatible-mutable-override]
    timezone: str  # ty: ignore[incompatible-mutable-override]

    @property
    def primary_url(self) -> str:
        """The URL stored in ``sources.url`` (unique per source)."""
        if self.type == "youtube":
            assert self.channel_id is not None  # noqa: S101 - validated
            return YOUTUBE_FEED.format(channel_id=self.channel_id)
        assert self.url is not None  # noqa: S101 - validated
        return self.url

    @property
    def fetch_urls(self) -> list[str]:
        """Listing pages in order, capped at ``max_pages``."""
        return [self.primary_url, *self.urls][: self.max_pages]

    @property
    def interval_delta(self) -> timedelta:
        """``interval`` as a timedelta."""
        return parse_interval(self.interval)

    @property
    def hosts(self) -> list[str]:
        """Hosts this source may reach: ``egress`` if given, else those of its URLs."""
        if self.egress:
            return sorted(set(self.egress))
        return sorted({urlparse(u).hostname or "" for u in [self.primary_url, *self.urls]})


class SourcesFile(_Strict):
    """The whole file."""

    defaults: Defaults = Defaults()
    sources: list[Source] = []

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [s.name for s in self.sources]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate source names: {', '.join(dupes)}")
        return self

    def resolve(self) -> list[ResolvedSource]:
        """Apply ``defaults`` to every source."""
        d = self.defaults
        out = []
        for s in self.sources:
            data = s.model_dump()
            for key in ("city", "timezone", "interval", "render", "extractor", "politeness"):
                if data[key] is None:
                    data[key] = getattr(d, key)
            if s.type == "youtube":
                data["extractor"] = "youtube" if data["extractor"] == "auto" else data["extractor"]
            out.append(ResolvedSource.model_validate(data))
        return out


def load_sources(path: Path) -> list[ResolvedSource]:
    """Read and validate ``sources.yaml``; raises ``ValueError`` with every problem listed."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SourcesFile.model_validate(raw).resolve()


class CrawlerSettings(BaseSettings):
    """Crawler environment (plain names, as in the deployment; no ``EVENTS_`` prefix)."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    database_url: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DATABASE_URL", "EVENTS_DATABASE_URL")
    )
    sources_file: Path = Path("/config/sources.yaml")
    user_agent: str = "events-mcp-crawler (+https://github.com/stkr22/events-mcp)"
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"
    geocode: bool = True
    source_timeout_sec: float = 300.0
    max_concurrency: int = 4
    horizon_days: int = 120

    cdp_url: str | None = None
    """Chrome DevTools endpoint for render: browser. Must be an IP: Chrome rejects other Host headers."""

    llm_provider_order: str = "mac,gemini"
    llm_mac_base_url: str | None = None
    llm_mac_model: str | None = None
    llm_gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_gemini_model: str | None = None
    gemini_api_key: SecretStr | None = None
    llm_mac_timeout_sec: float = 180.0
    llm_gemini_timeout_sec: float = 60.0
    llm_chunk_chars: int = 24_000

    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "garage"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    snapshot_retention_days: int = 90
