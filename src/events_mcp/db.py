"""Connection pool and the startup migration runner."""

import asyncio
import logging
from importlib import resources
from typing import Any, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row, tuple_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

# Arbitrary constant: serialises migrations if two pods ever start at once.
MIGRATION_LOCK_ID = 0x6576_656E_7473  # "events"


def migration_files() -> list[tuple[int, str, str]]:
    """Return ``(version, name, sql)`` for every bundled migration, in order."""
    found = []
    for entry in resources.files("events_mcp.migrations").iterdir():
        if entry.name.endswith(".sql"):
            version = int(entry.name.split("_", 1)[0])
            found.append((version, entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return found


async def migrate(conn: AsyncConnection[Any]) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the names applied."""
    applied: list[str] = []
    await conn.set_autocommit(True)
    await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    integer PRIMARY KEY,
                name       text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur = conn.cursor(row_factory=tuple_row)
        await cur.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in await cur.fetchall()}
        for version, name, sql in migration_files():
            if version in done:
                continue
            log.info("applying migration %s", name)
            async with conn.transaction():
                await conn.execute(sql.encode())
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (version, name),
                )
            applied.append(name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
    return applied


async def migrate_with_retry(dsn: str, attempts: int = 60, delay: float = 5.0) -> list[str]:
    """Run migrations, waiting for the database to come up (CNPG may still be starting)."""
    for attempt in range(1, attempts + 1):
        try:
            conn = await AsyncConnection.connect(dsn, connect_timeout=10)
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                raise
            log.warning("database not reachable (attempt %d/%d): %s", attempt, attempts, exc)
            await asyncio.sleep(delay)
            continue
        async with conn:
            return await migrate(conn)
    raise RuntimeError("database never became reachable")


Pool = AsyncConnectionPool[AsyncConnection[DictRow]]


def make_pool(dsn: str, max_size: int = 5) -> Pool:
    """Create (but do not open) the shared pool; rows come back as dicts."""
    return AsyncConnectionPool(
        dsn,
        connection_class=AsyncConnection[DictRow],
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )


async def connect(dsn: str) -> AsyncConnection[DictRow]:
    """One autocommit connection with dict rows, for the CLI commands."""
    conn = await AsyncConnection.connect(dsn, row_factory=dict_row, autocommit=True)  # ty: ignore[invalid-argument-type]
    return cast("AsyncConnection[DictRow]", conn)
