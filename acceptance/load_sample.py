"""Small separately approved real-provider sample beside infrastructure load.

This never launches the 50-user workload against the supplier. Each scenario
and Run consumes the same immutable release ledger as all evaluation/browser
work. Invocation remains prohibited until the user approves that budget.
"""
import argparse
from pathlib import Path

from acceptance.campaign import ScopedLedger
from acceptance.common import DATA, dataset, read, write
from acceptance.gateway import release_ledger
from acceptance.oracle import score
from acceptance.runner import Employee, execute, prepare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    provenance = read(args.stack / "provenance.json")
    cfg = read(args.stack / "private-config.json")
    if provenance.get("execution") != "immutable_containers" or provenance.get("real_provider") is not True or cfg["split"] != "development":
        raise ValueError("A real immutable-image development stack is required")
    if args.output.exists():
        raise ValueError("Never overwrite an earlier load sample")
    ledger = release_ledger(read(args.budget))
    scoped = ScopedLedger(ledger, "load")
    corpus = dataset()[1]
    selected = [next(s for s in corpus["scenarios"] if s["id"] == sid)
                for sid in ("development-orders-001", "development-policies-001", "development-images-001")]
    prepare(cfg, corpus)
    employee = Employee(cfg, "acceptance-employee").login()
    report = {**provenance, "source_revision": provenance["artifacts"]["revision"], "kind": "real_supplier_load_sample",
              "records": [], "scenario_count": 0, "passed": False, "quality_claim": False}
    try:
        for scenario in selected:
            if ledger.report()["remaining_requests"] <= 0:
                break
            observed = execute(employee, scenario, DATA, scoped, timeout=600)
            report["records"].append({"scenario_id": scenario["id"], "observed": observed, "score": score(scenario, observed, corpus)})
            report["scenario_count"] = len(report["records"])
            report["passed"] = len(report["records"]) == len(selected) and all(r["score"]["fact_correct"] for r in report["records"])
            write(args.output, report)
    finally:
        employee.close()
        write(args.output, report)


if __name__ == "__main__":
    main()
