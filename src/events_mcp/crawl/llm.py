"""LLM extraction: provider chain (Mac first, Gemini as fallback), structured output, repair, guards.

The model only writes content fields. Keys, validation and the database stay in code: a title that
is not on the page, an impossible date or a foreign URL is dropped, however confident the model was.
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from rapidfuzz import fuzz

from events_mcp.crawl.config import CrawlerSettings, ResolvedSource
from events_mcp.crawl.models import RawItem, VenueRef
from events_mcp.crawl.normalize import norm_text, parse_iso, same_site

log = logging.getLogger(__name__)

KINDS = ("event", "exhibition", "video", "sale", "other")
TITLE_MATCH_MIN = 90
MAX_URL = 2000


class ProviderUnavailableError(Exception):
    """The endpoint did not answer (asleep, timeout, 5xx, network). Try the next provider."""


class BadOutputError(Exception):
    """The model answered but not with usable JSON, even after one repair attempt."""


class NoProviderError(Exception):
    """Every configured provider is unavailable: the run becomes ``pending_extraction``."""


class BudgetExceededError(Exception):
    """The per-run LLM budget (calls or input characters) is used up."""


class LlmVenue(BaseModel):
    """Venue as the model reports it."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    address: str | None = None
    city: str | None = None


class LlmItem(BaseModel):
    """One listing as the model reports it. Everything but ``title`` is optional: omit rather than guess."""

    model_config = ConfigDict(extra="ignore")

    title: str
    kind: str | None = None
    summary: str | None = None
    raw_text: str | None = None
    url: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    status: Literal["scheduled", "cancelled", "postponed", "rescheduled"] | None = None
    registration_opens_at: str | None = None
    registration_closes_at: str | None = None
    tickets_url: str | None = None
    venue: LlmVenue | None = None


class LlmResult(BaseModel):
    """The whole answer."""

    model_config = ConfigDict(extra="ignore")

    items: list[LlmItem]


class ChatClient(Protocol):
    """One OpenAI-compatible endpoint."""

    name: str
    model: str

    async def available(self) -> bool:
        """Cheap liveness check."""
        ...

    async def complete(self, messages: list[dict[str, str]], schema: dict[str, Any], *, json_schema: bool) -> str:
        """Return the raw assistant text. Raises ``ProviderUnavailableError`` on transport trouble."""
        ...


class OpenAIChat:
    """``openai`` SDK against any OpenAI-compatible base URL."""

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float,
        *,
        health_check: bool,
        client: Any = None,
    ) -> None:
        """``client`` is injectable for tests; ``health_check`` probes ``GET /models`` first (Mac may sleep)."""
        from openai import AsyncOpenAI  # noqa: PLC0415 - heavy import, only when an LLM is configured

        self.name = name
        self.model = model
        self._health = health_check
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = client or AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    async def available(self) -> bool:
        """3 s ``GET /models`` for providers that can be asleep; others are assumed up."""
        if not self._health:
            return True
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(f"{self._base}/models", headers={"Authorization": f"Bearer {self._key}"})
            return resp.status_code < 500  # noqa: PLR2004
        except httpx.HTTPError:
            return False

    async def complete(self, messages: list[dict[str, str]], schema: dict[str, Any], *, json_schema: bool) -> str:
        """One chat completion, structured as ``json_schema`` or plain JSON mode."""
        import openai  # noqa: PLC0415

        fmt: dict[str, Any] = (
            {"type": "json_schema", "json_schema": {"name": "events", "strict": True, "schema": schema}}
            if json_schema
            else {"type": "json_object"}
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # ty: ignore[invalid-argument-type]
                response_format=fmt,  # ty: ignore[invalid-argument-type]
                temperature=0,
            )
        except openai.BadRequestError:
            raise
        except (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.InternalServerError,
            openai.RateLimitError,
        ) as exc:
            raise ProviderUnavailableError(f"{self.name}: {type(exc).__name__}") from exc
        return resp.choices[0].message.content or ""


def build_chain(settings: CrawlerSettings) -> list[ChatClient]:
    """Build the providers in ``LLM_PROVIDER_ORDER`` that are fully configured; skip the others."""
    chain: list[ChatClient] = []
    for name in (n.strip() for n in settings.llm_provider_order.split(",") if n.strip()):
        if name == "mac" and settings.llm_mac_base_url and settings.llm_mac_model:
            chain.append(
                OpenAIChat(
                    "mac",
                    settings.llm_mac_base_url,
                    settings.llm_mac_model,
                    "local",
                    settings.llm_mac_timeout_sec,
                    health_check=True,
                )
            )
        elif name == "gemini" and settings.gemini_api_key and settings.llm_gemini_model:
            chain.append(
                OpenAIChat(
                    "gemini",
                    settings.llm_gemini_base_url,
                    settings.llm_gemini_model,
                    settings.gemini_api_key.get_secret_value(),
                    settings.llm_gemini_timeout_sec,
                    health_check=False,
                )
            )
    return chain


SYSTEM = (
    "You extract listings (events, exhibitions, videos, sales) from the text of one web page. "
    "Rules: extract EVERY listing on the page, do not skip or merge any, but only items that are explicitly "
    "on the page; never invent. Omit any field you are not sure of. "
    "`title` is copied from the page. `raw_text` is the verbatim excerpt of the page about that item. "
    "Dates are ISO-8601 in local time (YYYY-MM-DD or YYYY-MM-DDTHH:MM); a date without a year takes the "
    "nearest plausible year given today's date. `kind` is one of: event, exhibition, video, sale, other. "
    "`starts_at` and `ends_at` are when the item itself takes place, never a ticket or registration date. "
    "Registration or ticket sales come before the item and are separate dates. `registration_opens_at` is when "
    'sales or registration START ("Tickets ab 1. Dezember", "Vorverkauf startet am 10.11.", "presale from", '
    '"registration opens"). `registration_closes_at` is the deadline ("Anmeldung bis 30. November", '
    '"register by", "Anmeldeschluss"). Fill each one the page states, either alone is fine, and never '
    "invent the other. `tickets_url` is the link to buy or register. Omit all three when the page says nothing. "
    "`status` only if the page says cancelled, postponed or rescheduled. Answer with one JSON object "
    '{"items": [...]} and nothing else. The page may be German or English.'
)


def build_prompt(source: ResolvedSource, text: str, today: datetime, tz: ZoneInfo, schema: dict[str, Any]) -> str:
    """User message: source context, today's date, the schema and the page chunk."""
    parts = [
        f"Source: {source.name} (type {source.type}). Default kind: {source.kind}.",
        f"Today is {today.astimezone(tz):%A %Y-%m-%d}, timezone {source.timezone}.",
    ]
    if source.intent:
        parts.append(f"What matters: {' '.join(source.intent.split())}")
    parts.append(f"JSON schema of the answer:\n{json.dumps(schema)}")
    parts.append(f"Page text:\n\n{text}")
    return "\n\n".join(parts)


_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


def chunk_text(text: str, limit: int) -> list[str]:
    """Split at headings (then blank lines, then hard) into pieces of at most ``limit`` characters."""
    if len(text) <= limit:
        return [text]
    starts = [m.start() for m in _HEADING.finditer(text)]
    sections = [text[a:b] for a, b in zip([0, *starts], [*starts, len(text)], strict=False) if text[a:b].strip()]
    pieces: list[str] = []
    for section in sections:
        rest = section
        while len(rest) > limit:
            cut = rest.rfind("\n\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(rest[:cut])
            rest = rest[cut:]
        pieces.append(rest)
    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + len(piece) <= limit:
            chunks[-1] += piece
        else:
            chunks.append(piece)
    return chunks


@dataclass
class Usage:
    """What a run spent, for ``scrape_runs``."""

    calls: int = 0
    input_chars: int = 0
    provider: str | None = None
    notes: list[str] = field(default_factory=list)


def _tickets_url(value: str | None, page_text: str, page_url: str) -> str | None:
    """Return a ticket or registration link: this site, or another one only if the page itself shows it.

    Ticket sellers live on other domains, so the same-site rule for item links does not fit; a link the
    model made up cannot appear in the page text.
    """
    if not value:
        return None
    url = urljoin(page_url, value.strip())
    if not url.startswith(("http://", "https://")) or len(url) > MAX_URL:
        return None
    if same_site(url, page_url) or url.rstrip("/") in page_text:
        return url
    return None


def apply_guards(
    items: list[LlmItem], page_text: str, source: ResolvedSource, tz: ZoneInfo, page_url: str
) -> tuple[list[RawItem], int]:
    """Turn model output into ``RawItem``s, dropping what fails the code-side checks. Returns ``(items, rejected)``."""
    haystack = norm_text(page_text)
    out: list[RawItem] = []
    rejected = 0
    for it in items:
        title = " ".join(it.title.split())
        if not title or fuzz.partial_ratio(norm_text(title), haystack) < TITLE_MATCH_MIN:
            rejected += 1  # not on the page: hallucinated or paraphrased
            continue
        starts = parse_iso(it.starts_at, tz)
        ends = parse_iso(it.ends_at, tz, end=True)
        if (it.starts_at and starts is None) or (it.ends_at and ends is None):
            rejected += 1
            continue
        if starts and ends and ends < starts:
            rejected += 1
            continue
        url = urljoin(page_url, it.url) if it.url else None
        if url and (not url.startswith("http") or not same_site(url, page_url)):
            url = None
        opens = parse_iso(it.registration_opens_at, tz)
        closes = parse_iso(it.registration_closes_at, tz, end=True)
        if opens and closes and closes < opens:
            opens = closes = None
        tickets = _tickets_url(it.tickets_url, page_text, page_url)
        kind = it.kind if it.kind in KINDS else source.kind
        venue = (
            VenueRef(name=it.venue.name, address=it.venue.address, city=it.venue.city)
            if it.venue and it.venue.name
            else None
        )
        out.append(
            RawItem(
                title=title,
                kind=kind,  # ty: ignore[invalid-argument-type]
                summary=(it.summary or "")[:500] or None,
                raw_text=it.raw_text,
                url=url,
                starts_at=starts,
                ends_at=ends,
                status=it.status,
                registration_opens_at=opens,
                registration_closes_at=closes,
                tickets_url=tickets,
                venue=venue,
            )
        )
    return out, rejected


async def _ask[T: BaseModel](
    client: ChatClient,
    system: str,
    user: str,
    schema: dict[str, Any],
    model: type[T] = LlmResult,  # ty: ignore[invalid-parameter-default]
) -> T:
    """One chunk against one provider: json_schema first, JSON mode if rejected, one repair retry."""
    import openai  # noqa: PLC0415

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    use_schema = True
    last_error = ""
    for attempt in range(2):  # the answer, then one repair
        try:
            raw = await client.complete(messages, schema, json_schema=use_schema)
        except openai.BadRequestError:
            if not use_schema:
                raise
            use_schema = False  # the endpoint does not take json_schema: JSON mode plus validation
            raw = await client.complete(messages, schema, json_schema=False)
        try:
            return model.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:500]
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"That was not valid for the schema: {last_error}\nAnswer again with only the corrected JSON."
                    ),
                },
            ]
            log.info("%s: invalid output (attempt %d): %s", client.name, attempt + 1, last_error)
    raise BadOutputError(f"{client.name}: {last_error}")


async def extract_with_llm(
    chain: Sequence[ChatClient],
    source: ResolvedSource,
    text: str,
    page_url: str,
    tz: ZoneInfo,
    now: datetime,
    chunk_chars: int,
) -> tuple[list[RawItem], int, Usage]:
    """Run every chunk through the chain. Returns ``(items, rejected, usage)``.

    Raises ``NoProviderError`` if no provider answered at all (nothing was extracted, nothing is
    touched), and ``BadOutputError`` if providers answered but never with usable JSON.
    """
    schema = LlmResult.model_json_schema()
    usage = Usage()
    gathered: list[LlmItem] = []
    chunks = chunk_text(text, chunk_chars)
    for n, chunk in enumerate(chunks):
        if usage.calls >= source.max_llm_calls or usage.input_chars + len(chunk) > source.max_input_chars:
            usage.notes.append(f"LLM budget used up: {len(chunks) - n} of {len(chunks)} chunks not read")
            break
        user = build_prompt(source, chunk, now, tz, schema)
        done = False
        bad: BadOutputError | None = None
        for client in chain:
            if not await client.available():
                log.info("%s unavailable, trying the next provider", client.name)
                continue
            try:
                usage.calls += 1
                usage.input_chars += len(chunk)
                result = await _ask(client, SYSTEM, user, schema)
            except ProviderUnavailableError as exc:
                log.info("%s", exc)
                continue
            except BadOutputError as exc:
                bad = exc
                continue
            usage.provider = client.name
            gathered.extend(result.items)
            done = True
            break
        if not done:
            raise bad or NoProviderError("no LLM provider is available")
    items, rejected = apply_guards(gathered, text, source, tz, page_url)
    return items, rejected, usage


async def ask_model[T: BaseModel](chain: Sequence[ChatClient], system: str, user: str, model: type[T]) -> tuple[T, str]:
    """One structured question through the chain. Returns ``(answer, provider name)``.

    Same fallthrough as ``extract_with_llm``: unavailable providers are skipped, unusable output moves
    on to the next provider, and ``NoProviderError`` / ``BadOutputError`` say why nothing came back.
    """
    schema = model.model_json_schema()
    bad: BadOutputError | None = None
    for client in chain:
        if not await client.available():
            continue
        try:
            return await _ask(client, system, user, schema, model), client.name
        except ProviderUnavailableError:
            continue
        except BadOutputError as exc:
            bad = exc
    raise bad or NoProviderError("no LLM provider is available")
