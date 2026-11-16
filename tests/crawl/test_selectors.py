from datetime import UTC, datetime

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp.crawl.config import CrawlerSettings
from events_mcp.crawl.extractors import Context
from events_mcp.crawl.extractors.selectors import SelectorSchema, extract_selectors, healthy, parse_dates
from events_mcp.crawl.models import Page, RawItem
from events_mcp.crawl.runner import sync_sources
from events_mcp.crawl.selector_gen import generate_selectors, trim_html

from .conftest import NOW, TZ, make_source
from .fakes import FakeChat
from .test_pipeline_db import Web
from .test_text_path import go, last_run, source

SCHEMA = {
    "base_selector": "article.card",
    "fields": {"title": "h3.name a", "url": "h3.name a::attr(href)", "date_text": "span.when"},
}
TITLES = [
    "Nordlicht Konzert",
    "Salz und Stein",
    "Fotografie im Wandel",
    "Hafenfest",
    "Lesung am Kai",
    "Nacht der Museen",
]


def cards(cls: str = "card", suffix: str = "") -> str:
    body = "".join(
        f'<article class="{cls}"><h3 class="name"><a href="/e/{i}">{t}</a></h3>'
        f'<span class="when">{21 + i}.11.2026{suffix}</span>'
        "<p>Ein Programmpunkt im Haus mit vielen Gästen.</p></article>"
        for i, t in enumerate(TITLES)
    )
    head = "<!doctype html><html><head><title>x</title></head><body><nav>Menü</nav>"
    return f"{head}<main><h1>Programm</h1>{body}</main></body></html>"


def page(html: str) -> Page:
    return Page(url="https://venue.example/programm", status=200, content_type="text/html", text=html)


def ctx() -> Context:
    return Context(make_source(url="https://venue.example/programm"), TZ, NOW)


def test_selectors_extract_titles_urls_and_dates() -> None:
    result = extract_selectors([page(cards())], SelectorSchema.model_validate(SCHEMA), ctx())
    assert result is not None and healthy(result)
    assert [i.title for i in result.items] == TITLES
    first = result.items[0]
    assert first.url == "https://venue.example/e/0"
    assert first.starts_at is not None and first.starts_at.isoformat() == "2026-11-21T00:00:00+01:00"


def test_no_match_is_none_and_unhealthy() -> None:
    result = extract_selectors([page(cards("tile"))], SelectorSchema.model_validate(SCHEMA), ctx())
    assert result is None and not healthy(result)


def test_healthy_requires_most_matches_to_have_titles() -> None:
    schema = SelectorSchema.model_validate({**SCHEMA, "fields": {"title": "h3.missing"}})
    result = extract_selectors([page(cards())], schema, ctx())
    assert result is not None and result.items == [] and result.rejected == 6 and not healthy(result)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("Sa. 21.11.", "2026-11-21T00:00:00+01:00", None),
        ("Fr 5.12.2026", "2026-12-05T00:00:00+01:00", None),
        ("20. November6. Dezember", "2026-11-20T00:00:00+01:00", "2026-12-06T23:59:59+01:00"),
        ("21. Dez. - 3. Jan.", "2026-12-21T00:00:00+01:00", "2027-01-03T23:59:59+01:00"),
        ("21.11.2026 20:00 Uhr", "2026-11-21T20:00:00+01:00", None),
        ("Sa., 21. November, 19 Uhr", "2026-11-21T19:00:00+01:00", None),
        ("2026-11-25", "2026-11-25T00:00:00+01:00", None),
        ("31.02.", None, None),
        ("keine Angabe", None, None),
    ],
)
def test_parse_dates(text: str, start: str | None, end: str | None) -> None:
    s, e = parse_dates(text, TZ, NOW)
    assert (s.isoformat() if s else None, e.isoformat() if e else None) == (start, end)


def test_trim_html_keeps_structure_and_drops_noise() -> None:
    html = (
        "<html><head><script>x()</script></head><body>"
        '<div class="a" style="color:red" onclick="x()"><img src="a.png"><a href="/x">T</a></div>'
        "</body></html>"
    )
    out = trim_html(html)
    assert 'class="a"' in out and 'href="/x"' in out
    assert "style" not in out and "onclick" not in out and "<img" not in out and "script" not in out
    assert len(trim_html("<body>" + "x" * 100_000 + "</body>")) == 40_000


REFERENCE_ANSWER = {
    "items": [{"title": t, "url": f"/e/{i}", "starts_at": f"2026-11-{21 + i}"} for i, t in enumerate(TITLES)]
}


async def test_generation_keeps_schema_only_if_it_reproduces_the_llm_items() -> None:
    reference = [RawItem(title=t, kind="event") for t in TITLES]
    src = make_source(url="https://venue.example/programm")
    good, _ = await generate_selectors([FakeChat("mac", [SCHEMA])], src, [page(cards())], reference, TZ, NOW)
    assert good is not None and good.base_selector == "article.card"
    bad = {**SCHEMA, "fields": {"title": "p"}}  # matches, but reproduces no title
    schema, provider = await generate_selectors([FakeChat("mac", [bad])], src, [page(cards())], reference, TZ, NOW)
    assert schema is None and provider == "mac"
    broken = {**SCHEMA, "base_selector": "article[["}  # not a selector at all
    schema, _ = await generate_selectors([FakeChat("mac", [broken])], src, [page(cards())], reference, TZ, NOW)
    assert schema is None
    # Too few reference items: not worth a call.
    chat = FakeChat("mac", [])
    assert (await generate_selectors([chat], src, [page(cards())], reference[:2], TZ, NOW))[0] is None
    assert chat.calls == []


@pytest.fixture
def web() -> Web:
    w = Web()
    w.routes["/programm"] = ("text/html", cards())
    return w


@pytest.fixture
def settings(schema: str) -> CrawlerSettings:
    return CrawlerSettings(database_url=schema, geocode=False)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 16, 12, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("events_mcp.crawl.runner.datetime", Frozen)


async def stored_schema(conn: AsyncConnection[DictRow]) -> dict | None:
    cur = await conn.execute("SELECT selector_schema FROM sources")
    row = await cur.fetchone()
    return row["selector_schema"] if row else None


async def test_llm_then_selectors_then_self_healing(
    conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings
) -> None:
    sid = (await sync_sources(conn, [source()]))["club"]
    # Run 1: no schema yet. One LLM call extracts, one more writes the selectors.
    assert await go(web, settings, sid, [FakeChat("mac", [REFERENCE_ANSWER, SCHEMA])]) == "success"
    run = await last_run(conn)
    assert (run["extractor"], run["llm_calls"], run["items_new"]) == ("llm", 2, 6)
    assert (await stored_schema(conn)) == SCHEMA

    # Run 2: the page changed, selectors take over, no LLM call at all.
    web.routes["/programm"] = ("text/html", cards(suffix=" 20:00"))
    silent = FakeChat("mac", [])
    assert await go(web, settings, sid, [silent]) == "success"
    run = await last_run(conn)
    assert (run["extractor"], run["llm_calls"], silent.calls) == ("selectors", 0, [])
    assert run["items_updated"] == 6

    # Run 3: nothing changed since run 2: hash gating, touched only.
    assert await go(web, settings, sid, [FakeChat("mac", [])]) == "success"
    run = await last_run(conn)
    assert (run["extractor"], run["items_touched"], run["llm_calls"]) == ("selectors", 6, 0)

    # Run 4: a redesign renames the card class. Selectors match nothing: LLM again, then new selectors.
    web.routes["/programm"] = ("text/html", cards("tile"))
    new_schema = {**SCHEMA, "base_selector": "article.tile"}
    assert await go(web, settings, sid, [FakeChat("mac", [REFERENCE_ANSWER, new_schema])]) == "success"
    run = await last_run(conn)
    assert (run["extractor"], run["llm_calls"]) == ("llm", 2)
    assert (await stored_schema(conn)) == new_schema


async def test_shop_defaults_to_sale_kind(conn: AsyncConnection[DictRow], web: Web, settings: CrawlerSettings) -> None:
    shop = source(type="shop")
    sid = (await sync_sources(conn, [shop]))["club"]
    answer = {"items": [{"title": "Nordlicht Konzert", "starts_at": "2026-11-20", "ends_at": "2026-11-29"}]}
    await go(web, settings, sid, [FakeChat("mac", [answer])], type="shop")
    cur = await conn.execute("SELECT kind FROM events")
    assert [r["kind"] for r in await cur.fetchall()] == ["sale"]
