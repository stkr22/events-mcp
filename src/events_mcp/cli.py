"""Command line: ``serve`` (default), ``migrate``, ``import``, ``backfill-embeddings`` and ``scrape``."""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from prometheus_client import start_http_server

from events_mcp.config import Settings, get_settings
from events_mcp.db import connect, migrate_with_retry
from events_mcp.embeddings import document_text
from events_mcp.ingest import Item, backfill_embeddings, finish_run, start_run, upsert_event, upsert_source
from events_mcp.server import create_app, make_embedder

log = logging.getLogger("events_mcp")


def _admin() -> int:
    try:
        from events_mcp.admin.app import create_admin_app  # noqa: PLC0415 - needs the [admin] extra
        from events_mcp.admin.settings import AdminSettings  # noqa: PLC0415
    except ImportError:
        print("the admin app needs the [admin] extra: pip install events-mcp[admin]", file=sys.stderr)
        return 2
    settings = AdminSettings()
    uvicorn.run(create_admin_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


def _serve(settings: Settings) -> None:
    app, state = create_app(settings, make_embedder(settings))
    start_http_server(settings.metrics_port, registry=state.metrics.registry)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, proxy_headers=False)


async def _migrate(settings: Settings) -> None:
    applied = await migrate_with_retry(settings.database_url.get_secret_value(), attempts=3)
    print("applied:", ", ".join(applied) or "nothing pending")


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_settings().tz)
    return dt


def _item(obj: dict[str, Any]) -> Item:
    return Item(
        title=obj["title"],
        dedupe_key=obj["dedupe_key"],
        kind=obj["kind"],
        summary=obj.get("summary"),
        raw_text=obj.get("raw_text"),
        url=obj.get("url"),
        starts_at=_parse_dt(obj.get("starts_at")),
        ends_at=_parse_dt(obj.get("ends_at")),
        city=obj.get("city"),
        status=obj.get("status"),
        registration_opens_at=_parse_dt(obj.get("registration_opens_at")),
        registration_closes_at=_parse_dt(obj.get("registration_closes_at")),
        tickets_url=obj.get("tickets_url"),
        venue=obj.get("venue"),
    )


async def _import(settings: Settings, args: argparse.Namespace) -> None:
    """Upsert a JSON-lines file of items for one source, recorded as one scrape_run."""
    text = await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    items = [_item(obj) for obj in lines]
    embedder = make_embedder(settings) if not args.no_embed else None
    vectors: list[list[float]] | None = None
    if embedder is not None:
        texts = [
            document_text(i.title, i.summary, i.raw_text, i.kind, i.city, (i.venue or {}).get("name")) for i in items
        ]
        vectors = await embedder.embed(texts)
        if vectors is None:
            log.warning("embedding endpoint unavailable; importing without embeddings (run backfill-embeddings later)")
        await embedder.aclose()
    async with await connect(settings.database_url.get_secret_value()) as conn:
        source_id = await upsert_source(conn, args.source_name, args.source_type, args.source_url, args.city)
        run_id = await start_run(conn, source_id)
        inserted = 0
        try:
            async with conn.transaction():
                for n, item in enumerate(items):
                    vec = vectors[n] if vectors else None
                    _, new = await upsert_event(conn, source_id, item, vec, settings.embedding_model if vec else None)
                    inserted += int(new)
        except Exception as exc:
            await finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
            raise
        await finish_run(conn, run_id, "success", len(items))
    print(f"{len(items)} items, {inserted} new, {len(items) - inserted} updated")


async def _import_sources(settings: Settings, args: argparse.Namespace) -> None:
    """Load a sources.yaml into the database once (defaults and sources)."""
    from events_mcp.crawl.store import import_file  # noqa: PLC0415 - needs PyYAML, in the crawler image only

    async with await connect(settings.database_url.get_secret_value()) as conn:
        n = await import_file(conn, args.file, actor="import")
    print(f"imported {n} sources")


async def _backfill(settings: Settings, args: argparse.Namespace) -> None:
    embedder = make_embedder(settings)
    if embedder is None:
        sys.exit("EVENTS_EMBEDDING_BASE_URL is not set")
    async with await connect(settings.database_url.get_secret_value()) as conn:
        n = await backfill_embeddings(conn, embedder, args.batch_size)
    await embedder.aclose()
    print(f"embedded {n} items")


def _scrape(args: argparse.Namespace) -> int:
    try:
        from events_mcp.crawl.config import CrawlerSettings  # noqa: PLC0415 - optional [crawl] dependencies
        from events_mcp.crawl.runner import scrape  # noqa: PLC0415
    except ImportError:
        print("the crawler needs the [crawl] extra: pip install events-mcp[crawl]", file=sys.stderr)
        return 2
    settings = CrawlerSettings()
    if args.config:
        settings.sources_file = args.config
    if args.command == "reextract":
        from events_mcp.crawl.runner import reextract  # noqa: PLC0415

        return asyncio.run(reextract(settings, args.source, args.snapshot))
    return asyncio.run(
        scrape(settings, names=args.sources, force=args.force, dry_run=args.dry_run, extractor=args.extractor)
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="events-mcp")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server (default)")
    sub.add_parser("migrate", help="apply pending migrations and exit")
    sub.add_parser("admin", help="run the admin app (sources and settings editor)")
    imp = sub.add_parser("import", help="upsert a JSON-lines file of items for one source")
    imp.add_argument("file")
    imp.add_argument("--source-name", required=True)
    imp.add_argument("--source-type", required=True, choices=["venue", "museum", "youtube", "feed", "shop"])
    imp.add_argument("--source-url", required=True)
    imp.add_argument("--city")
    imp.add_argument("--no-embed", action="store_true", help="skip the embedding endpoint")
    isrc = sub.add_parser("import-sources", help="load a sources.yaml into the database (replaces same-named sources)")
    isrc.add_argument("file", type=Path)
    sc = sub.add_parser("scrape", help="fetch the configured sources and upsert their items")
    sc.add_argument("--config", type=Path, help="read this sources.yaml instead of the database")
    sc.add_argument("--source", action="append", dest="sources", help="only this source (repeatable)")
    sc.add_argument("--force", action="store_true", help="ignore the due check and hash gating")
    sc.add_argument("--dry-run", action="store_true", help="print JSONL to stdout; write nothing")
    sc.add_argument("--extractor", help="override the tier (ical, rss, youtube, jsonld)")
    rx = sub.add_parser("reextract", help="rerun the LLM tier on a stored snapshot")
    rx.add_argument("--source", required=True)
    rx.add_argument("--snapshot", help="snapshot key (default: the newest)")
    rx.add_argument("--config", type=Path)
    bf = sub.add_parser("backfill-embeddings", help="embed items missing a vector from the configured model")
    bf.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args(argv)

    if args.command == "admin":
        sys.exit(_admin())
    if args.command in {"scrape", "reextract"}:
        sys.exit(_scrape(args))
    settings = get_settings()
    match args.command:
        case None | "serve":
            _serve(settings)
        case "migrate":
            asyncio.run(_migrate(settings))
        case "import-sources":
            asyncio.run(_import_sources(settings, args))
        case "import":
            asyncio.run(_import(settings, args))
        case "backfill-embeddings":
            asyncio.run(_backfill(settings, args))
