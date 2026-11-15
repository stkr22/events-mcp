-- Initial schema. In the cluster the extensions are created by CNPG at bootstrap
-- (as superuser); IF NOT EXISTS makes these no-ops there and lets a superuser test
-- database start from nothing.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS cube;
CREATE EXTENSION IF NOT EXISTS earthdistance;

CREATE TABLE sources (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Stable human label; it is the `source` label on the scrape metrics.
    name            text NOT NULL UNIQUE,
    type            text NOT NULL CHECK (type IN ('venue', 'museum', 'youtube', 'feed')),
    url             text NOT NULL UNIQUE,
    city            text,
    scrape_interval interval NOT NULL DEFAULT interval '1 day',
    last_run        timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE venues (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name    text NOT NULL,
    city    text,
    address text,
    lat     double precision CHECK (lat BETWEEN -90 AND 90),
    lon     double precision CHECK (lon BETWEEN -180 AND 180),
    CHECK ((lat IS NULL) = (lon IS NULL))
);
-- NULLS NOT DISTINCT so a venue without a city still upserts onto one row.
CREATE UNIQUE INDEX venues_name_city_key ON venues (name, city) NULLS NOT DISTINCT;

CREATE TABLE events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id       bigint NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    venue_id        bigint REFERENCES venues (id) ON DELETE SET NULL,
    title           text NOT NULL,
    summary         text,
    -- Cleaned page text (markdown), never HTML; the only durable copy once the page is gone.
    raw_text        text CHECK (char_length(raw_text) <= 20000),
    url             text,
    -- NULL for undated items (permanent collections, evergreen pages).
    starts_at       timestamptz,
    ends_at         timestamptz,
    city            text,
    kind            text NOT NULL CHECK (kind IN ('event', 'exhibition', 'video')),
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    dedupe_key      text NOT NULL,
    embedding       vector(1024),
    embedding_model text,
    -- Only states the scraper can observe; lifecycle (ended, stale) is computed in the views.
    status          text CHECK (status IN ('cancelled', 'postponed', 'sold_out')),
    search          tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('german', coalesce(title, '')), 'A')
        || setweight(to_tsvector('german', coalesce(summary, '')), 'B')
        || setweight(to_tsvector('german', coalesce(raw_text, '')), 'C')
        || setweight(to_tsvector('english', coalesce(title, '')), 'A')
        || setweight(to_tsvector('english', coalesce(summary, '')), 'B')
        || setweight(to_tsvector('english', coalesce(raw_text, '')), 'C')
    ) STORED,
    CONSTRAINT events_source_dedupe_key UNIQUE (source_id, dedupe_key),
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at),
    CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);

CREATE INDEX events_starts_at_idx ON events (starts_at);
CREATE INDEX events_ends_at_idx ON events (ends_at);
CREATE INDEX events_city_idx ON events (lower(city));
CREATE INDEX events_source_id_idx ON events (source_id);
CREATE INDEX events_venue_id_idx ON events (venue_id);
CREATE INDEX events_search_idx ON events USING gin (search);
-- No vector index on purpose: a brute-force scan is fine at this volume.

CREATE TABLE scrape_runs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   bigint NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'partial', 'failed')),
    items_seen  integer NOT NULL DEFAULT 0 CHECK (items_seen >= 0),
    error       text
);
CREATE INDEX scrape_runs_source_started_idx ON scrape_runs (source_id, started_at DESC);

-- Lifecycle is derived at query time, so nothing has to flip flags and nothing drifts.
--
-- window_start/window_end is the span an item occupies on the calendar:
--   event       starts_at .. ends_at, or starts_at + 12h when the listing gives no end
--   exhibition  starts_at .. ends_at; a missing end means open-ended (NULL)
--   video       the publish instant (starts_at) on both sides; videos never "end"
-- Items with neither date are 'undated' and never match a date window.
CREATE VIEW event_lifecycle AS
SELECT
    e.id, e.source_id, e.venue_id, e.title, e.summary, e.raw_text, e.url,
    e.starts_at, e.ends_at, e.city, e.kind, e.first_seen, e.last_seen,
    e.dedupe_key, e.embedding, e.embedding_model, e.status, e.search,
    w.window_start,
    w.window_end,
    CASE
        WHEN e.kind = 'video' THEN 'published'
        WHEN e.starts_at IS NULL AND e.ends_at IS NULL THEN 'undated'
        WHEN w.window_end < now() THEN 'ended'
        WHEN e.starts_at > now() THEN 'upcoming'
        ELSE 'ongoing'
    END AS lifecycle,
    -- Not seen in three scrape intervals (at least three days): probably gone from the site.
    e.last_seen < now() - greatest(3 * s.scrape_interval, interval '3 days') AS is_stale
FROM events e
JOIN sources s ON s.id = e.source_id
CROSS JOIN LATERAL (
    SELECT
        e.starts_at AS window_start,
        coalesce(
            e.ends_at,
            CASE e.kind
                WHEN 'event' THEN e.starts_at + interval '12 hours'
                WHEN 'video' THEN e.starts_at
            END
        ) AS window_end
) w;

CREATE VIEW upcoming AS
SELECT * FROM event_lifecycle
WHERE lifecycle IN ('upcoming', 'ongoing') AND status IS DISTINCT FROM 'cancelled';

CREATE VIEW ended AS
SELECT * FROM event_lifecycle WHERE lifecycle = 'ended';

CREATE VIEW stale AS
SELECT * FROM event_lifecycle WHERE is_stale AND lifecycle <> 'ended';
