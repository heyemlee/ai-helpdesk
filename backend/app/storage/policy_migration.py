"""Quarantine legacy KB names before enabling policy-aware storage.

Old binaries see empty, non-writable views; old DDL may fail closed. Data is
retained in policy_* tables, with draft metadata until reviewed by an admin.
Never restore the legacy names while scoped policies exist.
"""
TABLES = ("chunks", "documents")


def local_rename(conn):
    for suffix in TABLES:
        old, new = "kb_" + suffix, "policy_" + suffix
        row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (old,)).fetchone()
        if row and row[0] == "table":
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (new,)).fetchone():
                raise RuntimeError("policy_migration_conflicting_tables: restore from a verified backup")
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")


def local_seal(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(policy_documents)")}
    for name, definition in (("policy", "TEXT NOT NULL DEFAULT '{}'"), ("policy_revision", "TEXT NOT NULL DEFAULT 'legacy'")):
        if name not in cols:
            conn.execute(f"ALTER TABLE policy_documents ADD COLUMN {name} {definition}")
    for suffix in TABLES:
        row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", ("kb_" + suffix,)).fetchone()
        if row and row[0] != "view":
            raise RuntimeError("policy_migration_unsafe_legacy_name")
        conn.execute(f"DROP VIEW IF EXISTS kb_{suffix}")
        conn.execute(f"CREATE VIEW kb_{suffix} AS SELECT * FROM policy_{suffix} WHERE 0")


async def pg_rename(conn):
    for suffix in TABLES:
        old, new = "kb_" + suffix, "policy_" + suffix
        kind = await conn.fetchval("SELECT relkind::text FROM pg_class WHERE oid=to_regclass(format('%I.%I',current_schema(),$1::text))", old)
        if kind in ("r", "p"):
            if await conn.fetchval("SELECT to_regclass(format('%I.%I',current_schema(),$1::text))", new):
                raise RuntimeError("policy_migration_conflicting_tables: restore from a verified backup")
            await conn.execute(f"ALTER TABLE {old} RENAME TO {new}")


async def pg_seal(conn):
    # OFFSET 0 makes these views non-updatable, including INSERT/DELETE.
    for suffix in TABLES:
        kind = await conn.fetchval("SELECT relkind::text FROM pg_class WHERE oid=to_regclass(format('%I.%I',current_schema(),$1::text))", "kb_" + suffix)
        if kind and kind != "v":
            raise RuntimeError("policy_migration_unsafe_legacy_name")
        if not kind:
            await conn.execute(f"CREATE VIEW kb_{suffix} AS SELECT * FROM policy_{suffix} WHERE FALSE OFFSET 0")
