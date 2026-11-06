"""Authenticated whole-service scenario runner. No route/model substitutions."""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from acceptance.common import DATA, ROOT, dataset, read, sha, write
from acceptance.gateway import calibration_ledger, release_ledger
from acceptance.oracle import score, summarize


class Employee:
    def __init__(self, cfg, login):
        self.cfg, self.login_name = cfg, login
        self.client = httpx.Client(base_url=cfg["api"], headers={"Origin": cfg["origin"]}, timeout=200, trust_env=False)

    def request(self, method, path, expected=200, **kwargs):
        result = self.client.request(method, path, **kwargs)
        if result.status_code != expected:
            raise RuntimeError(f"{method} {path}: HTTP {result.status_code}: {result.text[:500]}")
        return result.json()

    def login(self):
        result = self.request("POST", "/auth/login", json={"login": self.login_name, "password": self.cfg["password"]})
        self.client.headers["X-CSRF-Token"] = result["csrf_token"]
        self.actor = result["actor"]
        return self

    def case(self, case_id):
        return self.request("GET", f"/tickets/{case_id}/case")

    def close(self):
        self.client.close()


def prepare(cfg, corpus):
    admin = Employee(cfg, "acceptance-admin").login()
    try:
        known = {row["login"] for row in admin.request("GET", "/admin/employees")["employees"]}
        for login, role in (("acceptance-employee", "employee"), ("acceptance-operations", "operations")):
            if login not in known:
                admin.request("POST", "/admin/employees", expected=201,
                              json={"login": login, "password": cfg["password"], "role": role})
        marker = Path(cfg["root"]) / "knowledge-import.json"
        if marker.exists():
            if read(marker)["corpus_sha256"] != sha(corpus):
                raise ValueError("Cannot reuse a stack across different corpora")
        else:
            docs = [{k: d[k] for k in ("title", "content", "policy", "category")} for d in corpus["policies"]]
            imported = admin.request("POST", "/knowledge/add", json={"documents": docs})
            write(marker, {"corpus_sha256": sha(corpus), "response": imported})
    finally:
        admin.close()


async def effects(cfg, case_ids):
    import asyncpg
    if urlsplit(cfg["postgres_dsn"]).port == 5433 or not Path(cfg["root"]).name.startswith("aihd-stage6-"):
        raise ValueError("Only the owned temporary database may be measured")
    conn = await asyncpg.connect(cfg["postgres_dsn"])
    try:
        async with conn.transaction(readonly=True):
            actions = [dict(r) for r in await conn.fetch("SELECT action_key,case_id,kind FROM case_actions WHERE case_id=ANY($1::text[])", case_ids)]
            notifications = [dict(r) for r in await conn.fetch("SELECT id,ticket_id,attempts,status FROM ticket_notifications WHERE ticket_id=ANY($1::text[])", case_ids)]
        return {"handoff": sum(a["kind"] == "handoff" for a in actions), "notification": len(notifications),
                "actions": actions, "notifications": notifications}
    finally:
        await conn.close()


def execute(employee, scenario, directory, ledger=None, timeout=240):
    observed = {"id": scenario["id"], "completed": False, "runs": [], "step_checks": [], "case_ids": {}, "invariant_violations": []}
    started = time.monotonic()
    ids = observed["case_ids"]
    conv = employee.request("POST", "/auth/conversations", expected=201)["conv_id"]
    remembered = None
    if ledger:
        ledger.register_scenario(scenario["id"])
    def settle(run_id):
        limit = time.monotonic() + timeout
        seen = set()
        while run_id:
            if run_id in seen:
                raise RuntimeError("Cyclic image follow-up Run")
            seen.add(run_id)
            if ledger:
                ledger.bind_run("helpdesk", run_id, scenario["id"])
            while time.monotonic() < limit:
                current = employee.request("GET", f"/tickets/cases/runs/{run_id}")
                if current["status"] in {"succeeded", "failed", "dead", "cancelled"}:
                    observed["runs"].append(current)
                    run_id = (current.get("result") or {}).get("next_run_id")
                    break
                time.sleep(0.25)
            else:
                raise TimeoutError("Scenario Run deadline exceeded")
    try:
        for index, step in enumerate(scenario["steps"]):
            alias = step.get("case_alias", "primary")
            snapshot = employee.case(ids[alias]) if alias in ids else None
            if step["kind"] == "input":
                body = {"message_id": "acc_" + uuid.uuid4().hex, "message": step["message"], "conv_id": conv,
                        "new_case": step.get("new_case", False)}
                for key in ("issue_type", "order_refs", "facts", "policy_scope"):
                    if key in step:
                        body[key] = step[key]
                if snapshot and not body["new_case"]:
                    body.update(case_id=ids[alias], expected_version=snapshot["version"], expected_control_version=snapshot["control_version"])
                if step.get("image"):
                    path = directory / step["image"]
                    with path.open("rb") as stream:
                        # Random external filename: the scenario ID and oracle
                        # are not sent as image metadata to the service.
                        attachment = employee.request("POST", "/attachments", expected=201,
                            files={"file": ("employee-upload.png", stream, "image/png")}, headers={"Idempotency-Key": uuid.uuid4().hex})
                    body["attachment_ids"] = [attachment["id"]]
                receipt = employee.request("POST", "/tickets/cases/inputs", expected=202, json=body)
                ids[alias] = receipt["case_id"]
                settle(receipt["run_id"])
                if step.get("expect"):
                    after = employee.case(ids[alias])
                    expected = step["expect"]
                    observed["step_checks"].append({"step": index, "passed": after["status"] == expected["status"] and sorted(after["pending_fields"]) == sorted(expected["pending"]),
                                                    "status": after["status"], "pending": after["pending_fields"]})
            elif step["kind"] == "action":
                body = {"expected_version": snapshot["version"], "expected_control_version": snapshot["control_version"], "operation": step["operation"]}
                for key in ("status", "reason"):
                    if key in step:
                        body[key] = step[key]
                employee.request("POST", f"/tickets/{ids[alias]}/case/actions", json=body)
            elif step["kind"] == "remember_draft":
                remembered = next((d for d in snapshot["drafts"] if d["valid"]), None)
                if remembered is None:
                    raise RuntimeError("No draft to test stale approval")
            elif step["kind"] == "reject_old_approval":
                if remembered is None:
                    raise RuntimeError("No remembered draft")
                response = employee.client.post(f"/tickets/{ids[alias]}/case/drafts/{remembered['id']}/approve",
                    json={"expected_version": snapshot["version"], "expected_control_version": snapshot["control_version"], "body_hash": remembered["body_hash"]})
                if response.status_code != 409:
                    observed["invariant_violations"].append("stale_draft_approval_not_rejected")
            else:
                raise ValueError("Unimplemented step: " + step["kind"])
        observed["completed"] = True
    except Exception as exc:
        observed["failure"] = {"type": type(exc).__name__, "detail": str(exc)}
    finally:
        if "primary" in ids:
            observed["case"] = employee.case(ids["primary"])
        observed["side_effects"] = asyncio.run(effects(employee.cfg, list(ids.values())))
        observed["elapsed_seconds"] = time.monotonic() - started
    return observed


def development_ledger(stack, cfg, provenance, budget):
    """Bind revalidation to the approval already mounted in this real stack."""
    if (cfg.get("split") != "development" or provenance.get("real_provider") is not True
            or provenance.get("execution") != "immutable_containers" or provenance.get("provider_mode") != "real"
            or Path(cfg["root"]).resolve() != stack.resolve()):
        raise ValueError("Development revalidation requires a real immutable development stack")
    approval = read(budget)
    mounted = read(stack / "provider-state/approval.json")
    # Compare the entire immutable approval, including ID, limits, model,
    # destination and corpus, before opening/creating any persistent ledger.
    if approval != mounted or approval.get("purpose") != "development_revalidation":
        raise ValueError("Development budget differs from the stack approval or purpose")
    return release_ledger(approval)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    real = parser.add_mutually_exclusive_group()
    real.add_argument("--real-calibration", action="store_true")
    real.add_argument("--budget", type=Path, help="Separate persistent budget for development revalidation")
    parser.add_argument("--scenario", action="append")
    args = parser.parse_args(argv)
    cfg = read(args.stack / "private-config.json")
    provenance = read(args.stack / "provenance.json")
    manifest, development, _ = dataset()
    if cfg["split"] != "development" or provenance["real_provider"] != bool(args.real_calibration or args.budget):
        raise ValueError("Development split and explicit matching provider mode are required")
    if cfg["manifest_sha256"] != sha((DATA / "manifest.json").read_bytes()):
        raise ValueError("Stack has a different corpus")
    choices = args.scenario or manifest["calibration_scenarios"]
    if len(set(choices)) != len(choices) or not set(choices) <= {s["id"] for s in development["scenarios"]}:
        raise ValueError("Unique development scenarios are required")
    if args.real_calibration and not set(choices) <= set(manifest["calibration_scenarios"]):
        raise ValueError("Scenario outside approved calibration selection")
    selected = [next(s for s in development["scenarios"] if s["id"] == sid) for sid in choices]
    if args.output.exists():
        raise ValueError("Evidence output already exists; never overwrite a measured run")
    ledger = (development_ledger(args.stack, cfg, provenance, args.budget) if args.budget
              else calibration_ledger() if args.real_calibration else None)
    args.output.mkdir(parents=True, exist_ok=False)
    prepare(cfg, development)
    employee = Employee(cfg, "acceptance-employee").login()
    operator = Employee(cfg, "acceptance-operations").login()
    results = []
    try:
        for scenario in selected:
            if ledger and ledger.report()["remaining_requests"] <= 0:
                break
            if (args.output / (scenario["id"] + ".json")).exists():
                raise ValueError("Evidence output already exists; never overwrite a measured run")
            actor = operator if any(step["kind"] in {"action", "reject_old_approval"} for step in scenario["steps"]) else employee
            observed = execute(actor, scenario, DATA, ledger, timeout=600 if args.budget else 240)
            result = score(scenario, observed, development)
            write(args.output / (scenario["id"] + ".json"), {"observed": observed, "score": result})
            results.append(result)
            report = {"kind": "development_revalidation" if args.budget else "development_calibration" if ledger else "simulated_provider_infrastructure" if provenance.get("provider_mode") == "simulated" else "provider_disabled_integration", "provenance": provenance,
                      "manifest_sha256": cfg["manifest_sha256"], "results": results, "summary": summarize(selected, results),
                      "budget": ledger.report() if ledger else None, "release_eligible": False}
            write(args.output / "report.json", report)
            print(scenario["id"], "correct=" + str(result["fact_correct"]), "errors=" + str(result["errors"]), flush=True)
    finally:
        employee.close()
        operator.close()


if __name__ == "__main__":
    main()
