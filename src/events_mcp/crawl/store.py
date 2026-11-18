"""Crawler configuration in the database: read by the crawler, written by the admin app.

``sources.config`` holds a source as authored (defaults not applied) so a change to the defaults
reaches every source that does not override them. The denormalised columns (type, url, city,
scrape_interval) are derived from it on every write; other code reads them.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection
from pydantic import ValidationError

from events_mcp.crawl.config import Defaults, ResolvedSource, Source, SourcesFile, resolve_source

log = logging.getLogger(__name__)

DEFAULTS_NAME = "(defaults)"
RunMode = Literal["due", "full"]


class ConfigError(ValueError):
    """A change the store refuses (source with data, disabled source, ...)."""


class UnknownSourceError(ConfigError):
    """No source of that name."""


async def load_defaults(conn: AsyncConnection[Any]) -> Defaults:
    """Return the file-level defaults; an empty row means the built-in ones."""
    cur = await conn.execute("SELECT config FROM crawler_defaults WHERE id = 1")
    row = await cur.fetchone()
    return Defaults.model_validate(_cell(row, "config") if row else {})


async def load_sources(
    conn: AsyncConnection[Any], *, include_disabled: bool = False
) -> tuple[list[ResolvedSource], dict[str, str]]:
    """Return the resolved sources, plus ``name -> problem`` for rows that no longer validate.

    One broken row must not stop the other sources from being crawled, so it is reported, not raised.
    Rows without a config (made by ``events-mcp import``) are not crawlable and are skipped.
    """
    defaults = await load_defaults(conn)
    cur = await conn.execute(
        "SELECT name, config FROM sources WHERE config <> '{}'::jsonb AND (enabled OR %s) ORDER BY name",
        (include_disabled,),
    )
    out: list[ResolvedSource] = []
    problems: dict[str, str] = {}
    for row in await cur.fetchall():
        name, config = _cell(row, "name"), _cell(row, "config")
        try:
            out.append(resolve_source(Source.model_validate(config), defaults))
        except ValidationError as exc:
            problems[name] = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            log.error("source %s has an invalid config: %s", name, problems[name])
    return out, problems


async def save_source(conn: AsyncConnection[Any], source: Source, *, actor: str, enabled: bool | None = None) -> int:
    """Create or replace a source's configuration; returns its id. ``name`` is the identity.

    ``enabled`` is kept as it is on an update and true on a create unless given.
    """
    defaults = await load_defaults(conn)
    resolved = resolve_source(source, defaults)  # raises on a combination the crawler would reject
    raw = source.model_dump(mode="json", exclude_none=True)
    async with conn.transaction():
        before = await _config(conn, source.name)
        cur = await conn.execute(
            """
            INSERT INTO sources (name, type, url, city, scrape_interval, config, enabled)
            VALUES (%s, %s, %s, %s, %s::interval, %s::jsonb, coalesce(%s, true))
            ON CONFLICT (name) DO UPDATE
                SET type = EXCLUDED.type, url = EXCLUDED.url, city = EXCLUDED.city,
                    scrape_interval = EXCLUDED.scrape_interval, config = EXCLUDED.config,
                    enabled = coalesce(%s, sources.enabled), updated_at = now()
            RETURNING id
            """,
            (
                resolved.name,
                resolved.type,
                resolved.primary_url,
                resolved.city,
                _interval(resolved),
                json.dumps(raw),
                enabled,
                enabled,
            ),
        )
        row = await cur.fetchone()
        assert row is not None  # noqa: S101 - RETURNING always yields a row
        await _log(conn, source.name, "create" if before is None else "update", actor, before, raw)
        return int(_cell(row, "id"))


async def set_enabled(conn: AsyncConnection[Any], name: str, enabled: bool, *, actor: str) -> None:
    """Switch crawling of one source on or off; its rows and history stay."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE sources SET enabled = %s, updated_at = now() WHERE name = %s AND enabled <> %s RETURNING name",
            (enabled, name, enabled),
        )
        if await cur.fetchone() is not None:
            await _log(conn, name, "enable" if enabled else "disable", actor, None, None)
        elif await _config(conn, name) is None:
            raise UnknownSourceError(f"unknown source {name!r}")


async def delete_source(conn: AsyncConnection[Any], name: str, *, actor: str) -> None:
    """Remove a source that has delivered nothing.

    One with events is disabled instead: deleting it would
    cascade to its events and to what the user was told about them.
    """
    async with conn.transaction():
        before = await _config(conn, name)
        if before is None:
            raise UnknownSourceError(f"unknown source {name!r}")
        cur = await conn.execute(
            "SELECT count(*) AS n FROM events e JOIN sources s ON s.id = e.source_id WHERE s.name = %s", (name,)
        )
        row = await cur.fetchone()
        if row is not None and _cell(row, "n"):
            raise ConfigError(f"{name!r} has {_cell(row, 'n')} stored items; disable it instead of deleting")
        await conn.execute("DELETE FROM sources WHERE name = %s", (name,))
        await _log(conn, name, "delete", actor, before, None)


async def save_defaults(conn: AsyncConnection[Any], defaults: Defaults, *, actor: str) -> None:
    """Replace the file-level defaults and re-derive every source's interval and city from them."""
    raw = defaults.model_dump(mode="json")
    async with conn.transaction():
        cur = await conn.execute("SELECT config FROM crawler_defaults WHERE id = 1")
        row = await cur.fetchone()
        before = _cell(row, "config") if row else None
        await conn.execute(
            "UPDATE crawler_defaults SET config = %s::jsonb, updated_at = now() WHERE id = 1", (json.dumps(raw),)
        )
        sources, problems = await load_sources(conn, include_disabled=True)
        if problems:
            raise ConfigError("defaults not saved; invalid sources: " + ", ".join(sorted(problems)))
        for s in sources:
            await conn.execute(
                "UPDATE sources SET city = %s, scrape_interval = %s::interval WHERE name = %s",
                (s.city, _interval(s), s.name),
            )
        await _log(conn, DEFAULTS_NAME, "defaults", actor, before, raw)


async def request_run(conn: AsyncConnection[Any], name: str, mode: RunMode) -> None:
    """Ask the crawler to take this source on its next tick (every 15 minutes)."""
    cur = await conn.execute(
        "UPDATE sources SET run_request = %s, run_requested_at = now() WHERE name = %s AND enabled RETURNING name",
        (mode, name),
    )
    if await cur.fetchone() is None:
        if await _config(conn, name) is None:
            raise UnknownSourceError(f"unknown source {name!r}")
        raise ConfigError(f"{name!r} is disabled; enable it first")


async def take_run_requests(conn: AsyncConnection[Any]) -> dict[str, RunMode]:
    """Read and clear the pending run requests: ``name -> mode``."""
    cur = await conn.execute(
        "WITH old AS (SELECT id, name, run_request FROM sources WHERE run_request IS NOT NULL FOR UPDATE) "
        "UPDATE sources s SET run_request = NULL, run_requested_at = NULL FROM old WHERE s.id = old.id "
        "RETURNING old.name, old.run_request"
    )
    return {_cell(r, "name"): _cell(r, "run_request") for r in await cur.fetchall()}


async def import_file(conn: AsyncConnection[Any], path: Path, *, actor: str) -> int:
    """Load a ``sources.yaml`` once: defaults and sources. Existing sources of the same name are replaced.

    Sources not in the file are left alone (they may have been added in the admin app since).
    """
    import yaml  # noqa: PLC0415 - only the crawler image carries PyYAML

    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    parsed = SourcesFile.model_validate(yaml.safe_load(text) or {})
    async with conn.transaction():
        await save_defaults(conn, parsed.defaults, actor=actor)
        for s in parsed.sources:
            await save_source(conn, s, actor=actor)
    return len(parsed.sources)


def _interval(s: ResolvedSource) -> str:
    return f"{int(s.interval_delta.total_seconds())} seconds"


async def _config(conn: AsyncConnection[Any], name: str) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT config FROM sources WHERE name = %s", (name,))
    row = await cur.fetchone()
    return None if row is None else _cell(row, "config")


async def _log(
    conn: AsyncConnection[Any], name: str, action: str, actor: str, before: Any | None, after: Any | None
) -> None:
    await conn.execute(
        "INSERT INTO source_config_history (source_name, action, actor, before, after) "
        "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)",
        (
            name,
            action,
            actor,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
        ),
    )


def _cell(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, dict) else row[0]
