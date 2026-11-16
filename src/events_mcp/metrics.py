"""Prometheus metrics. Database-derived gauges are refreshed by a background loop, not per scrape."""

import asyncio
import logging
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge

from events_mcp.db import Pool
from events_mcp.embeddings import OpenAIEmbedder

log = logging.getLogger(__name__)

LIFECYCLES = ("upcoming", "ongoing", "ended", "undated", "published")


class Metrics:
    """All metrics on a private registry, so tests can create as many as they like."""

    def __init__(self) -> None:
        """Register every metric."""
        self.registry = CollectorRegistry()
        self.last_success = Gauge(
            "events_last_successful_scrape_timestamp_seconds",
            "Finish time of the newest successful scrape_run per source; a source that never succeeded "
            "reports its creation time, which gives new sources the alert's grace period.",
            ["source"],
            registry=self.registry,
        )
        self.items = Gauge(
            "events_items",
            "Stored items by kind and computed lifecycle.",
            ["kind", "lifecycle"],
            registry=self.registry,
        )
        self.missing_embedding = Gauge(
            "events_items_missing_embedding",
            "Items with no embedding from the configured model (backfill backlog).",
            registry=self.registry,
        )
        self.interval = Gauge(
            "events_source_scrape_interval_seconds",
            "Configured scrape interval per enabled source.",
            ["source"],
            registry=self.registry,
        )
        self.last_status = Gauge(
            "events_scrape_last_status",
            "1 for the status of the source's newest finished run.",
            ["source", "status"],
            registry=self.registry,
        )
        self.source_items = Gauge(
            "events_source_items",
            "Items per enabled source and lifecycle (zero-filled, so a source that emptied out stays visible).",
            ["source", "lifecycle"],
            registry=self.registry,
        )
        self.llm_calls = Gauge(
            "events_llm_calls_24h",
            "LLM calls recorded by scrape runs in the last 24 hours, by provider.",
            ["provider"],
            registry=self.registry,
        )
        self.pending_age = Gauge(
            "events_pending_extraction_seconds",
            "Age of the oldest unresolved pending_extraction run per source; 0 when none.",
            ["source"],
            registry=self.registry,
        )
        self.db_up = Gauge("events_db_up", "1 if the last metrics refresh reached Postgres.", registry=self.registry)
        self.embedding_up = Gauge(
            "events_embedding_endpoint_up",
            "1 if the embedding endpoint answered the last probe.",
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "events_mcp_tool_calls",
            "MCP tool invocations.",
            ["tool", "outcome"],
            registry=self.registry,
        )
        self.search_mode = Counter(
            "events_mcp_searches",
            "search_events calls by whether semantic search took part.",
            ["semantic"],
            registry=self.registry,
        )
        self._sources: set[str] = set()

    async def refresh(self, pool: Pool, embedder: OpenAIEmbedder | None, embedding_model: str) -> None:
        """Re-read the database-derived gauges once."""
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """
                    SELECT s.name,
                           extract(epoch FROM coalesce(
                               max(r.finished_at) FILTER (WHERE r.status = 'success'), s.created_at
                           ))::float8 AS ts
                    FROM sources s LEFT JOIN scrape_runs r ON r.source_id = s.id
                    WHERE s.enabled
                    GROUP BY s.id
                    """
                )
                seen = set()
                for row in await cur.fetchall():
                    self.last_success.labels(source=row["name"]).set(row["ts"])
                    seen.add(row["name"])
                for gone in self._sources - seen:
                    self.last_success.remove(gone)
                self._sources = seen

                await self._refresh_crawl(conn)

                cur = await conn.execute("SELECT kind, lifecycle, count(*) AS n FROM event_lifecycle GROUP BY 1, 2")
                self.items.clear()
                for row in await cur.fetchall():
                    self.items.labels(kind=row["kind"], lifecycle=row["lifecycle"]).set(row["n"])

                cur = await conn.execute(
                    "SELECT count(*) AS n FROM events WHERE embedding_model IS DISTINCT FROM %s",
                    (embedding_model,),
                )
                row = await cur.fetchone()
                self.missing_embedding.set(row["n"] if row else 0)
            self.db_up.set(1)
        except Exception:
            log.exception("metrics refresh failed")
            self.db_up.set(0)
        if embedder is not None:
            self.embedding_up.set(1 if await embedder.probe() else 0)

    async def _refresh_crawl(self, conn: Any) -> None:
        """Per-source gauges for the crawler alerts. Disabled sources are left out on purpose."""
        for g in (self.interval, self.last_status, self.source_items, self.pending_age, self.llm_calls):
            g.clear()
        cur = await conn.execute(
            "SELECT name, extract(epoch FROM scrape_interval)::float8 AS secs FROM sources WHERE enabled"
        )
        for row in await cur.fetchall():
            self.interval.labels(source=row["name"]).set(row["secs"])
            for lifecycle in LIFECYCLES:
                self.source_items.labels(source=row["name"], lifecycle=lifecycle).set(0)
        cur = await conn.execute(
            """
            SELECT s.name, l.lifecycle, count(*) AS n
            FROM event_lifecycle l JOIN sources s ON s.id = l.source_id
            WHERE s.enabled GROUP BY 1, 2
            """
        )
        for row in await cur.fetchall():
            self.source_items.labels(source=row["name"], lifecycle=row["lifecycle"]).set(row["n"])
        cur = await conn.execute(
            """
            SELECT DISTINCT ON (s.id) s.name, r.status
            FROM sources s JOIN scrape_runs r ON r.source_id = s.id
            WHERE s.enabled AND r.status <> 'running'
            ORDER BY s.id, r.started_at DESC
            """
        )
        for row in await cur.fetchall():
            self.last_status.labels(source=row["name"], status=row["status"]).set(1)
        cur = await conn.execute(
            """
            SELECT s.name, extract(epoch FROM now() - min(r.started_at))::float8 AS age
            FROM sources s JOIN scrape_runs r ON r.source_id = s.id
            WHERE s.enabled AND r.status = 'pending_extraction'
              AND r.started_at > coalesce(
                  (SELECT max(started_at) FROM scrape_runs WHERE source_id = s.id AND status = 'success'),
                  '-infinity')
            GROUP BY s.name
            """
        )
        for row in await cur.fetchall():
            self.pending_age.labels(source=row["name"]).set(row["age"])
        cur = await conn.execute(
            "SELECT llm_provider, sum(llm_calls)::int AS n FROM scrape_runs "
            "WHERE llm_provider IS NOT NULL AND started_at > now() - interval '24 hours' GROUP BY 1"
        )
        for row in await cur.fetchall():
            self.llm_calls.labels(provider=row["llm_provider"]).set(row["n"])

    async def run(
        self,
        pool: Pool,
        embedder: OpenAIEmbedder | None,
        embedding_model: str,
        interval: float,
    ) -> None:
        """Refresh forever."""
        while True:
            await self.refresh(pool, embedder, embedding_model)
            await asyncio.sleep(interval)
