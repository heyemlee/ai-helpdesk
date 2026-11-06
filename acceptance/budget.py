"""Atomic, persistent reservation of actual outbound HTTP requests.

A reservation is consumed before network I/O and never refunded. A crashed
attempt remains unknown, not free. Opening this ledger again cannot reset it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from acceptance.common import canonical, sha


class Exhausted(RuntimeError):
    pass


class Ledger:
    def __init__(self, path, limits):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY CHECK(id=1), limits TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(
                    id INTEGER PRIMARY KEY, service TEXT, purpose TEXT, scenario TEXT, started REAL,
                    finished REAL, status INTEGER, request_sha256 TEXT, input_chars INTEGER,
                    image_bytes INTEGER, max_output_tokens INTEGER, response_model TEXT,
                    usage TEXT, error TEXT, first_byte_ms REAL);
                CREATE TABLE IF NOT EXISTS scenarios(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS correlations(service TEXT, run_id TEXT, scenario TEXT NOT NULL,
                    PRIMARY KEY(service,run_id), FOREIGN KEY(scenario) REFERENCES scenarios(id));
                CREATE TABLE IF NOT EXISTS request_runs(attempt INTEGER PRIMARY KEY, run_id TEXT,
                    parent_run_id TEXT);
            """)
            encoded = canonical(limits).decode()
            conn.execute("INSERT OR IGNORE INTO budget VALUES(1,?)", (encoded,))
            if conn.execute("SELECT limits FROM budget").fetchone()[0] != encoded:
                raise ValueError("Existing budget limits cannot be changed or reset")
        self.path.chmod(0o600)
        self.limits = limits

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def register_scenario(self, scenario):
        """Admission is persistent and precedes submitting a scenario to the SUT."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seen = {r[0] for r in conn.execute("SELECT id FROM scenarios")}
            if scenario not in seen and len(seen) >= self.limits["scenarios"]:
                raise Exhausted("Cumulative scenario budget exhausted")
            conn.execute("INSERT OR IGNORE INTO scenarios VALUES(?)", (scenario,))

    def bind_run(self, service, run_id, scenario):
        """Bind a returned Run identity; late requests keep their own Run identity.

        Never infer attribution from the last active scenario. SQL requests can
        carry an independently captured Helpdesk parent Run identity. Unbound
        requests remain unknown/background, including after process restart.
        """
        if not run_id:
            raise ValueError("Empty Run cannot be correlated")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM scenarios WHERE id=?", (scenario,)).fetchone():
                raise ValueError("Scenario was not admitted")
            old = conn.execute("SELECT scenario FROM correlations WHERE service=? AND run_id=?", (service, run_id)).fetchone()
            if old and old[0] != scenario:
                raise ValueError("Run correlation cannot be reassigned")
            conn.execute("INSERT OR IGNORE INTO correlations VALUES(?,?,?)", (service, run_id, scenario))

    def reserve(self, service, purpose, body, *, run_id=None, parent_run_id=None, input_chars=0, image_bytes=0):
        maximum = body.get("max_output_tokens", 0)
        if purpose == "responses" and (type(maximum) is not int or not 0 < maximum <= self.limits["max_output_tokens"]):
            raise ValueError("Generation output cap missing or above approved maximum")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
            if count >= self.limits["requests"]:
                raise Exhausted("Cumulative provider HTTP request budget exhausted")
            cursor = conn.execute("INSERT INTO attempts(service,purpose,scenario,started,request_sha256,input_chars,image_bytes,max_output_tokens) VALUES(?,?,?,?,?,?,?,?)",
                                  (service, purpose, "unknown/background", time.time(), sha(body), input_chars, image_bytes, maximum))
            conn.execute("INSERT INTO request_runs VALUES(?,?,?)", (cursor.lastrowid, run_id, parent_run_id))
            return cursor.lastrowid

    def finish(self, attempt, *, status=None, usage=None, model=None, error=None, first_byte_ms=None):
        with self.connect() as conn:
            changed = conn.execute("UPDATE attempts SET finished=?,status=?,usage=?,response_model=?,error=?,first_byte_ms=? WHERE id=? AND finished IS NULL",
                                   (time.time(), status, json.dumps(usage) if usage is not None else None, model, error, first_byte_ms, attempt)).rowcount
            if changed != 1:
                raise ValueError("Attempt missing or already completed")

    def report(self):
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT a.*,r.run_id,r.parent_run_id FROM attempts a LEFT JOIN request_runs r ON r.attempt=a.id ORDER BY a.id")]
            correlations = {(r["service"], r["run_id"]): r["scenario"] for r in conn.execute("SELECT * FROM correlations")}
            scenarios = [r[0] for r in conn.execute("SELECT id FROM scenarios ORDER BY id")]
        for row in rows:
            row["usage"] = json.loads(row["usage"]) if row["usage"] is not None else None
            own = correlations.get((row["service"], row["run_id"]))
            parent = correlations.get(("helpdesk", row["parent_run_id"]))
            if own and parent and own != parent:
                raise ValueError("Conflicting Run evidence")
            row["scenario"] = own or parent or "unknown/background"
        return {"limits": self.limits, "reserved_requests": len(rows), "remaining_requests": self.limits["requests"] - len(rows),
                "admitted_scenarios": scenarios,
                "unknown_usage_requests": sum(r["usage"] is None for r in rows), "attempts": rows,
                "billing": {"actual_invoice": None, "rates": None, "estimated_usd": None,
                            "reason": "No verified supplier tariff or invoice supplied; unknown is not zero"}}
