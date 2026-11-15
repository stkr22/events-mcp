import json

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from starlette.testclient import TestClient

from events_mcp.config import Settings
from events_mcp.embeddings import OpenAIEmbedder
from events_mcp.ingest import finish_run, start_run
from events_mcp.metrics import Metrics
from events_mcp.server import create_app

from .conftest import FakeEmbedder

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client: TestClient, method: str, params: dict | None = None, token: str | None = "secret") -> httpx.Response:
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", headers=headers, content=json.dumps(body))


def call(client: TestClient, tool: str, **arguments) -> dict:
    resp = rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.fixture
def client(seeded: dict[str, int], schema: str):
    settings = Settings(database_url=schema, mcp_token="secret")
    app, _ = create_app(settings, FakeEmbedder(), run_migrations=False, metrics_loop=False)
    with TestClient(app) as c:
        rpc(
            c,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        )
        yield c


def test_auth_required(client: TestClient) -> None:
    assert rpc(client, "tools/list", token=None).status_code == 401
    assert rpc(client, "tools/list", token="wrong").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").text == "ready"


def test_tools_listed(client: TestClient) -> None:
    tools = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools == {"search_events", "upcoming", "ending_soon", "get_event", "list_sources"}


def test_search_through_mcp(client: TestClient, seeded: dict[str, int]) -> None:
    out = call(client, "search_events", query="quiet art", city="Hamburg")
    assert out["semantic_search"] == "used"
    assert out["results"][0]["id"] == seeded["ex-romantik"]
    out = call(client, "search_events", date_from="2000-01-01", date_to="2000-01-02")
    assert out["count"] == 0


def test_other_tools_through_mcp(client: TestClient, seeded: dict[str, int]) -> None:
    assert call(client, "upcoming", city="Hamburg", days=7)["count"] == 3
    assert call(client, "ending_soon", days=21)["results"][0]["id"] == seeded["ex-romantik"]
    assert call(client, "get_event", event_id=seeded["yt-abc"])["source"] == "Space Channel"
    assert call(client, "list_sources")["count"] == 3


def test_bad_arguments_are_tool_errors(client: TestClient) -> None:
    resp = rpc(client, "tools/call", {"name": "search_events", "arguments": {"date_from": "tomorrow"}})
    assert resp.json()["result"]["isError"] is True
    resp = rpc(client, "tools/call", {"name": "get_event", "arguments": {"event_id": 424242}})
    assert resp.json()["result"]["isError"] is True


async def test_metrics_refresh(pool, conn: AsyncConnection[DictRow], seeded: dict[str, int]) -> None:
    cur = await conn.execute("SELECT id FROM sources WHERE name = 'Fabrik'")
    row = await cur.fetchone()
    assert row is not None
    src = row["id"]
    await finish_run(conn, await start_run(conn, src), "success", 3)
    m = Metrics()
    await m.refresh(pool, None, FakeEmbedder.model)
    reg = m.registry
    created = reg.get_sample_value("events_last_successful_scrape_timestamp_seconds", {"source": "Space Channel"})
    fabrik = reg.get_sample_value("events_last_successful_scrape_timestamp_seconds", {"source": "Fabrik"})
    assert created is not None and fabrik is not None and fabrik >= created
    assert reg.get_sample_value("events_items", {"kind": "exhibition", "lifecycle": "ongoing"}) == 2
    assert reg.get_sample_value("events_items_missing_embedding") == 0
    assert reg.get_sample_value("events_db_up") == 1

    await conn.execute("DELETE FROM sources WHERE name = 'Space Channel'")
    await m.refresh(pool, None, FakeEmbedder.model)
    gone = reg.get_sample_value("events_last_successful_scrape_timestamp_seconds", {"source": "Space Channel"})
    assert gone is None


async def test_openai_embedder_breaker() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        if calls["n"] == 1:
            return httpx.Response(503)
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.0] * 1024} for i in range(len(texts))]})

    e = OpenAIEmbedder("http://mac/v1", "m", 1024, transport=httpx.MockTransport(handler), retry_after_sec=3600)
    assert await e.embed(["a"]) is None  # 503 trips the breaker
    assert await e.embed(["a"]) is None  # skipped without a request
    assert calls["n"] == 1
    assert await e.probe() is True  # probe closes it again
    assert await e.embed(["a", "b"]) == [[0.0] * 1024, [0.0] * 1024]
    await e.aclose()
