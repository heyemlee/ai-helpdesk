"""Owned-container fault campaign with raw API, state and image-level probes.

Uses only the local simulated supplier. Stops/recreates only its own Worker
and flushes only its own disposable Redis. It never imports historical test
pass counters or creates production/quality approval.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import time
import uuid

import httpx

from acceptance.common import DATA, write
from acceptance.container_stack import ContainerStack
from acceptance.gate import INVARIANTS
from acceptance.invariant_oracle import evaluate
from acceptance.runner import Employee, execute, effects, prepare


def settle(actor, run_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = actor.request("GET", "/tickets/cases/runs/" + run_id)
        if run["status"] in {"succeeded", "failed", "dead", "cancelled"}:
            return run
        time.sleep(.2)
    raise TimeoutError("Fault campaign Run did not settle: " + run_id)


def submit(actor, *, full=False):
    conv = actor.request("POST", "/auth/conversations", expected=201)["conv_id"]
    body = {"message_id": uuid.uuid4().hex, "message": "订单号还没有，请保留问题。", "new_case": True, "issue_type": "order", "conv_id": conv}
    if full:
        body.update(message="请查询 SO-2026-11000 的合同金额和订单状态。", order_refs=["SO-2026-11000"])
    return actor.request("POST", "/tickets/cases/inputs", expected=202, json=body)


def probe(stack, service, data):
    completed = subprocess.run([*stack.compose, "exec", "-T", service, "python", "-m", "acceptance.fault_probe"],
                               input=json.dumps(data), text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError("Image probe failed: " + completed.stderr[-2000:])
    return json.loads(completed.stdout.splitlines()[-1])


def completion(actor, receipt):
    run = settle(actor, receipt["run_id"])
    return {"run": run, "case": actor.case(receipt["case_id"]), "submitted_run_id": receipt["run_id"],
            "effects": asyncio.run(effects(actor.cfg, [receipt["case_id"]]))}


def exercise(stack, report, output):
    prepare(stack.cfg, stack.corpus)
    admin, employee = Employee(stack.cfg, "acceptance-admin").login(), Employee(stack.cfg, "acceptance-employee").login()
    observations = report["observations"]
    def save():
        result = evaluate(observations)
        result["passed"] = result["passed"] and set(observations) == INVARIANTS
        report.update(result)
        write(output, report)
    try:
        anonymous = []
        with httpx.Client(base_url=stack.cfg["api"], trust_env=False) as client:
            for headers in ({}, {"X-Actor-Id": "admin", "X-Actor-Roles": "admin", "X-API-Key": stack.token}):
                anonymous.append(client.get("/tickets/cases", headers=headers).status_code)
        who = "fault-" + uuid.uuid4().hex[:12]
        account = admin.request("POST", "/admin/employees", expected=201, json={"login": who, "password": stack.password, "role": "employee"})
        revoked = Employee(stack.cfg, who).login()
        stack.run("stop", "--timeout", "15", "worker")
        receipt = submit(revoked)
        admin.request("PATCH", "/admin/employees/" + revoked.actor["actor_id"], json={"active": False})
        session_status = revoked.client.get("/auth/session").status_code
        revoked.close()
        stack.run("up", "-d", "--wait", "worker")
        observations["employee_identity"] = {**completion(admin, receipt), "anonymous_statuses": anonymous, "revoked_session_status": session_status,
                                              "account_id": account["actor_id"]}
        save()

        policy = employee.client.post("/tickets/cases/inputs", json={"message_id": uuid.uuid4().hex, "new_case": True, "conv_id": employee.request("POST", "/auth/conversations", expected=201)["conv_id"],
            "message": "查看管理政策", "policy_scope": {"audience": "admin"}})
        observations["role_and_tenant_isolation"] = {"admin_status": employee.client.get("/admin/employees").status_code,
            "policy_status": policy.status_code, "tenant_probe": probe(stack, "api", {"operation": "tenant", "case_id": receipt["case_id"]})}
        save()

        first = submit(admin)
        original = completion(admin, first)
        case = original["case"]
        old = case["drafts"][0]
        next_receipt = admin.request("POST", "/tickets/cases/inputs", expected=202, json={"message_id": uuid.uuid4().hex, "message": "补充材料已收到，单号仍缺失。",
            "conv_id": case["conv_id"], "case_id": case["case_id"], "expected_version": case["version"], "expected_control_version": case["control_version"]})
        second = completion(admin, next_receipt)
        case = second["case"]
        rejected = admin.client.post(f"/tickets/{case['case_id']}/case/drafts/{old['id']}/approve", json={"expected_version": case["version"], "expected_control_version": case["control_version"], "body_hash": old["body_hash"]})
        observations["stale_approval"] = {"old_draft_id": old["id"], "http_status": rejected.status_code, "response": rejected.json(), "case": admin.case(case["case_id"])}
        save()

        before = admin.case(case["case_id"])
        duplicate = probe(stack, "api", {"operation": "duplicate", "run_id": second["run"]["run_id"]})
        observations["duplicate_callback"] = {"before": before, "after": admin.case(case["case_id"]), "original_result": second["run"]["result"], "probe": duplicate}
        save()

        running = submit(admin, full=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = admin.request("GET", "/tickets/cases/runs/" + running["run_id"])
            if status["status"] == "running": break
            time.sleep(.1)
        if status["status"] != "running": raise RuntimeError("No running work was available for restart")
        before_container = stack.run("ps", "-q", "worker").strip()
        stats_before = httpx.get(f"http://127.0.0.1:{stack.ports['gateway']}/stats", trust_env=False).json()
        stack.run("up", "-d", "--no-deps", "--force-recreate", "--wait", "worker")
        observations["worker_restart"] = {**completion(admin, running), "before_run": status,
            "before_container": before_container, "after_container": stack.run("ps", "-q", "worker").strip(),
            "provider_before": stats_before, "provider_after": httpx.get(f"http://127.0.0.1:{stack.ports['gateway']}/stats", trust_env=False).json()}
        save()

        stack.run("stop", "--timeout", "15", "worker")
        pending = submit(admin)
        flushed = stack.run("exec", "-T", "redis", "redis-cli", "FLUSHDB")
        stack.run("up", "-d", "--wait", "worker")
        observations["redis_loss"] = {**completion(admin, pending), "redis_flush_reply": flushed}
        save()

        stack.run("stop", "--timeout", "15", "worker")
        pending = submit(admin)
        case = admin.case(pending["case_id"])
        admin.request("POST", f"/tickets/{case['case_id']}/case/actions", json={"expected_version": case["version"], "expected_control_version": case["control_version"],
                       "operation": "status", "status": "cancelled", "reason": "Isolated cancellation fault probe"})
        stack.run("up", "-d", "--wait", "worker")
        observations["cancellation"] = completion(admin, pending)
        save()
        observations["readonly_sql"] = probe(stack, "sql", {"operation": "sql"})
        save()

        scenario = next(s for s in stack.corpus["scenarios"] if s["id"] == "development-orders-001")
        actual = execute(admin, scenario, DATA, timeout=180)
        payload = next(e["evidence_payload"] for d in actual["case"]["drafts"] if d["valid"] for e in d["evidence"] if e.get("source") == "business_query")
        mutations = probe(stack, "api", {"operation": "evidence", "payload": payload})
        report["evidence_origin"] = actual
        observations.update(mutations)
        save()
    finally:
        admin.close()
        employee.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise ValueError("Never overwrite a fault recording")
    stack = ContainerStack(args.manifest, split="development", provider="simulated", delay=.8)
    report = {"kind": "immutable_image_fault_campaign", "passed": False, "observations": {}, "quality_claim": False}
    try:
        stack.start()
        report.update(stack.provenance, source_revision=stack.manifest["revision"])
        exercise(stack, report, args.output)
    except Exception as exc:
        report.update(passed=False, failure={"type": type(exc).__name__, "detail": str(exc)})
        raise
    finally:
        write(args.output, report)
        stack.close()


if __name__ == "__main__":
    main()
