"""Small probes executed inside the actual candidate images, never host mocks."""
import asyncio
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys


async def backend(data):
    from app.storage.pg import Database
    from app.tickets.cases import CaseService
    from app.core.actor import ActorContext
    from fastapi import HTTPException
    db = Database(os.environ["POSTGRES_DSN"])
    await db.connect(256)
    try:
        service = CaseService(db, "staff")
        if data["operation"] == "tenant":
            try:
                await service.get(ActorContext("fixture-other-instance", frozenset({"employee"}), "customer"), data["case_id"])
            except HTTPException as exc:
                return {"status": exc.status_code, "instance": "customer", "target_instance": "staff"}
            return {"status": 200}
        if data["operation"] == "duplicate":
            run = await service.run(data["run_id"])
            results = [await service.complete(run, {"response": "THIS DUPLICATE MUST NEVER REPLACE THE DRAFT"}) for _ in range(2)]
            return {"results": results, "run_id": run.run_id}
    finally:
        await db.close()
    raise ValueError("Unknown database probe")


def evidence(data):
    from app.core.evidence import assess
    from app.core.business_facts import verified_cells
    raw = data["payload"]
    contracts, integrity = [], []
    for kind in ("original", "missing_status", "missing_risk", "wrong_success_type"):
        value = copy.deepcopy(raw)
        if kind == "missing_status": value.pop("execution_status", None)
        if kind == "missing_risk": value.pop("has_risk", None)
        if kind == "wrong_success_type": value["steps"][0]["success"] = "true"
        contracts.append({"mutation": kind, "input": value, "verdict": asdict(assess(value))})
    for kind in ("original", "wrong_value", "missing_cell", "wrong_unit", "stale"):
        value = copy.deepcopy(raw)
        if kind == "wrong_value": value["evidence"][0]["value"] = "forged-value"
        if kind == "missing_cell": value["evidence"].pop()
        if kind == "wrong_unit": value["evidence"][0]["unit"] = "invented-unit"
        if kind == "stale": value["observed_at"] = "2000-01-01T00:00:00Z"
        cells, errors = verified_cells(value)
        integrity.append({"mutation": kind, "input": value, "cells": cells, "errors": errors})
    return {"source_contract": contracts, "evidence_integrity": integrity}


def sql():
    import yaml
    from ai_sql_agent.execution.sqlite_executor import SQLiteMCPExecutor
    from ai_sql_agent.execution.objects import MCPExecutionRequest
    from ai_sql_agent.auth.enforcement import authorization_scope
    from ai_sql_agent.auth.objects import DataPolicy
    config = yaml.safe_load(Path("/semantic/erp_db.yaml").read_text())
    executor = SQLiteMCPExecutor("erp_db", config["connection"]["db_path"], readonly=True)
    count = MCPExecutionRequest("erp_db", "SELECT count(*) AS n FROM orders")
    policy = DataPolicy.from_dict(json.loads(Path(os.environ["AI_SQL_API_KEYS_FILE"]).read_text())["policies"]["readonly"])
    with authorization_scope(policy, caller="acceptance-helpdesk", user_id="ai-helpdesk"):
        before = asdict(executor.execute(count))
        denied = [asdict(executor.execute(MCPExecutionRequest("erp_db", command))) for command in (
            "DELETE FROM orders", "UPDATE orders SET contract_amount=0", "DROP TABLE orders", "SELECT 1; DELETE FROM orders")]
        after = asdict(executor.execute(count))
    connection = executor._connect()
    try:
        try:
            connection.execute("DELETE FROM orders")
            database_denied = False
        except Exception:
            database_denied = True
    finally:
        connection.close()
    return {"before": before, "after": after, "write_results": denied,
            "database_connection_write_denied": database_denied}


if __name__ == "__main__":
    data = json.load(sys.stdin)
    result = sql() if data["operation"] == "sql" else evidence(data) if data["operation"] == "evidence" else asyncio.run(backend(data))
    print(json.dumps(result, ensure_ascii=False, default=str))
