"""Measured usage and a non-authorizing bulk plan; unknown prices stay unknown."""
import argparse
from collections import Counter
from pathlib import Path
import math

from acceptance.common import DATA, read, sha, write


def plan(recording):
    attempts = (recording or {}).get("attempts", [])
    calls = Counter(a["scenario"] for a in attempts if a.get("scenario") != "unknown/background")
    measured = list(calls.values())
    tokens = {kind: sum((a.get("usage") or {}).get(kind, 0) for a in attempts) for kind in ("input_tokens", "output_tokens")}
    estimate = math.ceil(max(measured) * 800 * 1.25) if measured else None
    return {"kind": "proposed_release_budget", "authorized": False, "scope": "3 x 200 candidate scenarios + 200 baseline capability records",
            "model": "gpt-6.1-sol", "max_output_tokens": 2048, "corpus_sha256": sha((DATA / "manifest.json").read_bytes()),
            "calibration_actual_http_attempts": len(attempts), "measured_scenarios": len(calls), "known_usage_tokens": tokens,
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
