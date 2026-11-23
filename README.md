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
| `registration_deadlines(city, days, which, kind)` | Items whose stated registration / ticket-sale window closes (`which="closing"`, default) or opens (`"opening"`) within `days`, soonest first. Ended and cancelled items are left out |
| `get_event(event_id)` | One item, with page text, venue and source |
| `list_sources()` | Sources with item counts and their last successful scrape |
| `track_event(event_id, status, task_ref)` | Record that the user was told about an event (`raised`), turned it down (`dismissed`) or that it is now in their task manager (`planned`, `task_ref` = the task id). Working state, not a knowledge store |
| `my_events(status, changed)` | Tracked events with current data; `changes` shows what differs from what the user was told |
| `untrack_event(event_id)` | Forget one event |

Bare dates (`2026-11-21`) are local days in `EVENTS_TIMEZONE`. A `date_to` bound includes that whole day.

## Data model

- `sources`: venue sites, museums, channels and feeds (`type` is `venue`, `museum`, `youtube` or `feed`). A `youtube`
  source takes the channel's `channel_id` (`UC...`) and reads its uploads playlist (`UULF...`), which has the same videos
  as the channel feed without the Shorts, each with its full description
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

# one-off: delete URL-less rows that also exist with a URL (dry run; add --apply to delete)
events-mcp dedupe-linkless
events-mcp import-sources sources.yaml   # one-off: load defaults and sources into the database
events-mcp admin                     # the admin app (needs the [admin] extra or the -admin image)
events-mcp scrape [--config sources.yaml] [--source NAME ...] [--force] [--dry-run] [--extractor TIER]
events-mcp reextract --source NAME [--snapshot KEY]
```

`import` upserts one JSON object per line (`title`, `dedupe_key`, `kind`, optional `summary`,
`raw_text`, `url`, `starts_at`, `ends_at`, `registration_opens_at`, `registration_closes_at`, `tickets_url`, `city`, `status`,
`venue: {name, city, address, lat, lon}`).
The whole file is recorded as one `scrape_runs` row. Running the same import twice changes nothing
except `last_seen`.

## Crawler

`events-mcp scrape` reads the sources from the database (`sources.config` and `crawler_defaults`, edited in the admin app,
see below) and processes the due ones. A `--config sources.yaml` reads that file instead and, for a live run, syncs it
into the database as before (sources dropped from the file are disabled, never deleted); `events-mcp import-sources FILE`
loads such a file once. Install with `pip install events-mcp[crawl]` or use the
`ghcr.io/stkr22/events-mcp-crawler` image. A source is due when its `interval` has passed since the last success, after a
failed run on a 15 min / 30 min / 1 h backoff, or when the admin app asked for a run. A source whose stored config no
longer validates is logged and skipped; the others still run.

Each page is fetched politely (per-host rate limit, robots.txt; a robots block is the run status `blocked_robots`),
then run through the cheapest tier that yields items: iCal, RSS/Atom, YouTube uploads feed, schema.org Event
(JSON-LD/microdata). The result is hashed; an unchanged hash only bumps `last_seen` (`touch_source`) and costs
no extraction. `--dry-run` prints the import records as JSONL and writes nothing: use it to onboard a source.

Pages without structured data go through the **LLM tier**: cleaned markdown, hashed, then (only if the hash changed)
chunked by heading and sent to an OpenAI-compatible provider chain (`LLM_PROVIDER_ORDER`, default `mac,gemini`: the Mac
/ oMLX endpoint first, Gemini as fallback). The model only writes content fields; keys, validation and the write
are code. Code-side guards reject titles that are not on the page (fuzzy match >= 90), implausible or unparseable
dates, unknown kinds, and foreign URLs; rejections are counted in `scrape_runs.items_rejected`. If no provider
answers, the run is `pending_extraction`: nothing is touched, nothing is marked extracted, and the next run retries.
Budget per source: `max_llm_calls` (10) and `max_input_chars` (150000).

After a good LLM extraction of a listing (3 or more items) the crawler asks the LLM once for a CSS schema
(`base_selector` plus `title`, `url`, `date_text` selectors, from the trimmed HTML) and keeps it only if replaying it
reproduces at least 80% of the items the LLM found. From then on the **selectors tier** runs first and costs no LLM
call. If it later matches nothing, or more than half of its matches have no title (a redesign), the schema is dropped,
the LLM extracts again and writes a new one. Dates come from a day-first German/English date reader; a date without
a year takes the nearest plausible one. `type: shop` sources default to `kind: sale`.

`search_events` and `upcoming` leave out `kind=sale` unless `kind` or `kinds` asks for it; `ending_soon` includes
every kind.

**Registration phase.** An item can carry the phase before the event: `registration_opens_at`, `registration_closes_at`
and `tickets_url` (which may be another site). `starts_at`/`ends_at` stay the event's own window. Listings return a
derived `registration_state`: `not_yet_open`, `open` or `closed`; it is absent when the item says nothing about
registration. With no stated close, registration counts as open until the item ends. `search_events` and `upcoming`
take `registration=<state>`. The LLM tier reads the fields from the page, the JSON-LD tier from schema.org `offers`
(`validFrom`, `validThrough`, `url`); iCal, RSS, YouTube and the stored-selector tier do not carry them. A later run
that does not state them keeps the stored values; a new date pair replaces the old pair as a whole. Registration data
is usually on an event's own page, not on the listing the crawler reads, so expect it where the listing shows it.

**Tracking.** `event_user_state` holds, per event, what the agent told the user: `raised`, `dismissed` or `planned` (with an opaque
`task_ref` into the task manager), plus a snapshot of the title, start, registration window and status at that moment.
Every listing shows `tracked_status`; `hide_handled=true` leaves dismissed and planned events out (off by default, so a
search for a title still finds it). `my_events(changed=true)` compares the snapshot with the crawler's current data, which is
how a moved date or a newly found registration window reaches the agent. A postponed event that has no stable id or
URL becomes a new row at the source; the old one goes `is_stale`. It is deliberately **not** a place for notes, reminders,
calendar links or preferences: planning belongs in the user's task manager (Vikunja), long-term preferences elsewhere.
Rows for events that ended over 60 days ago are hidden and purged on the next write. This is the only per-user data in the
database and the one thing a re-crawl cannot rebuild, so it is covered by the database backup, not by the crawler.
The crawler role has no grant on it.

`render: browser` / `browser_stealth` fetch through a shared Chrome over CDP (`CDP_URL`, an IP: Chrome rejects
hostnames), one fresh context per page, at most one at a time. Stealth is per context only (UA, locale, timezone,
init scripts); Chrome's launch flags are never changed. Robots rules and pacing apply to browser renders too.

With `S3_*` set, the cleaned text is stored as `snapshots/<source>/<date>/<sha256>.md.zst` whenever the hash changes and
pruned after 90 days; `reextract` reruns the LLM tier on the newest (or a given) snapshot.

Environment: `DATABASE_URL` (or `EVENTS_DATABASE_URL`), `SOURCES_FILE` (optional, reads a file instead of the database), `USER_AGENT`, `GEOCODER_URL`.
The database role `events_crawler`, if it exists, is granted exactly the privileges it needs on every startup
of the server (`migrate`).

## Admin app

`events-mcp admin` (image `ghcr.io/stkr22/events-mcp-admin`) serves a React UI and a JSON API to edit the crawler's
sources and defaults and to see how each source is doing (last run, error, items, change history). Everything the
`sources.yaml` schema offers is editable; the API validates with the crawler's own models, so a saved source is one the
crawler accepts. Edits are logged with who made them and the values before and after (`source_config_history`).
"Check now" / "Re-extract now" set a flag the crawler picks up on its next tick (every 15 minutes); a source with stored
items cannot be deleted from the UI (disable it: deleting would cascade to its events and to what users were told).

signed session cookie. Being signed in is not enough: the user needs the Zitadel project role `events-admin` (`EVENTS_ADMIN_ADMIN_ROLE`). A
signed session cookie. Being signed in is not enough: the user's `sub` must be listed in `EVENTS_ADMIN_SUBJECTS`. A
refused user is told which role is missing and which roles arrived. The roles are read at sign-in (the `urn:zitadel:iam:org:project:roles` scope, from the ID token or userinfo), so a revoked role lasts until the session ends (8 h). State-changing requests from another origin are refused.

| Variable | |
|---|---|
| `EVENTS_ADMIN_DATABASE_URL` | the `events_admin` role: may edit the two config tables and read crawl state and events; nothing else |
| `EVENTS_ADMIN_OIDC_ISSUER`, `EVENTS_ADMIN_OIDC_CLIENT_ID` | e.g. `https://zitadel.example`; the redirect URI `<public url>/auth/callback` must be registered |
| `EVENTS_ADMIN_PUBLIC_BASE_URL` | where users reach it (https sets `Secure` cookies and the origin check) |
| `EVENTS_ADMIN_SESSION_SECRET` | at least 32 characters |
| `EVENTS_ADMIN_ADMIN_ROLE` | Zitadel project role that may edit (default `events-admin`); grant it in Zitadel |
| `EVENTS_ADMIN_DEV_AUTH` | `true` signs in as a fixed developer at `/auth/dev-login`; refused together with an issuer |

Local development: `EVENTS_ADMIN_DATABASE_URL=… EVENTS_ADMIN_DEV_AUTH=true events-mcp admin`, and `npm run dev` in
`frontend/` (the Vite server proxies `/api` and `/auth` to port 8080). The API documents itself at `/api/docs`.

## Metrics

| Metric | |
|---|---|
| `events_last_successful_scrape_timestamp_seconds{source}` | Newest successful `scrape_runs.finished_at` per source. A source that has never succeeded reports its creation time, so new sources get a grace period. Alert on `time() - …`. |
| `events_items{kind,lifecycle}` | item counts |
| `events_items_missing_embedding` | backfill backlog |
| `events_source_scrape_interval_seconds{source}`, `events_scrape_last_status{source,status}`, `events_source_items{source,lifecycle}`, `events_pending_extraction_seconds{source}`, `events_llm_calls_24h{provider}` | crawler health, enabled sources only |
| `events_scrape_consecutive_failures{source}` | runs since the last success that failed, were blocked or waited for an LLM; alert on `>= 3`, independent of the interval |
| `events_source_latest_starts_at_timestamp_seconds{source}` | newest `starts_at` among a source's live items; `... - time()` is its horizon in seconds |
| `events_db_up`, `events_embedding_endpoint_up` | refreshed every 60 s |
| `events_mcp_tool_calls_total{tool,outcome}`, `events_mcp_searches_total{semantic}` | usage |

## Development

```
uv sync --dev
EVENTS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest
```

The tests need a Postgres with pgvector (`docker run -e POSTGRES_PASSWORD=postgres -p 5432:5432
pgvector/pgvector:0.8.2-pg18`); they drop and recreate the `public` schema of that database.
