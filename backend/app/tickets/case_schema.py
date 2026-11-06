"""Additive Case migration; old ticket IDs and row shapes remain readable.

Legacy writers can still update untouched tickets. Once adopted into Case work,
legacy status writes fail at the database boundary. New workers use a distinct
agent_runs.instance/Redis stream, so rollback workers cannot claim their work.
"""

VERSION = "case-v1"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS helpdesk_migrations (version TEXT PRIMARY KEY);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS case_context JSONB;
CREATE TABLE IF NOT EXISTS ticket_case_state (
    case_id TEXT PRIMARY KEY REFERENCES tickets(id),
    instance TEXT NOT NULL DEFAULT '',
    managed BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL CHECK (status IN ('open','waiting_for_info','in_progress','resolved','cancelled')),
    issue_type TEXT NOT NULL DEFAULT 'general',
    customer_ref TEXT NOT NULL DEFAULT '',
    order_refs JSONB NOT NULL DEFAULT '[]',
    owner_actor_id TEXT,
    control_mode TEXT NOT NULL DEFAULT 'human' CHECK (control_mode IN ('assistant','human')),
    version BIGINT NOT NULL DEFAULT 1,
    control_version BIGINT NOT NULL DEFAULT 1,
    facts JSONB NOT NULL DEFAULT '{}',
    pending_fields JSONB NOT NULL DEFAULT '[]',
    created_by TEXT,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_scope ON ticket_case_state(instance, status);
CREATE TABLE IF NOT EXISTS case_events (
    id BIGSERIAL PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES tickets(id),
    actor_id TEXT,
    kind TEXT NOT NULL,
    version BIGINT NOT NULL,
    control_version BIGINT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_events ON case_events(case_id,id);
CREATE TABLE IF NOT EXISTS case_messages (
    message_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES tickets(id),
    instance TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    seq BIGINT REFERENCES conversation_log(seq) ON DELETE SET NULL,
    run_id TEXT NOT NULL,
    version BIGINT NOT NULL,
    request_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_messages ON case_messages(case_id,seq);
CREATE TABLE IF NOT EXISTS case_materials (
    case_id TEXT NOT NULL REFERENCES tickets(id),
    attachment_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    message_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    version BIGINT NOT NULL,
    PRIMARY KEY(case_id,attachment_id)
);
CREATE TABLE IF NOT EXISTS case_drafts (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES tickets(id),
    run_id TEXT NOT NULL UNIQUE,
    version BIGINT NOT NULL,
    control_version BIGINT NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    approved_by TEXT,
    invalid_reason TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_drafts ON case_drafts(case_id,created_at);
CREATE TABLE IF NOT EXISTS case_actions (
    action_key TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES tickets(id),
    kind TEXT NOT NULL,
    data JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS case_run_receipts (
    run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES tickets(id),
    applied BOOLEAN NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    result JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_notification_claims (
    delivery_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    lease_until DOUBLE PRECISION NOT NULL
);

CREATE OR REPLACE FUNCTION helpdesk_ticket_case_bridge() RETURNS trigger AS $$
DECLARE c ticket_case_state%ROWTYPE; mapped TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO ticket_case_state(case_id,status,updated_at)
        VALUES(NEW.id, CASE WHEN NEW.status='assigned' THEN 'in_progress' ELSE NEW.status END, NEW.updated_at)
        ON CONFLICT DO NOTHING;
        RETURN NEW;
    END IF;
    SELECT * INTO c FROM ticket_case_state WHERE case_id=NEW.id FOR UPDATE;
    IF c.managed AND current_setting('ai_helpdesk.case_write',true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'case_requires_versioned_api' USING ERRCODE='55000';
    END IF;
    IF NOT c.managed AND NEW.status IS DISTINCT FROM OLD.status THEN
        mapped := CASE WHEN NEW.status='assigned' THEN 'in_progress' ELSE NEW.status END;
        UPDATE ticket_case_state SET status=mapped, version=version+1,
            updated_at=NEW.updated_at WHERE case_id=NEW.id;
        INSERT INTO case_events(case_id,kind,version,control_version,data,created_at)
        SELECT NEW.id,'legacy_status',version,control_version,jsonb_build_object('status',mapped),NEW.updated_at
        FROM ticket_case_state WHERE case_id=NEW.id;
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS helpdesk_case_insert ON tickets;
CREATE TRIGGER helpdesk_case_insert AFTER INSERT ON tickets FOR EACH ROW EXECUTE FUNCTION helpdesk_ticket_case_bridge();
DROP TRIGGER IF EXISTS helpdesk_case_legacy_write ON tickets;
CREATE TRIGGER helpdesk_case_legacy_write BEFORE UPDATE ON tickets FOR EACH ROW EXECUTE FUNCTION helpdesk_ticket_case_bridge();

INSERT INTO ticket_case_state(case_id,status,updated_at)
SELECT id,CASE WHEN status='assigned' THEN 'in_progress' ELSE status END,updated_at FROM tickets
ON CONFLICT DO NOTHING;
"""


async def migrate(conn) -> None:
    # Runs under Database's transaction/advisory lock, including on existing schemas.
    exists = await conn.fetchval("SELECT to_regclass(format('%I.helpdesk_migrations',current_schema()))")
    if exists and await conn.fetchval("SELECT 1 FROM helpdesk_migrations WHERE version=$1", VERSION):
        return
    await conn.execute(SCHEMA)
    await conn.execute("INSERT INTO helpdesk_migrations(version) VALUES($1) ON CONFLICT DO NOTHING", VERSION)
