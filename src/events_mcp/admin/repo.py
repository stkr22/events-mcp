"""Reads for the admin API; writes go through ``events_mcp.crawl.store``."""

from typing import Any

from psycopg import AsyncConnection

from events_mcp.admin.models import Effective, HistoryEntry, RunView, SourceView
from events_mcp.crawl import store
from events_mcp.crawl.config import ResolvedSource

_RUN_COLUMNS = (
    "id, status, started_at, finished_at, error, extractor, llm_provider, llm_calls, llm_input_chars, "
    "items_seen, items_new, items_updated, items_touched, items_rejected"
)

_SOURCES = f"""
SELECT s.name, s.type, s.url, s.enabled, s.config, s.run_request, s.updated_at,
       (SELECT count(*) FROM events e WHERE e.source_id = s.id) AS item_count,
       (SELECT max(finished_at) FROM scrape_runs r WHERE r.source_id = s.id AND r.status = 'success')
           AS last_success_at,
       (SELECT to_jsonb(lr) FROM (
            SELECT {_RUN_COLUMNS} FROM scrape_runs WHERE source_id = s.id ORDER BY started_at DESC, id DESC LIMIT 1
        ) lr) AS last_run
FROM sources s
WHERE s.config <> '{{}}'::jsonb
"""


def _effective(s: ResolvedSource) -> Effective:
    return Effective(
        interval=s.interval,
        city=s.city,
        timezone=s.timezone,
        render=s.render,
        extractor=s.extractor,
        kind=s.kind,
        hosts=s.hosts,
        primary_url=s.primary_url,
    )


async def list_sources(conn: AsyncConnection[Any], name: str | None = None) -> list[SourceView]:
    """Every configured source (or one), with its last run, newest crawl info and validity."""
    cur = await conn.execute(
        _SOURCES + (" AND s.name = %s" if name else "") + " ORDER BY s.name", (name,) if name else ()
    )
    rows = await cur.fetchall()
    resolved, problems = await store.load_sources(conn, include_disabled=True)
    by_name = {s.name: s for s in resolved}
    return [
        SourceView(
            **{k: v for k, v in r.items() if k != "last_run"},
            effective=_effective(by_name[r["name"]]) if r["name"] in by_name else None,
            problem=problems.get(r["name"]),
            last_run=RunView.model_validate(r["last_run"]) if r["last_run"] else None,
        )
        for r in rows
    ]


async def runs(conn: AsyncConnection[Any], name: str, limit: int = 20) -> list[RunView]:
    """Recent crawls of one source, newest first."""
    cur = await conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM scrape_runs WHERE source_id = (SELECT id FROM sources WHERE name = %s) "
        "ORDER BY started_at DESC, id DESC LIMIT %s",
        (name, limit),
    )
    return [RunView.model_validate(r) for r in await cur.fetchall()]


async def history(conn: AsyncConnection[Any], name: str | None = None, limit: int = 50) -> list[HistoryEntry]:
    """List configuration changes, newest first; one source's, or all."""
    cur = await conn.execute(
        "SELECT id, source_name, action, actor, changed_at, before, after FROM source_config_history "
        "WHERE (%(n)s::text IS NULL OR source_name = %(n)s) ORDER BY changed_at DESC, id DESC LIMIT %(l)s",
        {"n": name, "l": limit},
    )
    return [HistoryEntry.model_validate(r) for r in await cur.fetchall()]
