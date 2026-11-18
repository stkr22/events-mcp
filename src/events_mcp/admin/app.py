"""The admin FastAPI app: sign-in, the configuration API, and the built UI."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, get_args
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from events_mcp.admin import repo
from events_mcp.admin.auth import AuthError, Oidc
from events_mcp.admin.models import HistoryEntry, Me, Options, RunRequest, SourceDetail, SourceView
from events_mcp.admin.settings import AdminSettings
from events_mcp.crawl import store
from events_mcp.crawl.config import Defaults, ExtractorName, KindName, Render, Source, SourceType
from events_mcp.db import Pool, make_pool

log = logging.getLogger(__name__)

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
DEV_SUB = "dev"


def _literals(t: Any) -> list[str]:
    return list(get_args(t))


def _actor(user: dict[str, Any]) -> str:
    who = user.get("name") or user.get("email") or user["sub"]
    return f"{who} ({user['sub']})"[:200]


def create_admin_app(settings: AdminSettings, pool: Pool | None = None) -> FastAPI:  # noqa: PLR0915 - one route per endpoint
    """Build the app. A ``pool`` passed in is used as it is (tests); otherwise one is opened at startup."""
    settings.check()
    own_pool = pool is None
    http = httpx.AsyncClient(timeout=10.0)
    oidc = (
        Oidc(settings.oidc_issuer, settings.oidc_client_id or "", f"{settings.public_base_url}/auth/callback", http)
        if settings.oidc_issuer
        else None
    )
    origin = urlparse(settings.public_base_url)
    allowed_origin = f"{origin.scheme}://{origin.netloc}"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool = pool or make_pool(settings.database_url.get_secret_value(), max_size=4)
        if own_pool:
            await app.state.pool.open(wait=True)
        yield
        if own_pool:
            await app.state.pool.close()
        await http.aclose()

    app = FastAPI(title="events-mcp admin", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    if pool is not None:
        app.state.pool = pool
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value() or "dev-only-not-a-secret",
        session_cookie="events_admin",
        max_age=settings.session_max_age_sec,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    @app.middleware("http")
    async def same_origin(request: Request, call_next: Any) -> Response:
        # SameSite=Lax already withholds the cookie from cross-site posts; this is the second lock.
        if request.method in UNSAFE and (o := request.headers.get("origin")) and o != allowed_origin:
            return Response("cross-origin request refused", status_code=403)
        return await call_next(request)

    def user(request: Request) -> dict[str, Any]:
        u = request.session.get("user")
        if not u:
            raise HTTPException(401, "not signed in")
        return u

    def admin(u: Annotated[dict[str, Any], Depends(user)]) -> dict[str, Any]:
        if u["sub"] not in settings.admin_subjects and not (settings.dev_auth and u["sub"] == DEV_SUB):
            raise HTTPException(403, "this account may not edit the crawler configuration")
        return u

    Admin = Annotated[dict[str, Any], Depends(admin)]  # noqa: N806 - a type alias

    async def conn_dep(request: Request) -> AsyncIterator[Any]:
        async with request.app.state.pool.connection() as conn:
            yield conn

    Conn = Annotated[Any, Depends(conn_dep)]  # noqa: N806

    # --- sign-in -------------------------------------------------------------------------------
    @app.get("/auth/login")
    async def login(request: Request) -> RedirectResponse:
        if oidc is None:
            raise HTTPException(404, "OIDC is not configured")
        try:
            url, flow = await oidc.start()
        except (httpx.HTTPError, AuthError) as exc:
            log.error("OIDC discovery failed: %s", exc)
            raise HTTPException(502, "the identity provider is not reachable") from exc
        request.session["flow"] = flow.as_dict()
        return RedirectResponse(url)

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
        if oidc is None:
            raise HTTPException(404, "OIDC is not configured")
        flow = request.session.pop("flow", None)
        if error or not code:
            raise HTTPException(400, f"sign-in was not completed: {error or 'no code'}")
        try:
            who = await oidc.finish(code, state, flow or {})
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            log.error("OIDC token exchange failed: %s", exc)
            raise HTTPException(502, "the identity provider is not reachable") from exc
        request.session["user"] = {"sub": who.sub, "name": who.name, "email": who.email}
        if who.sub not in settings.admin_subjects:
            log.warning("sign-in by %s (%s) refused: not in EVENTS_ADMIN_SUBJECTS", who.sub, who.email)
        return RedirectResponse("/")

    @app.get("/auth/dev-login")
    async def dev_login(request: Request) -> RedirectResponse:
        if not settings.dev_auth:
            raise HTTPException(404)
        request.session["user"] = {"sub": DEV_SUB, "name": "Developer", "email": None}
        return RedirectResponse("/")

    @app.post("/auth/logout")
    async def logout(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request) -> Me:
        u = request.session.get("user")
        if not u:
            return Me(authenticated=False, oidc=oidc is not None)
        is_admin = u["sub"] in settings.admin_subjects or (settings.dev_auth and u["sub"] == DEV_SUB)
        return Me(authenticated=True, admin=is_admin, oidc=oidc is not None, **u)

    # --- configuration ---------------------------------------------------------------------------
    @app.get("/api/options")
    async def options(_: Admin) -> Options:
        return Options(
            source_types=_literals(SourceType),
            renders=_literals(Render),
            extractors=_literals(ExtractorName),
            kinds=_literals(KindName),
            run_modes=["due", "full"],
        )

    @app.get("/api/sources")
    async def list_sources(_: Admin, conn: Conn) -> list[SourceView]:
        return await repo.list_sources(conn)

    @app.get("/api/sources/{name}")
    async def get_source(name: str, _: Admin, conn: Conn) -> SourceDetail:
        found = await repo.list_sources(conn, name)
        if not found:
            raise HTTPException(404, f"unknown source {name!r}")
        return SourceDetail(
            **found[0].model_dump(), runs=await repo.runs(conn, name), history=await repo.history(conn, name, 30)
        )

    async def _save(conn: Any, source: Source, who: dict[str, Any]) -> SourceView:
        try:
            await store.save_source(conn, source, actor=_actor(who))
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return (await repo.list_sources(conn, source.name))[0]

    @app.post("/api/sources", status_code=201)
    async def create_source(source: Source, who: Admin, conn: Conn) -> SourceView:
        if await repo.list_sources(conn, source.name):
            raise HTTPException(409, f"{source.name!r} already exists")
        return await _save(conn, source, who)

    @app.put("/api/sources/{name}")
    async def update_source(name: str, source: Source, who: Admin, conn: Conn) -> SourceView:
        if source.name != name:
            raise HTTPException(422, "a source cannot be renamed; create a new one")
        if not await repo.list_sources(conn, name):
            raise HTTPException(404, f"unknown source {name!r}")
        return await _save(conn, source, who)

    def _guard(exc: store.ConfigError) -> HTTPException:
        return HTTPException(404 if isinstance(exc, store.UnknownSourceError) else 409, str(exc))

    @app.post("/api/sources/{name}/enable")
    async def enable(name: str, who: Admin, conn: Conn) -> SourceView:
        try:
            await store.set_enabled(conn, name, True, actor=_actor(who))
        except store.ConfigError as exc:
            raise _guard(exc) from exc
        return (await repo.list_sources(conn, name))[0]

    @app.post("/api/sources/{name}/disable")
    async def disable(name: str, who: Admin, conn: Conn) -> SourceView:
        try:
            await store.set_enabled(conn, name, False, actor=_actor(who))
        except store.ConfigError as exc:
            raise _guard(exc) from exc
        return (await repo.list_sources(conn, name))[0]

    @app.delete("/api/sources/{name}", status_code=204)
    async def delete(name: str, who: Admin, conn: Conn) -> Response:
        try:
            await store.delete_source(conn, name, actor=_actor(who))
        except store.ConfigError as exc:
            raise _guard(exc) from exc
        return Response(status_code=204)

    @app.post("/api/sources/{name}/run", status_code=202)
    async def run_now(name: str, body: RunRequest, _: Admin, conn: Conn) -> SourceView:
        try:
            await store.request_run(conn, name, body.mode)
        except store.ConfigError as exc:
            raise _guard(exc) from exc
        return (await repo.list_sources(conn, name))[0]

    @app.get("/api/defaults")
    async def get_defaults(_: Admin, conn: Conn) -> Defaults:
        return await store.load_defaults(conn)

    @app.put("/api/defaults")
    async def put_defaults(defaults: Defaults, who: Admin, conn: Conn) -> Defaults:
        try:
            await store.save_defaults(conn, defaults, actor=_actor(who))
        except store.ConfigError as exc:
            raise HTTPException(409, str(exc)) from exc
        return defaults

    @app.get("/api/history")
    async def history(_: Admin, conn: Conn, limit: int = 50) -> list[HistoryEntry]:
        return await repo.history(conn, None, min(max(limit, 1), 200))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- the UI ------------------------------------------------------------------------------------
    if settings.static_dir:
        root = Path(settings.static_dir).resolve()

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            if path.startswith(("api/", "auth/")):
                raise HTTPException(404)
            candidate = (root / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html")

    return app
