"""Response models of the admin API (request bodies are the crawler's own ``Source`` and ``Defaults``)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class Me(BaseModel):
    """The signed-in user, or ``authenticated: false``."""

    authenticated: bool
    admin: bool = False
    sub: str | None = None
    name: str | None = None
    email: str | None = None
    oidc: bool = True


class RunView(BaseModel):
    """One crawl of one source."""

    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    extractor: str | None
    llm_provider: str | None
    llm_calls: int
    llm_input_chars: int
    items_seen: int
    items_new: int
    items_updated: int
    items_touched: int
    items_rejected: int


class Effective(BaseModel):
    """What the crawler will actually use once defaults are applied."""

    interval: str
    city: str | None
    timezone: str
    render: str
    extractor: str
    kind: str
    hosts: list[str]
    primary_url: str


class SourceView(BaseModel):
    """A source with its configuration and how it is doing."""

    name: str
    type: str
    url: str
    enabled: bool
    config: dict[str, Any]
    effective: Effective | None
    problem: str | None
    item_count: int
    last_run: RunView | None
    last_success_at: datetime | None
    run_request: Literal["due", "full"] | None
    updated_at: datetime


class SourceDetail(SourceView):
    """A source with its recent runs and changes."""

    runs: list[RunView]
    history: list[HistoryEntry]


class HistoryEntry(BaseModel):
    """One configuration change."""

    id: int
    source_name: str
    action: str
    actor: str
    changed_at: datetime
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class RunRequest(BaseModel):
    """Body of ``POST /sources/{name}/run``."""

    mode: Literal["due", "full"] = "due"


class Options(BaseModel):
    """The choices the forms offer, so the UI never hard-codes the crawler's vocabulary."""

    source_types: list[str]
    renders: list[str]
    extractors: list[str]
    kinds: list[str]
    run_modes: list[str]
