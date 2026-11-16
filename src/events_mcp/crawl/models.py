"""Items as extractors produce them, and the page they came from."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["scheduled", "cancelled", "postponed", "rescheduled"]
Kind = Literal["event", "exhibition", "video", "sale", "other"]
RAW_TEXT_LIMIT = 20_000


class VenueRef(BaseModel):
    """Venue as stated by the source; resolved to a ``venues`` row later."""

    model_config = ConfigDict(extra="ignore")

    name: str
    city: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None


class RawItem(BaseModel):
    """One listing before keying. Extractors fill what they know; the pipeline computes ``dedupe_key``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=500)
    kind: Kind
    summary: str | None = None
    raw_text: str | None = None
    url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: Status | None = None
    venue: VenueRef | None = None
    native_id: str | None = None
    """Stable id from the source (iCal UID + occurrence, videoId, JSON-LD @id); beats every other key."""

    @field_validator("title")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("raw_text")
    @classmethod
    def _cap(cls, v: str | None) -> str | None:
        return v[:RAW_TEXT_LIMIT] if v else v

    def canonical(self) -> dict[str, Any]:
        """Stable dict for hashing structured payloads."""
        return self.model_dump(mode="json", exclude_none=True)


@dataclass
class Page:
    """One fetched document."""

    url: str
    status: int
    content_type: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_html(self) -> bool:
        """True for HTML / XHTML responses."""
        return "html" in self.content_type


@dataclass
class Extraction:
    """What a tier found. ``payload_hash_input`` is the canonical text structured tiers hash."""

    extractor: str
    items: list[RawItem]
    rejected: int = 0


def canonical_payload(items: list[RawItem]) -> str:
    """Deterministic JSON of the items, sorted, for hashing structured-tier results."""
    rows = sorted((i.canonical() for i in items), key=lambda d: json.dumps(d, sort_keys=True))
    return json.dumps(rows, sort_keys=True, ensure_ascii=False)
