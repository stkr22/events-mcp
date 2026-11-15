# events-mcp

An MCP server (streamable HTTP) over a Postgres + pgvector store of scraped venue events, museum
exhibitions and YouTube channel content. A personal assistant uses it to answer questions such as:

- "What's on this weekend in Hamburg?"
- "Which exhibitions end in the next 3 weeks?"
- "Anything new from <channel/venue> since last week?"
- "Something quiet and arty for a rainy day"

Postgres is the source of truth. The scraper is a separate project that writes through the same
upsert SQL (`events_mcp.ingest`). This server owns the schema: its migrations run at startup.

## Tools

| Tool | What it does |
|---|---|
| `search_events(query, date_from, date_to, city, kind, source, new_since, include_ended, limit)` | Hybrid search. Full-text search (German + English stems) always runs. pgvector similarity runs when the embedding endpoint answers. The two are fused by reciprocal rank. When the endpoint is down, the result says `semantic_search: "unavailable"` and holds keyword matches only. |
| `upcoming(city, days, kind)` | Items running now or starting within `days`; cancelled items are left out |
| `ending_soon(city, days, kind)` | Items whose explicit `ends_at` falls within `days` |
| `get_event(event_id)` | One item, with page text, venue and source |
| `list_sources()` | Sources with item counts and their last successful scrape |

Bare dates (`2026-11-21`) are local days in `EVENTS_TIMEZONE`. A `date_to` bound includes that whole day.

## Data model

- `sources`: venue sites, museums, channels and feeds (`type` is `venue`, `museum`, `youtube` or `feed`)
- `venues`: name, city, address, lat/lon (geocoded at scrape time; `cube` + `earthdistance` are loaded
  for distance queries)
- `events`: one row per listing, unique on `(source_id, dedupe_key)`. Upserts keep `first_seen`
  and move `last_seen`. A stored embedding survives an upsert that leaves title, summary and raw_text
  unchanged; otherwise it is cleared, and `backfill-embeddings` re-embeds the row. `raw_text` holds the
  cleaned page text (never HTML), capped at 20 000 characters.
- `scrape_runs`: one row per scrape with status, items seen and error

The generated `search` column combines `to_tsvector('german', …)` and `to_tsvector('english', …)`
over title, summary and raw_text (weights A/B/C) and has one GIN index. There is no vector index;
a brute-force scan is fine at this volume.

Lifecycle is computed at query time in views, never stored:

| View | Contents |
|---|---|
| `event_lifecycle` | every item plus `window_start`/`window_end`, `lifecycle` (`upcoming`, `ongoing`, `ended`, `undated`, `published`) and `is_stale` |
| `upcoming` | `upcoming` or `ongoing`, not cancelled |
| `ended` | the calendar window has passed |
| `stale` | not seen for 3 scrape intervals (at least 3 days) and not ended |

An event without an end time is treated as lasting 12 hours. An exhibition without an end date
stays open-ended. A video occupies its publish instant and never ends.

## Configuration

| Variable | Default | |
|---|---|---|
| `EVENTS_DATABASE_URL` | required | libpq URL |
| `EVENTS_MCP_TOKEN` | unset | Static bearer token. If unset, the server runs unauthenticated (development only). |
| `EVENTS_EMBEDDING_BASE_URL` | unset | OpenAI-compatible base URL (`…/v1`); unset disables semantic search |
| `EVENTS_EMBEDDING_MODEL` | `Qwen3-Embedding-0.6B-8bit` | must produce 1024-dim vectors (the column is `vector(1024)`) |
| `EVENTS_EMBEDDING_TIMEOUT_SEC` | `5` | per request |
| `EVENTS_EMBEDDING_RETRY_AFTER_SEC` | `60` | how long a failed endpoint is skipped before being retried |
| `EVENTS_SEMANTIC_MAX_DISTANCE` | `0.75` | cosine distance beyond which a vector hit is dropped. With Qwen3-Embedding-0.6B, relevant listings scored 0.49–0.64 and unrelated ones 0.83+. Each semantic hit reports `semantic_distance`. |
| `EVENTS_EMBEDDING_QUERY_PREFIX` | Qwen3 instruction | prepended to search queries only, never to documents |
| `EVENTS_TIMEZONE` | `Europe/Berlin` | |
| `EVENTS_PORT` / `EVENTS_METRICS_PORT` | `8000` / `9090` | MCP at `/mcp`, `/healthz`, `/readyz`; Prometheus on its own port |

## Commands

```
events-mcp serve                     # default: migrate, then serve
events-mcp migrate
events-mcp import items.jsonl --source-name "Kunsthalle" --source-type museum \
    --source-url https://www.hamburger-kunsthalle.de --city Hamburg
events-mcp backfill-embeddings
```

`import` upserts one JSON object per line (`title`, `dedupe_key`, `kind`, optional `summary`,
`raw_text`, `url`, `starts_at`, `ends_at`, `city`, `status`, `venue: {name, city, address, lat, lon}`).
The whole file is recorded as one `scrape_runs` row. Running the same import twice changes nothing
except `last_seen`.

## Metrics

| Metric | |
|---|---|
| `events_last_successful_scrape_timestamp_seconds{source}` | Newest successful `scrape_runs.finished_at` per source. A source that has never succeeded reports its creation time, so new sources get a grace period. Alert on `time() - …`. |
| `events_items{kind,lifecycle}` | item counts |
| `events_items_missing_embedding` | backfill backlog |
| `events_db_up`, `events_embedding_endpoint_up` | refreshed every 60 s |
| `events_mcp_tool_calls_total{tool,outcome}`, `events_mcp_searches_total{semantic}` | usage |

## Development

```
uv sync --dev
EVENTS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest
```

The tests need a Postgres with pgvector (`docker run -e POSTGRES_PASSWORD=postgres -p 5432:5432
pgvector/pgvector:0.8.2-pg18`); they drop and recreate the `public` schema of that database.
