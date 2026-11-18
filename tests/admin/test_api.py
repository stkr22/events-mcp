"""The admin API end to end against the real schema: sign-in guard, CRUD, validation and history."""

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from itsdangerous import TimestampSigner
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp.admin.app import create_admin_app
from events_mcp.admin.settings import AdminSettings
from events_mcp.db import Pool

CLUB: dict[str, Any] = {
    "name": "club",
    "type": "venue",
    "url": "https://club.example/events",
    "intent": "Concerts only.",
}


@pytest.fixture(autouse=True)
async def _clean(conn: AsyncConnection[DictRow]) -> None:
    await conn.execute("TRUNCATE source_config_history")
    await conn.execute("UPDATE crawler_defaults SET config = '{}'")


@pytest.fixture
async def api(pool: Pool) -> AsyncIterator[httpx.AsyncClient]:
    settings = AdminSettings(database_url="postgresql://unused", dev_auth=True)
    app = create_admin_app(settings, pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as c:
        assert (await c.get("/auth/dev-login", follow_redirects=False)).status_code == 307
        yield c


async def test_nothing_without_signing_in(pool: Pool) -> None:
    app = create_admin_app(AdminSettings(database_url="postgresql://x", dev_auth=True), pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/sources")).status_code == 401
        assert (await c.get("/api/me")).json()["authenticated"] is False
        assert (await c.post("/api/sources", json=CLUB)).status_code == 401


async def test_a_signed_in_user_not_on_the_list_is_refused(pool: Pool) -> None:
    secret = "s" * 40
    settings = AdminSettings(
        database_url="postgresql://x",
        oidc_issuer="https://id.example",
        oidc_client_id="cid",
        session_secret=secret,
        admin_subjects="boss",
    )
    app = create_admin_app(settings, pool)

    def cookie(sub: str) -> str:
        data = base64.b64encode(json.dumps({"user": {"sub": sub, "name": "N", "email": None}}).encode())
        return TimestampSigner(secret).sign(data).decode()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.cookies.set("events_admin", cookie("stranger"))
        me = (await c.get("/api/me")).json()
        assert me["authenticated"] and not me["admin"] and me["sub"] == "stranger"  # the UI shows this id
        assert (await c.get("/api/sources")).status_code == 403
        c.cookies.set("events_admin", cookie("boss"))
        assert (await c.get("/api/sources")).status_code == 200


async def test_create_edit_and_read_back(api: httpx.AsyncClient) -> None:
    r = await api.post("/api/sources", json=CLUB)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["enabled"] and body["effective"]["interval"] == "24h" and body["last_run"] is None
    assert (await api.post("/api/sources", json=CLUB)).status_code == 409
    r = await api.put("/api/sources/club", json={**CLUB, "intent": "Jazz.", "render": "browser"})
    assert r.status_code == 200 and r.json()["effective"]["render"] == "browser"
    detail = (await api.get("/api/sources/club")).json()
    assert detail["config"]["intent"] == "Jazz." and [h["action"] for h in detail["history"]] == ["update", "create"]
    assert detail["history"][0]["before"]["intent"] == "Concerts only."
    assert (await api.get("/api/sources/nope")).status_code == 404


async def test_bad_input_is_explained(api: httpx.AsyncClient) -> None:
    r = await api.post("/api/sources", json={"name": "Bad Name", "type": "venue", "url": "https://x.example"})
    assert r.status_code == 422 and "lowercase slug" in r.text
    r = await api.post("/api/sources", json={"name": "yt", "type": "youtube", "channel_id": "PLabc"})
    assert r.status_code == 422 and "UC" in r.text
    r = await api.post("/api/sources", json={**CLUB, "interval": "soon"})
    assert r.status_code == 422
    r = await api.post("/api/sources", json={**CLUB, "colour": "red"})  # unknown keys are refused
    assert r.status_code == 422
    await api.post("/api/sources", json=CLUB)
    assert (await api.put("/api/sources/club", json={**CLUB, "name": "other"})).status_code == 422


async def test_an_edit_keeps_a_disabled_source_disabled(api: httpx.AsyncClient) -> None:
    await api.post("/api/sources", json=CLUB)
    assert (await api.post("/api/sources/club/disable")).json()["enabled"] is False
    assert (await api.put("/api/sources/club", json={**CLUB, "intent": "x"})).json()["enabled"] is False
    assert (await api.post("/api/sources/club/run", json={"mode": "due"})).status_code == 409  # disabled
    assert (await api.post("/api/sources/club/enable")).json()["enabled"] is True


async def test_run_now_and_delete(api: httpx.AsyncClient, conn: AsyncConnection[DictRow]) -> None:
    await api.post("/api/sources", json=CLUB)
    assert (await api.post("/api/sources/club/run", json={"mode": "full"})).json()["run_request"] == "full"
    assert (await api.post("/api/sources/nope/run", json={})).status_code == 404
    await conn.execute(
        "INSERT INTO events (source_id, title, kind, dedupe_key) SELECT id, 'Gig', 'event', 'g' FROM sources"
    )
    r = await api.delete("/api/sources/club")
    assert r.status_code == 409 and "disable it" in r.text
    await conn.execute("DELETE FROM events")
    assert (await api.delete("/api/sources/club")).status_code == 204
    assert (await api.get("/api/sources")).json() == []


async def test_defaults_round_trip_and_reach_the_sources(api: httpx.AsyncClient) -> None:
    await api.post("/api/sources", json=CLUB)
    assert (await api.get("/api/defaults")).json()["interval"] == "24h"
    d = (await api.get("/api/defaults")).json()
    assert (await api.put("/api/defaults", json={**d, "interval": "6h", "city": "Berlin"})).status_code == 200
    eff = (await api.get("/api/sources/club")).json()["effective"]
    assert (eff["interval"], eff["city"]) == ("6h", "Berlin")
    assert (await api.put("/api/defaults", json={**d, "interval": "never"})).status_code == 422
    history = (await api.get("/api/history")).json()
    assert history[0]["action"] == "defaults" and history[0]["source_name"] == "(defaults)"


async def test_cross_origin_writes_are_refused(api: httpx.AsyncClient) -> None:
    r = await api.post("/api/sources", json=CLUB, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await api.post("/api/sources", json=CLUB, headers={"Origin": "http://localhost:8080"})
    assert r.status_code == 201


async def test_options_come_from_the_crawler_vocabulary(api: httpx.AsyncClient) -> None:
    o = (await api.get("/api/options")).json()
    assert "youtube" in o["source_types"] and "browser_stealth" in o["renders"] and "sale" in o["kinds"]


def test_unsafe_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="OIDC_ISSUER"):
        AdminSettings(database_url="postgresql://x").check()
    with pytest.raises(ValueError, match="at least 32"):
        AdminSettings(
            database_url="postgresql://x",
            oidc_issuer="https://id.example",
            oidc_client_id="c",
        ).check()
    with pytest.raises(ValueError, match="cannot be combined"):
        AdminSettings(
            database_url="postgresql://x",
            oidc_issuer="https://id.example",
            oidc_client_id="c",
            dev_auth=True,
        ).check()
