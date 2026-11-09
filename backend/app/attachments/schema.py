"""Additive attachment storage; no legacy table column/reader changes."""

VERSION = "attachments-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    instance TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    upload_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    preview_sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size BIGINT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('staging','ready','failed','deleting')),
    object_key TEXT NOT NULL,
    preview_key TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(instance, actor_id, upload_key)
);
CREATE INDEX IF NOT EXISTS idx_attachments_cleanup ON attachments(state, updated_at);
CREATE TABLE IF NOT EXISTS attachment_extractions (
    attachment_id TEXT NOT NULL REFERENCES attachments(id),
    sha256 TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(attachment_id,sha256,extractor_version)
);
CREATE TABLE IF NOT EXISTS case_image_reviews (
    case_id TEXT NOT NULL REFERENCES tickets(id),
    attachment_id TEXT NOT NULL REFERENCES attachments(id),
    sha256 TEXT NOT NULL,
    run_id TEXT NOT NULL,
    version BIGINT NOT NULL,
    control_version BIGINT NOT NULL,
    result JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(case_id,attachment_id,run_id)
);
CREATE TABLE IF NOT EXISTS message_attachments (
    message_id TEXT NOT NULL REFERENCES case_messages(message_id),
    attachment_id TEXT NOT NULL REFERENCES attachments(id),
    sha256 TEXT NOT NULL,
    PRIMARY KEY(message_id,attachment_id)
);
-- Keep image pending state safe even if an older application is rolled back.
-- Legacy workers can ask for more information, but cannot silently clear the
-- new server-owned state or accept a same-named client fact as verification.
CREATE OR REPLACE FUNCTION guard_case_image_state() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM jsonb_object_keys(NEW.facts) AS k WHERE k LIKE 'image:%') THEN
        RAISE EXCEPTION 'image facts are server-owned' USING ERRCODE='23514';
    END IF;
    IF current_setting('ai_helpdesk.image_review',true) IS DISTINCT FROM 'on' AND EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(OLD.pending_fields) AS k
        WHERE k LIKE 'image:%' AND NOT (NEW.pending_fields ? k)
    ) THEN
        RAISE EXCEPTION 'image review is required' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS case_image_state_guard ON ticket_case_state;
CREATE TRIGGER case_image_state_guard BEFORE UPDATE OF facts,pending_fields ON ticket_case_state
    FOR EACH ROW EXECUTE FUNCTION guard_case_image_state();
"""


async def migrate(conn):
    if await conn.fetchval("SELECT 1 FROM helpdesk_migrations WHERE version=$1", VERSION):
        return
    await conn.execute(SCHEMA)
    await conn.execute("INSERT INTO helpdesk_migrations VALUES($1)", VERSION)
