"""Nominatim lookups for venues the database has not seen. Failures never fail a run."""

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class Geocoder:
    """One request per second at most, as the Nominatim usage policy demands."""

    def __init__(self, url: str, user_agent: str, client: httpx.AsyncClient | None = None) -> None:
        """``client`` is injectable for tests."""
        self.url = url
        self._client = client or httpx.AsyncClient(timeout=15.0, headers={"User-Agent": user_agent})
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        """Close the client."""
        await self._client.aclose()

    async def lookup(self, name: str, address: str | None, city: str | None) -> tuple[float, float] | None:
        """``(lat, lon)`` of the best match, or ``None`` on no match or any error."""
        query = ", ".join(p for p in (name if not address else None, address, city) if p)
        if not query:
            return None
        async with self._lock:
            wait = 1.0 - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = await self._client.get(self.url, params={"q": query, "format": "jsonv2", "limit": 1})
                resp.raise_for_status()
                hits: list[dict[str, Any]] = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("geocoding %r failed: %s", query, exc)
                return None
        if not hits:
            return None
        try:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
        except KeyError, ValueError:
            return None
