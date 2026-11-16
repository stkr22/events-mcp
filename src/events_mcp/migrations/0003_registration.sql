-- Two-phase events: a registration / ticket-sale phase before the event itself.
-- starts_at/ends_at stay the event's own window; these columns describe the phase before it.

ALTER TABLE events
    ADD COLUMN registration_opens_at timestamptz,
    ADD COLUMN registration_closes_at timestamptz,
    -- Where to buy or register; may be another site (a ticket seller).
    ADD COLUMN tickets_url text,
    ADD CONSTRAINT events_registration_order
        CHECK (registration_opens_at IS NULL OR registration_closes_at IS NULL
               OR registration_closes_at >= registration_opens_at);

CREATE INDEX events_registration_closes_idx ON events (registration_closes_at)
    WHERE registration_closes_at IS NOT NULL;

-- New columns go last so CREATE OR REPLACE keeps the view's existing columns and grants.
-- registration_state is NULL when the item says nothing about registration. Without a stated close,
-- registration is taken to run until the item ends.
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
    e.last_seen < now() - greatest(3 * s.scrape_interval, interval '3 days') AS is_stale,
    e.registration_opens_at,
    e.registration_closes_at,
    e.tickets_url,
    CASE
        WHEN e.registration_opens_at IS NULL AND e.registration_closes_at IS NULL AND e.tickets_url IS NULL THEN NULL
        WHEN e.registration_opens_at > now() THEN 'not_yet_open'
        WHEN coalesce(e.registration_closes_at, w.window_end) < now() THEN 'closed'
        ELSE 'open'
    END AS registration_state
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
