"""Attach an already-authored independent review equally to either version.

This tool never creates a verdict. The review identifies human versus AI,
the reviewer/model, rationale, independence and the exact raw-evidence hash.
Unreviewed or unexecuted common questions continue to block acceptance.
"""
import argparse
from pathlib import Path

from acceptance.common import DATA, read, sha, write
from acceptance.common_facts import adjudicate, common_score
from acceptance.comparison import compare, comparable_ids
from acceptance.evidence import load_evidence
from acceptance.gate import verify


def attach(report_file, scope, scenario_id, review_file):
    report_file = Path(report_file)
    root, report = report_file.resolve().parent, read(report_file)
    if scenario_id not in comparable_ids(report["baseline"]):
        raise ValueError("Independent common review applies only to questions supported by both versions")
    if scope == "baseline":
        target = report["baseline"]
    elif scope in {"round-1", "round-2", "round-3"}:
        target = report["rounds"][int(scope[-1]) - 1]
    else:
        raise ValueError("Choose a candidate round or baseline")
    entry = next(r for r in target["results"] if r["id"] == scenario_id)
    recording = load_evidence(root, entry)
    corpus = read(DATA / "release.json")
    scenario = next(s for s in corpus["scenarios"] if s["id"] == scenario_id)
    if scope == "baseline":
        if recording.get("status") != "measured": raise ValueError("Unsupported or failed execution cannot be reviewed into success")
        visible = recording.get("visible", "")
    else:
        observed = recording["observed"]
        if not observed.get("completed"): raise ValueError("Incomplete execution cannot be reviewed into success")
        visible = next((d["body"] for d in observed.get("case", {}).get("drafts", []) if d.get("valid")), "")
    if not visible:
        raise ValueError("A missing answer cannot be reviewed into a successful fact comparison")
    computed = common_score(scenario, visible, corpus)
    if computed["status"] != "review_required":
        raise ValueError("A review cannot override a deterministic fact error or an execution failure")
    review = read(review_file)
    correct = adjudicate(review, entry["evidence_sha256"])
    path = root / "reviews" / (sha(review) + ".json")
    write(path, review)
    entry["review"] = {"evidence_file": str(path.relative_to(root)), "evidence_sha256": sha(path.read_bytes())}
    entry["fact_correct" if scope == "baseline" else "common_fact_correct"] = correct
    report["common_comparison"] = compare(report["rounds"], report["baseline"])
    report["baseline"]["common_fact_accuracy"] = report["common_comparison"]["baseline"]["accuracy"]
    if "coverage" in report["baseline"]:
        report["baseline"]["coverage"]["review_required"] = sum(
            r["status"] in {"measured", "measured_failure"} and r.get("fact_correct") is None
            for r in report["baseline"]["results"])
    report["gate"] = verify(report)
    output = root / ("reviewed-" + sha(report) + ".json")
    write(output, report)
    return {"report": str(output), "gate_passed": report["gate"]["passed"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scope", choices=("round-1", "round-2", "round-3", "baseline"), required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    print(attach(args.report, args.scope, args.scenario, args.review))
