SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS devpilot_memory;
CREATE TABLE IF NOT EXISTS devpilot_memory.migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS devpilot_memory.profiles (
    user_id text NOT NULL,
    key text NOT NULL,
    value text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'deleted')),
    locked boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 1,
    source_session text NOT NULL,
    source_message text NOT NULL,
    quote text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS devpilot_memory.observations (
    id uuid PRIMARY KEY,
    user_id text NOT NULL,
    key text NOT NULL,
    value text NOT NULL,
    quote text NOT NULL,
    intent text NOT NULL CHECK (intent IN ('stable', 'correction', 'temporary', 'inferred')),
    source_session text NOT NULL,
    source_message text NOT NULL,
    fingerprint text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS observations_pending_idx
    ON devpilot_memory.observations (user_id, created_at, id) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS devpilot_memory.profile_history (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    key text NOT NULL,
    value text NOT NULL,
    version integer NOT NULL,
    action text NOT NULL,
    source_session text NOT NULL,
    source_message text NOT NULL,
    quote text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, key, version)
);
CREATE TABLE IF NOT EXISTS devpilot_memory.short_term (
    user_id text NOT NULL,
    project_id text NOT NULL,
    session_id text NOT NULL,
    key text NOT NULL,
    content text NOT NULL,
    source_message text NOT NULL,
    expires_at timestamptz NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, project_id, session_id, key)
);
CREATE INDEX IF NOT EXISTS short_term_expiry_idx ON devpilot_memory.short_term (expires_at);
"""
