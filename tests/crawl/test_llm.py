import httpx
import openai
import pytest

from events_mcp.crawl.llm import (
    BadOutputError,
    LlmItem,
    NoProviderError,
    ProviderUnavailableError,
    apply_guards,
    chunk_text,
    extract_with_llm,
)

from .conftest import NOW, TZ, make_source
from .fakes import FakeChat

PAGE = """# Programm

## Sa. 21.11. Konzert Nordlicht
Die Band Nordlicht spielt um 20 Uhr in der Halle. Eintritt frei.

## Ausstellung Salz und Stein
Bis 14.02.2027 im Foyer, täglich geöffnet.
"""
URL = "https://venue.example/programm"
GOOD = {
    "items": [
        {
            "title": "Konzert Nordlicht",
            "kind": "event",
            "starts_at": "2026-11-21T20:00",
            "raw_text": "Die Band Nordlicht spielt",
        },
        {"title": "Salz und Stein", "kind": "exhibition", "starts_at": "2026-11-01", "ends_at": "2027-02-14"},
    ]
}


def src(**kw: object):
    return make_source(url=URL, type="venue", **kw)


async def run(chain: list[FakeChat], text: str = PAGE, **kw: object):
    return await extract_with_llm(chain, src(**kw), text, URL, TZ, NOW, 24_000)


async def test_happy_path_uses_the_first_provider() -> None:
    mac, gemini = FakeChat("mac", [GOOD]), FakeChat("gemini", [])
    items, rejected, usage = await run([mac, gemini])
    assert [i.title for i in items] == ["Konzert Nordlicht", "Salz und Stein"]
    assert items[1].ends_at.isoformat() == "2027-02-14T23:59:59+01:00"  # type: ignore[union-attr]
    assert (rejected, usage.provider, usage.calls) == (0, "mac", 1)
    assert gemini.calls == []
    prompt = mac.calls[0][1]["content"]
    assert "Today is Monday 2026-11-16" in prompt and "Page text:" in prompt


async def test_mac_asleep_falls_back_to_gemini() -> None:
    mac, gemini = FakeChat("mac", [], up=False), FakeChat("gemini", [GOOD])
    _, _, usage = await run([mac, gemini])
    assert usage.provider == "gemini" and mac.calls == []


async def test_mac_failing_midway_falls_back() -> None:
    mac, gemini = FakeChat("mac", [ProviderUnavailableError("mac: timeout")]), FakeChat("gemini", [GOOD])
    _, _, usage = await run([mac, gemini])
    assert usage.provider == "gemini"


async def test_nobody_available_is_pending_not_failure() -> None:
    with pytest.raises(NoProviderError):
        await run([FakeChat("mac", [], up=False), FakeChat("gemini", [], up=False)])
    with pytest.raises(NoProviderError):
        await run([])


async def test_invalid_json_gets_one_repair() -> None:
    mac = FakeChat("mac", ["not json at all", GOOD])
    items, _, _ = await run([mac])
    assert len(items) == 2 and len(mac.calls) == 2
    assert "not valid for the schema" in mac.calls[1][-1]["content"]


async def test_unrepairable_output_tries_the_next_provider_then_fails() -> None:
    mac, gemini = FakeChat("mac", ["x", "y"]), FakeChat("gemini", ["x", "y"])
    with pytest.raises(BadOutputError):
        await run([mac, gemini])
    assert len(gemini.calls) == 2


async def test_json_schema_rejection_falls_back_to_json_mode() -> None:
    req = httpx.Request("POST", "http://x")
    bad = openai.BadRequestError("no json_schema", response=httpx.Response(400, request=req), body=None)
    mac = FakeChat("mac", [bad, GOOD])
    items, _, _ = await run([mac])
    assert len(items) == 2 and mac.formats == [True, False]


async def test_hallucinated_titles_and_bad_values_are_rejected_and_counted() -> None:
    answer = {
        "items": [
            *GOOD["items"],
            {"title": "Weltraum Gala mit Bläserquartett", "starts_at": "2026-12-01"},  # not on the page
            {"title": "Konzert Nordlicht", "starts_at": "1999-01-01"},  # implausible year, dedupes later
            {"title": "Salz und Stein", "starts_at": "2026-12-01", "ends_at": "2026-11-01"},  # ends before start
            {"title": "Salz und Stein", "starts_at": "kommenden Samstag"},  # unparseable date
        ]
    }
    items, rejected, _ = await run([FakeChat("mac", [answer])])
    assert len(items) == 3  # 1999 survives apply_guards; the pipeline's plausibility window drops it
    assert rejected == 3


def test_guards_normalise_kind_and_urls() -> None:
    raw = [
        LlmItem(title="Konzert Nordlicht", kind="party", url="/e/1"),
        LlmItem(title="Salz und Stein", url="https://tickets.other.example/x"),
        LlmItem(title="Konzert Nordlicht", url="javascript:alert(1)"),
    ]
    items, rejected = apply_guards(raw, PAGE, src(default_kind="exhibition"), TZ, URL)
    assert rejected == 0
    assert [i.kind for i in items] == ["exhibition"] * 3  # unknown kind falls to default_kind
    assert [i.url for i in items] == ["https://venue.example/e/1", None, None]


async def test_budget_stops_reading_and_notes_it() -> None:
    text = "\n\n".join(f"## Block {i}\n" + ("Konzert Nordlicht " * 400) for i in range(6))
    mac = FakeChat("mac", [{"items": []}] * 10)
    _, _, usage = await extract_with_llm([mac], src(max_llm_calls=2), text, URL, TZ, NOW, 9_000)
    assert usage.calls == 2 and "budget used up" in usage.notes[0]


def test_chunking_prefers_headings_and_respects_the_limit() -> None:
    text = "\n".join(f"## H{i}\n" + "x" * 900 for i in range(10))
    chunks = chunk_text(text, 3000)
    assert all(len(c) <= 3000 for c in chunks) and "".join(chunks) == text
    assert all(c.lstrip().startswith("## H") for c in chunks)
    assert chunk_text("short", 3000) == ["short"]
    huge = "y" * 7000
    assert [len(c) for c in chunk_text(huge, 3000)] == [3000, 3000, 1000]


def test_guards_keep_registration_and_only_drop_what_is_wrong() -> None:
    page = PAGE + "\nTickets: https://tickets.other.example/nordlicht\n"
    raw = [
        LlmItem(
            title="Konzert Nordlicht",
            registration_opens_at="2026-11-01",
            registration_closes_at="2026-11-20T18:00",
            tickets_url="https://tickets.other.example/nordlicht",  # another site, but the page shows it
        ),
        LlmItem(title="Salz und Stein", registration_closes_at="2026-11-20", tickets_url="/buy"),
        LlmItem(  # a link the page never mentions, and dates out of order
            title="Konzert Nordlicht",
            registration_opens_at="2026-12-01",
            registration_closes_at="2026-11-01",
            tickets_url="https://made-up.example/x",
        ),
        LlmItem(title="Salz und Stein", registration_closes_at="bald", tickets_url="javascript:alert(1)"),
    ]
    items, rejected = apply_guards(raw, page, src(), TZ, URL)
    assert rejected == 0  # a bad registration field never costs the item
    first, second, third, fourth = items
    assert first.registration_opens_at is not None and first.registration_closes_at is not None
    assert first.registration_closes_at.isoformat() == "2026-11-20T18:00:00+01:00"
    assert first.tickets_url == "https://tickets.other.example/nordlicht"
    assert (second.registration_opens_at, second.registration_closes_at is not None) == (None, True)
    assert second.tickets_url == "https://venue.example/buy"
    assert (third.registration_opens_at, third.registration_closes_at, third.tickets_url) == (None, None, None)
    assert (fourth.registration_closes_at, fourth.tickets_url) == (None, None)


def test_prompt_separates_opening_from_closing_and_from_the_event() -> None:
    from events_mcp.crawl.llm import SYSTEM  # noqa: PLC0415

    # Measured on the production model: without cues for "opens" it returned only the deadline.
    for cue in ("registration_opens_at", "registration_closes_at", "tickets_url", "Tickets ab", "Anmeldung bis"):
        assert cue in SYSTEM
    assert "never a ticket or registration date" in SYSTEM


def test_prompt_demands_every_listing() -> None:
    from events_mcp.crawl.llm import SYSTEM  # noqa: PLC0415

    # Measured on the production model: the registration wording alone made it return 8 of 16 films
    # on the Savoy page; this instruction brought all 16 back.
    assert "extract EVERY listing" in SYSTEM
