"""Extraction tiers, cheapest first. Each returns ``None`` when it does not apply to the pages."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from events_mcp.crawl.config import ResolvedSource


@dataclass
class Context:
    """What every extractor needs besides the pages."""

    source: ResolvedSource
    tz: ZoneInfo
    now: datetime
    horizon_days: int = 120

    @property
    def default_kind(self) -> str:
        """The source's ``default_kind`` or ``event``."""
        return self.source.default_kind or "event"
