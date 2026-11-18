-- What the agent told a user about an event, and what became of it. Working state for suggesting and
-- planning, not a knowledge store: no notes, reminders or preferences live here. Planning itself
-- (to-dos, reminders, notes, attended) belongs in the user's task manager; task_ref points at it.
-- `events` stays user-independent; this table is the only per-user data in the database and, unlike
-- everything else here, cannot be rebuilt by crawling.

CREATE TABLE event_user_state (
    event_id bigint NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    -- One person for now; no tool takes it. Part of the key so a second person needs no migration.
    owner    text NOT NULL DEFAULT 'default',
    status   text NOT NULL CHECK (status IN ('raised', 'dismissed', 'planned')),
    -- Opaque pointer into the task manager; the server never resolves it.
    task_ref text CHECK (char_length(task_ref) BETWEEN 1 AND 200),
    -- What the user was told, to notice that the crawler learned something different since.
    snapshot_title                   text,
    snapshot_starts_at               timestamptz,
    snapshot_registration_opens_at   timestamptz,
    snapshot_registration_closes_at  timestamptz,
    snapshot_event_status            text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, owner),
    CHECK (task_ref IS NULL OR status = 'planned')
);
