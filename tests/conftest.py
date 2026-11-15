import hashlib
import math
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from events_mcp.db import Pool, connect, make_pool, migrate
from events_mcp.ingest import Item, upsert_event, upsert_source

DSN = os.environ.get("EVENTS_TEST_DATABASE_URL")
DIMS = 1024


def needs_db() -> None:
    if DSN is None:
        pytest.skip("EVENTS_TEST_DATABASE_URL not set")


class FakeEmbedder:
    """Bag-of-concepts vectors: texts sharing a concept word land close together."""

    model = "fake-embedding"
    CONCEPTS = ("quiet", "art", "music", "loud", "food", "science")

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        self.calls += 1
        if not self.up:
            return None
        return [self.vector(t) for t in texts]

    @classmethod
    def vector(cls, text: str) -> list[float]:
        text = text.lower()
        vec = [0.0] * DIMS
        for i, concept in enumerate(cls.CONCEPTS):
            if concept in text:
                vec[i] = 1.0
        # Tiny text-specific component so no two vectors are identical.
        h = int(hashlib.sha256(text.encode()).hexdigest(), 16)
        vec[100 + h % 800] = 0.05
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec]


@pytest.fixture(scope="session")
async def schema() -> AsyncIterator[str]:
    needs_db()
    assert DSN is not None
    async with await AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        await migrate(conn)
    yield DSN


@pytest.fixture
async def conn(schema: str) -> AsyncIterator[AsyncConnection[DictRow]]:
    async with await connect(schema) as c:
        await c.execute("TRUNCATE sources, venues, events, scrape_runs RESTART IDENTITY CASCADE")
        yield c


@pytest.fixture
async def pool(conn: AsyncConnection[DictRow], schema: str) -> AsyncIterator[Pool]:
    p = make_pool(schema)
    await p.open(wait=True)
    yield p
    await p.close()


NOW = datetime.now(UTC)


def days(n: float) -> datetime:
    return NOW + timedelta(days=n)


SEED: list[tuple[str, Item]] = [
    (
        "kunsthalle",
        Item(
            title="Licht und Schatten: Malerei der Romantik",
            dedupe_key="ex-romantik",
            kind="exhibition",
            summary="Stille Landschaftsgemälde und Ausstellungen zur Malerei. Quiet art for contemplation.",
            starts_at=days(-30),
            ends_at=days(10),
            city="Hamburg",
            venue={
                "name": "Kunsthalle",
                "city": "Hamburg",
                "address": "Glockengießerwall 5",
                "lat": 53.55,
                "lon": 10.0,
            },
        ),
    ),
    (
        "kunsthalle",
        Item(
            title="Modern Paintings from the North",
            dedupe_key="ex-north",
            kind="exhibition",
            summary="A survey of paintings by Nordic artists. Art.",
            starts_at=days(-5),
            ends_at=days(60),
            city="Hamburg",
            venue={"name": "Kunsthalle", "city": "Hamburg"},
        ),
    ),
    (
        "fabrik",
        Item(
            title="Punk Night",
            dedupe_key="ev-punk",
            kind="event",
            summary="Loud music, three bands.",
            starts_at=days(2),
            city="Hamburg",
            venue={"name": "Fabrik", "city": "Hamburg"},
        ),
    ),
    (
        "fabrik",
        Item(
            title="Jazzkonzert im Hof",
            dedupe_key="ev-jazz-past",
            kind="event",
            summary="Music under the stars.",
            starts_at=days(-3),
            city="Hamburg",
        ),
    ),
    (
        "fabrik",
        Item(
            title="Street Food Market",
            dedupe_key="ev-food-berlin",
            kind="event",
            summary="Food trucks and quiet acoustic music.",
            starts_at=days(5),
            city="Berlin",
            status="cancelled",
        ),
    ),
    (
        "channel",
        Item(
            title="How telescopes work",
            dedupe_key="yt-abc",
            kind="video",
            summary="Science explainer.",
            starts_at=days(-2),
        ),
    ),
]


@pytest.fixture
async def seeded(conn: AsyncConnection[DictRow]) -> dict[str, int]:
    sources = {
        "kunsthalle": await upsert_source(
            conn, "Kunsthalle Hamburg", "museum", "https://kunsthalle.example", "Hamburg"
        ),
        "fabrik": await upsert_source(conn, "Fabrik", "venue", "https://fabrik.example", "Hamburg"),
        "channel": await upsert_source(conn, "Space Channel", "youtube", "https://youtube.example/space"),
    }
    ids = {}
    for src, item in SEED:
        vec = FakeEmbedder.vector(f"{item.title} {item.summary}")
        ids[item.dedupe_key], _ = await upsert_event(conn, sources[src], item, vec, FakeEmbedder.model)
    return ids
