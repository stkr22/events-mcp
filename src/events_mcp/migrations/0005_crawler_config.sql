-- Crawler configuration lives in the database instead of a ConfigMap: sources.config holds the entry
-- as written (defaults not applied), crawler_defaults the file-level defaults. An admin app edits both;
-- the crawler reads them on every run.

CREATE TABLE crawler_defaults (
    id         smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    config     jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO crawler_defaults DEFAULT VALUES;

ALTER TABLE sources
    -- "Run now": 'due' skips the interval check, 'full' also ignores the unchanged-page gate (use after
    -- editing the intent). The crawler clears it when it picks the source up.
    ADD COLUMN run_request text CHECK (run_request IN ('due', 'full')),
    ADD COLUMN run_requested_at timestamptz,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD CHECK ((run_request IS NULL) = (run_requested_at IS NULL));

-- Who changed which setting, and what it was before. By name so it outlives a deleted source.
CREATE TABLE source_config_history (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name text NOT NULL,
    action      text NOT NULL CHECK (action IN ('create', 'update', 'delete', 'enable', 'disable', 'defaults', 'import')),
    actor       text NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now(),
    before      jsonb,
    after       jsonb
);
CREATE INDEX source_config_history_name_idx ON source_config_history (source_name, changed_at DESC);
CREATE INDEX source_config_history_at_idx ON source_config_history (changed_at DESC);
