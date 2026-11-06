"""Seed independent synthetic records in an explicitly disposable stack."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

from acceptance.common import read


def business(root, corpus, sql_source, *, container_directory=None):
    sys.path.insert(0, str(sql_source))
    from tools.simple_erp.schema import DATABASES, create_schema
    from tools.simple_erp.semantic import build_semantic
    dbdir, semantic = root / "business", root / "semantic"
    dbdir.mkdir()
    semantic.mkdir()
    for db, (filename, _, _, tables) in DATABASES.items():
        path = dbdir / filename
        with sqlite3.connect(path) as conn:
            create_schema(conn, tables)
            for table in tables:
                rows = corpus["records"][db][table]
                for row in rows:
                    columns = ','.join('"' + k + '"' for k in row)
                    values = ','.join('?' for _ in row)
                    conn.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({values})', list(row.values()))
        path.chmod(0o444)
    build_semantic(dbdir, semantic)
    # The synthetic fixture explicitly uses pieces. The general ERP example
    # leaves these column units unspecified; populate this fixture's dictionary
    # before the real SQL service loads it. Never infer units in returned rows.
    import yaml
    for db, tables in {"erp_db": {"order_item": ["quantity"]},
                       "wms_db": {"material": ["on_hand_qty", "allocated_qty", "safety_stock"], "receipt": ["purchased_qty", "received_qty"]},
                       "svc_db": {"replacement_part": ["quantity"]}}.items():
        path = semantic / (db + ".yaml")
        document = yaml.safe_load(path.read_text())
        for table, columns in tables.items():
            for column in columns:
                document["tables"][table]["columns"][column]["unit"] = "pcs"
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    if container_directory:
        for path in semantic.glob("*.yaml"):
            document = yaml.safe_load(path.read_text())
            if "connection" in document:
                document["connection"]["db_path"] = container_directory + "/" + Path(document["connection"]["db_path"]).name
                path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))


async def main(config):
    root = Path(config["root"]).resolve()
    if root.parent not in {Path("/private/tmp"), Path("/tmp")} or not root.name.startswith("aihd-stage6-"):
        raise ValueError("Only owned disposable stack paths are allowed")
    if urlsplit(config["postgres_dsn"]).port == 5433 or urlsplit(config["redis_url"]).port == 6379:
        raise ValueError("Production ports are forbidden")
    corpus = read(config["corpus"])
    business(root, corpus, root / "source/ai-sql-agent")
    from app.storage.pg import Database
    from app.core.staff_auth import StaffAuth
    database = Database(os.environ["POSTGRES_DSN"])
    await database.connect(embedding_dim=256)
    auth = StaffAuth(database, "staff", origin=config["origin"])
    await auth.create_account("acceptance-admin", config["password"], "admin", bootstrap=True)
    await database.close()
    print(json.dumps({"seeded": True, "orders": len(corpus["records"]["erp_db"]["orders"]), "policies_pending_api": len(corpus["policies"])}))


if __name__ == "__main__":
    asyncio.run(main(read(sys.argv[1])))
