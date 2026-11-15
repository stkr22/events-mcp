"""Prometheus metrics. Database-derived gauges are refreshed by a background loop, not per scrape."""

import asyncio
import logging

from prometheus_client import CollectorRegistry, Counter, Gauge

from events_mcp.db import Pool
from events_mcp.embeddings import OpenAIEmbedder

log = logging.getLogger(__name__)


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
