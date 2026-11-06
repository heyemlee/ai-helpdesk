"""Measured authenticated infrastructure load; no model-quality claims."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import math
from pathlib import Path
import statistics
import threading
import time
import uuid

import httpx

from acceptance.common import DATA, dataset, read, write
from acceptance.runner import Employee, execute, prepare

PROFILES = {
    "weekly_steady": {"consultations_per_minute": 3000 / (5 * 8 * 60), "count": 3},
    "tenfold_burst": {"consultations_per_minute": 12.5, "count": 20},
    "fifty_simultaneous": {"consultations_per_minute": None, "count": 50},
}


def prelogin(cfg):
    admin = Employee(cfg, "acceptance-admin").login()
    employees = []
    try:
        known = {e["login"] for e in admin.request("GET", "/admin/employees")["employees"]}
        for i in range(50):
            login = f"acceptance-load-{i:02d}"
            if login not in known:
                admin.request("POST", "/admin/employees", expected=201, json={"login": login, "password": cfg["password"], "role": "employee"})
        # Actual source address and the unchanged application limits: six per
        # account/minute, forty per peer/minute. No forwarded-address spoofing.
        # Leave a full window for prior administration/browser logins too.
        time.sleep(61)
        window = time.monotonic()
        for i in range(50):
            if i == 35:
                time.sleep(max(0, 61 - (time.monotonic() - window)))
            employee = Employee(cfg, f"acceptance-load-{i:02d}").login()
            employees.append(employee)
    finally:
        admin.close()
    return employees


def consultation(employee, profile, index, scenarios=None):
    started = time.monotonic()
    record = {"profile": profile, "index": index, "actor_id": employee.actor["actor_id"], "started_at": time.time(), "passed": False}
    try:
        if scenarios:
            scenario = scenarios[index % len(scenarios)]
            observed = execute(employee, scenario, DATA, timeout=600)
            case = observed.get("case", {})
            evidence = [e for d in case.get("drafts", []) if d.get("valid") for e in d.get("evidence", [])]
            business = [e for e in evidence if e.get("source") == "business_query"]
            rows = sum(len(s.get("rows", [])) for e in business for s in (e.get("evidence_payload") or e).get("steps", []) if s.get("success") is True)
            policies = [e for e in evidence if e.get("source") != "business_query" and e.get("policy_revision")]
            reviews = [f for r in case.get("image_reviews", []) for f in r.get("result", {}).get("fields", [])]
            record.update(scenario_id=scenario["id"], group=scenario["group"], case_ids=observed["case_ids"],
                          runs=[{"run_id": r["run_id"], "status": r["status"], "error": r.get("error"),
                                 "case_rejection": (r.get("result") or {}).get("case_rejection"),
                                 "service_unavailable": (r.get("result") or {}).get("service_unavailable")} for r in observed["runs"]],
                          case_status=case.get("status"), sql_row_count=rows, policy_citation_count=len(policies),
                          image_field_count=len(reviews), image_verified_count=sum(f.get("outcome") == "matched" for f in reviews),
                          step_checks=observed["step_checks"], failure=observed.get("failure"))
            exercised = rows > 0 if scenario["group"] == "orders" else len(policies) > 0 if scenario["group"] == "policies" else len(reviews) == 5 and all(f.get("outcome") == "matched" for f in reviews)
            record["passed"] = observed["completed"] and all(r["status"] == "succeeded" for r in observed["runs"]) and exercised and not observed["invariant_violations"]
            return record
        conv = employee.request("POST", "/auth/conversations", expected=201)["conv_id"]
        body = {"message_id": "load_" + uuid.uuid4().hex, "message": "我要查询订单进展，订单号还没找到。", "issue_type": "order", "new_case": True, "conv_id": conv}
        receipt = employee.request("POST", "/tickets/cases/inputs", expected=202, json=body)
        repeated = employee.request("POST", "/tickets/cases/inputs", expected=202, json=body)
        if receipt["run_id"] != repeated["run_id"] or not repeated.get("deduplicated"):
            raise AssertionError("Identical submission created a second Run")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = employee.request("GET", "/tickets/cases/runs/" + receipt["run_id"])
            if state["status"] in {"succeeded", "failed", "dead", "cancelled"}:
                break
            time.sleep(0.1)
        case = employee.case(receipt["case_id"])
        record.update(run_id=receipt["run_id"], case_id=receipt["case_id"], run_status=state["status"],
                      case_status=case["status"], pending_fields=case["pending_fields"],
                      draft_count=len(case["drafts"]), message_count=len(case["messages"]), runs=len(case["runs"]))
        record["passed"] = state["status"] == "succeeded" and case["status"] == "waiting_for_info" and case["pending_fields"] == ["order_ref"] and len(case["runs"]) == 1 and len(case["messages"]) == 2 and len(case["drafts"]) == 1
    except Exception as exc:
        record["error"] = str(exc)
    finally:
        record["elapsed_seconds"] = time.monotonic() - started
    return record


class QueueObserver:
    def __init__(self, cfg):
        self.cfg, self.samples, self.errors, self.stop = cfg, [], [], threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.observe()), daemon=True)

    async def observe(self):
        import asyncpg
        conn = None
        try:
            conn = await asyncpg.connect(self.cfg["postgres_dsn"])
            while not self.stop.is_set():
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch("SELECT status,count(*) AS count FROM agent_runs GROUP BY status")
                self.samples.append({"at": time.time(), **{r["status"]: r["count"] for r in rows}})
                await asyncio.sleep(.5)
        except Exception as exc:
            self.errors.append(str(exc))
        finally:
            if conn:
                await conn.close()

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        return {"samples": self.samples, "errors": self.errors,
                "max_queued": max((s.get("queued", 0) for s in self.samples), default=0),
                "max_running": max((s.get("running", 0) for s in self.samples), default=0),
                "final_queued": self.samples[-1].get("queued", 0) if self.samples else None,
                "final_running": self.samples[-1].get("running", 0) if self.samples else None}


def measure(employees, name, config, scenarios=None):
    started = time.monotonic()
    rate, count = config["consultations_per_minute"], config["count"]
    records = []
    gateway = "http://127.0.0.1:" + str(employees[0].cfg["gateway_port"])
    before = httpx.get(gateway + "/stats", trust_env=False).json() if scenarios else None
    observer = QueueObserver(employees[0].cfg)
    observer.thread.start()
    last_arrival = started
    with ThreadPoolExecutor(max_workers=50) as workers:
        pending = []
        for i in range(count):
            if rate:
                time.sleep(max(0, started + i * 60 / rate - time.monotonic()))
            last_arrival = time.monotonic()
            pending.append(workers.submit(consultation, employees[i % 50], name, i, scenarios))
        for future in as_completed(pending):
            records.append(future.result())
    durations = sorted(r["elapsed_seconds"] for r in records)
    drained_at = time.monotonic()
    time.sleep(max(0, started + config.get("duration_seconds", 0) - time.monotonic()))
    time.sleep(.6)
    queue = observer.finish()
    failures = Counter()
    for record in records:
        if record["passed"]:
            continue
        reason = record.get("error") or (str(record["failure"]) if record.get("failure") else None)
        reason = reason or next((r.get("case_rejection") or r.get("error") for r in record.get("runs", []) if r.get("case_rejection") or r.get("error")), "chain_not_exercised")
        failures[reason] += 1
    after = httpx.get(gateway + "/stats", trust_env=False).json() if scenarios else None
    groups = {}
    for group in {r.get("group", "queue_smoke") for r in records}:
        values = sorted(r["elapsed_seconds"] for r in records if r.get("group", "queue_smoke") == group)
        groups[group] = {"count": len(values), "p50_seconds": statistics.median(values), "p95_seconds": values[math.ceil(len(values) * .95) - 1]}
    return {"passed": all(r["passed"] for r in records) and len(records) == count and not queue["errors"] and queue["final_queued"] == queue["final_running"] == 0,
            "business_groups": groups, "simulated_provider_before": before, "simulated_provider_after": after,
            "records": sorted(records, key=lambda r: r["index"]), "queue": queue, "failure_distribution": dict(failures),
            "queue_drain_seconds_after_last_arrival": drained_at - last_arrival,
            "authenticated_accounts_available": 50, "active_accounts_used": len({r["actor_id"] for r in records}),
            "configured_arrival_rate_per_minute": rate, "count": count, "elapsed_seconds": time.monotonic() - started,
            "p50_seconds": statistics.median(durations), "p95_seconds": durations[max(0, int(len(durations) * .95 + .999) - 1)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mixed", action="store_true")
    parser.add_argument("--steady-seconds", type=float, default=600)
    parser.add_argument("--burst-seconds", type=float, default=300)
    parser.add_argument("--profile", action="append", choices=tuple(PROFILES))
    args = parser.parse_args()
    cfg = read(args.stack / "private-config.json")
    provenance = read(args.stack / "provenance.json")
    if provenance["real_provider"]:
        raise ValueError("This long infrastructure workload must use the provider-disabled stack")
    if args.mixed and provenance.get("provider_mode") != "simulated":
        raise ValueError("Mixed load requires the explicitly simulated HTTP provider")
    if min(args.steady_seconds, args.burst_seconds) <= 0:
        raise ValueError("Durations must be positive")
    _, development, _ = dataset()
    scenarios = None
    if args.mixed:
        prepare(cfg, development)
        scenarios = [next(s for s in development["scenarios"] if s["id"] == sid)
                     for sid in ("development-orders-001", "development-policies-001", "development-images-001")]
        PROFILES["weekly_steady"]["count"] = math.floor(args.steady_seconds / 60 * 1.25) + 1
        PROFILES["tenfold_burst"]["count"] = math.floor(args.burst_seconds / 60 * 12.5) + 1
        PROFILES["weekly_steady"]["duration_seconds"] = args.steady_seconds
        PROFILES["tenfold_burst"]["duration_seconds"] = args.burst_seconds
    employees = prelogin(cfg)
    report = {"kind": "authenticated_mixed_infrastructure_load" if args.mixed else "authenticated_queue_smoke", "real_provider": False, "infrastructure_only_labeled": True,
              "model_quality_claim": False, "workload": [s["id"] for s in scenarios] if scenarios else ["missing_order_no_model_path"],
              "configured_duration_seconds": {"weekly_steady": args.steady_seconds, "tenfold_burst": args.burst_seconds},
              "authenticated_accounts": 50, "login_rate_limits_unchanged": True, "forwarded_addresses_spoofed": False,
              "weekly_assumption": "3000 consultations / (5 days * 8 hours * 60 minutes) = 1.25/minute",
              "provenance": read(args.stack / "provenance.json"), "profiles": {}, "real_provider_sample": None, "release_eligible": False}
    try:
        for name, config in PROFILES.items():
            if args.profile and name not in args.profile:
                continue
            result = measure(employees, name, config, scenarios)
            report["profiles"][name] = result
            report["passed"] = all(v["passed"] for v in report["profiles"].values()) and len(report["profiles"]) == 3
            write(args.output, report)
            print(name, "passed=" + str(result["passed"]), "count=" + str(result["count"]), "p95=" + str(round(result["p95_seconds"], 3)), flush=True)
    finally:
        for employee in employees:
            employee.close()


if __name__ == "__main__":
    main()
