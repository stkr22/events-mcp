"""Raw page snapshots in Garage (S3): zstd markdown, written only when the content hash changes."""

import asyncio
import logging
from compression import zstd
from datetime import UTC, date, datetime, timedelta
from typing import Any

from events_mcp.crawl.config import CrawlerSettings

log = logging.getLogger(__name__)


def snapshot_key(source: str, digest: str, today: date) -> str:
    """``snapshots/<source>/<YYYY-MM-DD>/<sha256>.md.zst``."""
    return f"snapshots/{source}/{today.isoformat()}/{digest}.md.zst"


class SnapshotStore:
    """Thin S3 wrapper; boto3 is synchronous, so every call goes through a thread."""

    def __init__(self, settings: CrawlerSettings, client: Any = None) -> None:
        """``client`` is injectable for tests."""
        assert settings.s3_bucket is not None  # noqa: S101 - checked by from_settings
        self.bucket = settings.s3_bucket
        self.retention = timedelta(days=settings.snapshot_retention_days)
        if client is None:
            import boto3  # noqa: PLC0415
            from botocore.config import Config  # noqa: PLC0415

            assert settings.s3_secret_access_key is not None  # noqa: S101
            client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint,
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key_id,
                aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
                config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}, connect_timeout=5),
            )
        self._s3 = client

    @classmethod
    def from_settings(cls, settings: CrawlerSettings) -> SnapshotStore | None:
        """``None`` when S3 is not configured: snapshots are then skipped, never required."""
        if settings.s3_bucket and settings.s3_endpoint and settings.s3_access_key_id and settings.s3_secret_access_key:
            return cls(settings)
        return None

    async def put(self, key: str, text: str) -> None:
        """Compress and upload."""
        body = zstd.compress(text.encode("utf-8"))
        await asyncio.to_thread(self._s3.put_object, Bucket=self.bucket, Key=key, Body=body)

    async def get(self, key: str) -> str:
        """Download and decompress."""
        resp = await asyncio.to_thread(self._s3.get_object, Bucket=self.bucket, Key=key)
        data = await asyncio.to_thread(resp["Body"].read)
        return zstd.decompress(data).decode("utf-8")

    async def latest(self, source: str) -> str | None:
        """Key of the newest snapshot of ``source``."""
        keys = await self._list(f"snapshots/{source}/")
        return max(keys, key=lambda k: k[1])[0] if keys else None

    async def _list(self, prefix: str) -> list[tuple[str, datetime]]:
        def run() -> list[tuple[str, datetime]]:
            out: list[tuple[str, datetime]] = []
            for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                out.extend((o["Key"], o["LastModified"]) for o in page.get("Contents", []))
            return out

        return await asyncio.to_thread(run)

    async def prune(self) -> int:
        """Delete snapshots older than the retention; used in place of a bucket lifecycle rule."""
        cutoff = datetime.now(UTC) - self.retention
        old = [k for k, ts in await self._list("snapshots/") if ts < cutoff]
        for i in range(0, len(old), 1000):
            batch = [{"Key": k} for k in old[i : i + 1000]]
            await asyncio.to_thread(self._s3.delete_objects, Bucket=self.bucket, Delete={"Objects": batch})
        if old:
            log.info("pruned %d snapshots older than %s", len(old), self.retention)
        return len(old)
