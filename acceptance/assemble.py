"""Attach immutable component evidence without rerunning paid evaluations."""
import argparse
from pathlib import Path

from acceptance.common import read, sha, write
from acceptance.gate import require, verify


def assemble(campaign, components, budget=None):
    campaign = Path(campaign)
    source = campaign if campaign.is_file() else campaign / "report.json"
    campaign = source.resolve().parent
    report = read(source)
    for item in components:
        name, source = item.split("=", 1)
        if name not in {"invariants", "browser", "load", "load_real", "recovery_local", "recovery_s3", "rollback"}:
            raise ValueError("Unknown component: " + name)
        data = read(source)
        if data.get("artifacts") != report["artifacts"] or data.get("execution") != "immutable_containers":
            raise ValueError("Component was not executed against these immutable images")
        if data.get("config_sha256") != report["config_sha256"]:
            raise ValueError("Component uses a different acceptance configuration")
        target = campaign / "components" / (name + "-" + sha(data) + ".json")
        if target.exists() and read(target) != data:
            raise ValueError("Evidence path was already used")
        write(target, data)
        report[name] = {**data, "evidence_file": str(target.relative_to(campaign)), "evidence_sha256": sha(target.read_bytes())}
    if budget:
        from acceptance.gateway import release_ledger
        from acceptance.usage_evidence import export_budget
        report["budget"] = export_budget(campaign, release_ledger(read(budget)))
    report["gate"] = verify(report)
    target = campaign / ("assembled-" + sha(report) + ".json")
    write(target, report)
    try:
        require(target)
    except (ValueError, KeyError, TypeError) as exc:
        return {"report": str(target), "passed": False, "reason": str(exc)}
    return {"report": str(target), "passed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--component", action="append", default=[])
    parser.add_argument("--budget", type=Path, help="Export the existing ledger after browser/load samples; sends no requests")
    args = parser.parse_args()
    result = assemble(args.campaign, args.component, args.budget)
    print(__import__("json").dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
