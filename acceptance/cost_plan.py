"""Measured usage and a non-authorizing bulk plan; unknown prices stay unknown."""
import argparse
from collections import Counter
from pathlib import Path
import math
import statistics

from acceptance.common import DATA, read, sha, write, dataset


def distribution(values):
    values = sorted(values)
    return {"n": len(values), "min": min(values) if values else None,
            "p50": statistics.median(values) if values else None,
            "p95": values[math.ceil(len(values) * .95) - 1] if values else None,
            "max": max(values) if values else None}


def usage_summary(attempts):
    result = {}
    for kind in ("input_tokens", "output_tokens"):
        known = [(a.get("usage") or {}).get(kind) for a in attempts]
        values = [v for v in known if type(v) is int and v >= 0]
        result[kind] = {"known_sum": sum(values) if values else None,
                        "total": sum(values) if values and len(values) == len(attempts) else None,
                        "unknown_attempts": len(attempts) - len(values), "per_request": distribution(values)}
    return result


def plan(recording):
    attempts = (recording or {}).get("attempts", [])
    calls = Counter(a["scenario"] for a in attempts if a.get("scenario") != "unknown/background")
    measured = list(calls.values())
    _, development, release = dataset()
    groups = {s["id"]: s["group"] for s in development["scenarios"] + release["scenarios"]}
    per_scenario = {sid: {"requests": n, "group": groups.get(sid, "unknown"),
                          "by_service": dict(Counter(a["service"] for a in attempts if a["scenario"] == sid and a.get("service"))),
                          "usage": usage_summary([a for a in attempts if a["scenario"] == sid])} for sid, n in calls.items()}
    per_group = {}
    for group in sorted(set(groups.values()) | {"unknown/background"}):
        selected = [a for a in attempts if groups.get(a["scenario"], "unknown/background") == group]
        group_calls = Counter(a["scenario"] for a in selected)
        per_group[group] = {"scenarios": len(group_calls), "requests": len(selected),
                            "calls_per_scenario": distribution(list(group_calls.values())), "usage": usage_summary(selected)}
    tokens = usage_summary(attempts)
    estimate = math.ceil(max(measured) * 800 * 1.25) if measured else None
    return {"kind": "proposed_release_budget", "authorized": False, "scope": "3 x 200 candidate scenarios + 200 baseline capability records",
            "model": "gpt-6.1-sol", "max_output_tokens": 2048, "corpus_sha256": sha((DATA / "manifest.json").read_bytes()),
            "calibration_actual_http_attempts": len(attempts), "measured_scenarios": len(calls), "usage": tokens,
            "known_usage_tokens": {kind: value["known_sum"] for kind, value in tokens.items()},
            "scenario_measurements": per_scenario, "group_measurements": per_group,
            "unknown_usage_attempts": sum(a.get("usage") is None for a in attempts),
            "observed_calls_per_scenario": dict(calls), "proposed_request_cap_for_review": estimate,
            "max_output_tokens_under_proposed_cap": estimate * 2048 if estimate else None,
            "method": "Largest observed scenario request count x 800 x 1.25; provisional only, not a statistical bound",
            "estimated_cost": None, "currency": None, "supplier_tariff_verified": False,
            "status": "review_required" if measured else "insufficient_calibration",
            "limitation": "Supplier tariff/invoice is unavailable. Unknown usage and unknown cost are never reported as zero; failed calls consume the cap."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write(args.output, plan(read(args.ledger_report) if args.ledger_report else None))
