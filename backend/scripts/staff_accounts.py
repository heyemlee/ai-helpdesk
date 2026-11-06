"""Deployment bootstrap. Explicit DSN from environment; passwords from a terminal.

No default accounts, no command-line passwords, no overwrite of an existing login.
Run only against the deployment being provisioned. Tests use their own fixtures.
"""
import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.staff_auth import StaffAuth  # noqa: E402
from app.storage.pg import Database  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("login")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--role", choices=["employee", "operations", "admin"], required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Bootstrap requires an interactive terminal; do not pipe credentials")
    password = getpass.getpass("New employee password: ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match")
    db = Database(os.environ["POSTGRES_DSN"])
    await db.connect_pool()
    try:
        account = await StaffAuth(db, args.instance).create_account(args.login, password, args.role, bootstrap=True)
        print("Created employee " + account["actor_id"])
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
