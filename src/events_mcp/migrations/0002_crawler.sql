-- Crawler support: per-source config and runtime state, richer scrape_runs, kind/status vocabularies.

ALTER TABLE sources
    ADD COLUMN enabled boolean NOT NULL DEFAULT true,
    -- The synced sources.yaml entry; the crawler never edits it.
    ADD COLUMN config jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN last_content_hash text,
    -- Hash of the content the last successful extraction ran on; equal hash means nothing to extract.
    ADD COLUMN last_extracted_hash text,
    -- Start of the run that extracted it: rows with last_seen at or after this were present in it.
    ADD COLUMN last_extracted_at timestamptz,
    ADD COLUMN selector_schema jsonb,
    ADD COLUMN selector_schema_at timestamptz,
    ADD COLUMN etag text,
    ADD COLUMN last_modified text;

ALTER TABLE sources DROP CONSTRAINT sources_type_check;
ALTER TABLE sources ADD CONSTRAINT sources_type_check
    CHECK (type IN ('venue', 'museum', 'youtube', 'feed', 'shop'));

ALTER TABLE scrape_runs
    ADD COLUMN extractor text,
    ADD COLUMN llm_provider text,
    ADD COLUMN llm_calls integer NOT NULL DEFAULT 0,
    ADD COLUMN llm_input_chars integer NOT NULL DEFAULT 0,
    ADD COLUMN content_hash text,
    ADD COLUMN snapshot_key text,
    ADD COLUMN items_new integer NOT NULL DEFAULT 0,
    ADD COLUMN items_updated integer NOT NULL DEFAULT 0,
    ADD COLUMN items_touched integer NOT NULL DEFAULT 0,
    ADD COLUMN items_rejected integer NOT NULL DEFAULT 0;

ALTER TABLE scrape_runs DROP CONSTRAINT scrape_runs_status_check;
ALTER TABLE scrape_runs ADD CONSTRAINT scrape_runs_status_check
    CHECK (status IN ('running', 'success', 'partial', 'failed', 'pending_extraction', 'blocked_robots', 'skipped'));

ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check
    CHECK (kind IN ('event', 'exhibition', 'video', 'sale', 'other'));

-- sold_out stays: the Phase 1 vocabulary had it and existing rows may carry it.
ALTER TABLE events DROP CONSTRAINT events_status_check;
ALTER TABLE events ADD CONSTRAINT events_status_check
    CHECK (status IN ('scheduled', 'cancelled', 'postponed', 'rescheduled', 'sold_out'));

-- Same columns as before, so the dependent views stay valid. New kinds: 'other' behaves like
-- an event; 'sale' like an exhibition (a campaign runs until its end date, open-ended without one).
CREATE OR REPLACE VIEW event_lifecycle AS
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
                WHEN 'other' THEN e.starts_at + interval '12 hours'
                WHEN 'video' THEN e.starts_at
            END
        ) AS window_end
) w;

-- Event-style lookups by source for the run loop.
CREATE INDEX scrape_runs_status_idx ON scrape_runs (source_id, status, started_at DESC);
