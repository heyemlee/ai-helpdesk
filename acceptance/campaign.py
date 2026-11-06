"""Three fresh image-based rounds, comparable baseline, and fail-closed assembly.

Defaults to a provider-disabled offline rehearsal. A smoke limit never changes
the 200-scenario denominator. No report can make a simulated run releaseable.
"""
import argparse
from pathlib import Path
import shutil

from acceptance import baseline
from acceptance.common import DATA, read, sha, write
from acceptance.container_stack import CONFIGURATION, ContainerStack
from acceptance.gate import verify
from acceptance.gateway import release_ledger
from acceptance.oracle import score, summarize
from acceptance.runner import Employee, execute, prepare


class ScopedLedger:
    def __init__(self, ledger, scope):
        self.ledger, self.scope = ledger, scope

    def register_scenario(self, scenario):
        self.ledger.register_scenario(self.scope + ":" + scenario)

    def bind_run(self, service, run, scenario):
        self.ledger.bind_run(service, run, self.scope + ":" + scenario)

    def report(self):
        return self.ledger.report()


def round_(stack, output, number, limit=None, ledger=None):
    directory = output / ("round-" + str(number))
    directory.mkdir(parents=True, exist_ok=False)
    prepare(stack.cfg, stack.corpus)
    employee = Employee(stack.cfg, "acceptance-employee").login()
    operator = Employee(stack.cfg, "acceptance-operations").login()
    results = []
    try:
        for scenario in stack.corpus["scenarios"][:limit]:
            if ledger and ledger.report()["remaining_requests"] <= 0:
                break
            actor = operator if any(step["kind"] in {"action", "reject_old_approval"} for step in scenario["steps"]) else employee
            observed = execute(actor, scenario, DATA, ledger, timeout=600)
            computed = score(scenario, observed, stack.corpus)
            path = directory / (scenario["id"] + ".json")
            write(path, {"id": scenario["id"], **stack.provenance, "observed": observed})
            results.append({**computed, "evidence_file": str(path.relative_to(output)), "evidence_sha256": sha(path.read_bytes())})
            write(directory / "summary.json", {**stack.provenance, "results": results, "summary": summarize(stack.corpus["scenarios"], results)})
            print(f"round {number}: {len(results)}/200 observed; provider={stack.provider}", flush=True)
    finally:
        employee.close()
        operator.close()
    return {**stack.provenance, "results": results, "summary": summarize(stack.corpus["scenarios"], results)}


def assemble(output, artifacts, rounds, comparison=None, ledger=None, components=()):
    report = {"schema_version": 1, "kind": "release_acceptance", "manifest_sha256": sha((DATA / "manifest.json").read_bytes()),
              "artifacts": artifacts, "config_sha256": sha(CONFIGURATION), "rounds": rounds, "baseline": comparison or {},
              "quality_claim": bool(rounds) and all(r["real_provider"] for r in rounds),
              "budget": {"authorized": ledger is not None, "exceeded": False, "recording": ledger.report() if ledger else None}}
    for item in components:
        name, source = item.split("=", 1)
        if name not in {"invariants", "browser", "load", "recovery_local", "recovery_s3", "rollback"}:
            raise ValueError("Unknown component " + name)
        data = read(source)
        if data.get("artifacts") != artifacts or data.get("execution") != "immutable_containers":
            raise ValueError("Source-copy results cannot be relabeled as image acceptance")
        dest = output / "components" / (name + ".json")
        write(dest, data)
        report[name] = {**data, "evidence_file": str(dest.relative_to(output)), "evidence_sha256": sha(dest.read_bytes())}
    report["gate"] = verify(report, artifacts)
    write(output / "report.json", report)
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--baseline-manifest", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--provider", choices=("disabled", "simulated", "real"), default="disabled")
    p.add_argument("--budget", type=Path)
    p.add_argument("--max-scenarios", type=int)
    p.add_argument("--component", action="append", default=[])
    args = p.parse_args()
    if args.max_scenarios is not None and not 1 <= args.max_scenarios <= 200:
        raise ValueError("Smoke coverage must be between 1 and 200")
    if args.output.exists():
        raise ValueError("Never overwrite a previous campaign")
    ledger = release_ledger(read(args.budget)) if args.provider == "real" and args.budget else None
    if args.provider == "real" and ledger is None:
        raise ValueError("Bulk evaluation requires separate explicit budget approval")
    args.output.mkdir(parents=True)
    manifest = read(args.manifest)
    artifacts = {k: manifest[k] for k in ("revision", "images")}
    rounds, comparison = [], None
    try:
        for number in range(1, 4):
            stack = ContainerStack(manifest, provider=args.provider, budget=args.budget)
            try:
                stack.start()
                rounds.append(round_(stack, args.output, number, args.max_scenarios, ScopedLedger(ledger, "round-" + str(number)) if ledger else None))
            finally:
                stack.close()
            assemble(args.output, artifacts, rounds, ledger=ledger)
        if args.baseline_manifest:
            stack = ContainerStack(args.baseline_manifest, provider=args.provider, budget=args.budget, baseline=True)
            try:
                stack.start()
                comparison = baseline.run(stack, args.output / "baseline", limit=args.max_scenarios, ledger=ScopedLedger(ledger, "baseline") if ledger else None)
            finally:
                stack.close()
    finally:
        report = assemble(args.output, artifacts, rounds, comparison, ledger, args.component)
        print("release_gate_passed=" + str(report["gate"]["passed"]), flush=True)


if __name__ == "__main__":
    main()
