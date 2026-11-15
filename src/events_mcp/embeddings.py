"""Client for the OpenAI-compatible embedding endpoint (the Mac's oMLX), with a short circuit breaker.

Semantic search depends on the Mac being awake. Every failure is turned into ``None`` so callers
fall back to keyword search, and after a failure the endpoint is skipped for a while so a sleeping
Mac costs one timeout, not one per query.
"""

import logging
import time
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class Embedder(Protocol):
    """Anything that turns texts into vectors, or ``None`` when it cannot right now."""

    model: str

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed ``texts`` in order; ``None`` means unavailable."""
        ...


class OpenAIEmbedder:
    """``POST {base_url}/embeddings`` with a circuit breaker."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensions: int,
        api_key: str = "local",
        timeout_sec: float = 5.0,
        retry_after_sec: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client; no request is made until the first ``embed``."""
        self.model = model
        self.dimensions = dimensions
        self.retry_after_sec = retry_after_sec
        self._down_until = 0.0
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_sec, connect=min(timeout_sec, 2.0)),
            transport=transport,
        )

    @property
    def available(self) -> bool:
        """False while the breaker is open after a recent failure."""
        return time.monotonic() >= self._down_until

    def _trip(self, reason: str) -> None:
        if self.available:
            log.warning("embedding endpoint unavailable, keyword-only for %.0fs: %s", self.retry_after_sec, reason)
        self._down_until = time.monotonic() + self.retry_after_sec

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed ``texts``; ``None`` if the endpoint is down, slow or returns something unusable."""
        if not texts:
            return []
        if not self.available:
            return None
        try:
            resp = await self._client.post("embeddings", json={"model": self.model, "input": texts})
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            self._trip(f"{type(exc).__name__}: {exc}")
            return None
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            self._trip(f"expected {len(texts)} vectors of {self.dimensions} dims")
            return None
        return vectors

    async def probe(self) -> bool:
        """Cheap liveness check (``GET /models``) for the metrics loop; also resets the breaker."""
        try:
            resp = await self._client.get("models")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            self._trip(f"{type(exc).__name__}: {exc}")
            return False
        self._down_until = 0.0
        return True

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


def vector_literal(vec: list[float]) -> str:
    """Render a vector in pgvector's text format, for a ``%s::vector`` parameter."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def document_text(
    title: str, summary: str | None, raw_text: str | None, kind: str, city: str | None, venue: str | None
) -> str:
    """Text that is embedded for one item. Shared by ingest and backfill so both produce the same vectors."""
    where = " ".join(p for p in (f"in {city}" if city else "", f"at {venue}" if venue else "") if p)
    parts = [title, f"{kind} {where}".strip(), summary or "", (raw_text or "")[:2000]]
    return "\n".join(p for p in parts if p)
