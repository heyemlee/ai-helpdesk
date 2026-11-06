"""Additive identity and ownership state. No default employees or passwords."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_accounts (
 actor_id TEXT PRIMARY KEY, instance TEXT NOT NULL, login TEXT NOT NULL,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('employee','operations','admin')),
 active BOOLEAN NOT NULL DEFAULT true, auth_version BIGINT NOT NULL DEFAULT 1,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(instance,login)
);
CREATE TABLE IF NOT EXISTS staff_sessions (
 token_hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES staff_accounts(actor_id),
 auth_version BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS staff_sessions_actor ON staff_sessions(actor_id);
CREATE TABLE IF NOT EXISTS staff_login_limits (
 bucket TEXT PRIMARY KEY, window_start BIGINT NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_audit (
 seq BIGSERIAL PRIMARY KEY, actor_id TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '',
 action TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS staff_conversations (
 instance TEXT NOT NULL, conv_id TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES staff_accounts(actor_id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(instance,conv_id)
);
CREATE TABLE IF NOT EXISTS staff_sql_runs (
 run_id TEXT PRIMARY KEY, instance TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES staff_accounts(actor_id),
 trace_id TEXT NOT NULL DEFAULT '', history JSONB NOT NULL DEFAULT '{}',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS staff_sql_runs_owner ON staff_sql_runs(instance,actor_id,created_at DESC);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS actor_context JSONB NOT NULL DEFAULT '{}';
"""
