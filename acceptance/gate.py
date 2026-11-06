"""One fail-closed release decision over the same code/configuration/artifacts."""
from __future__ import annotations

import argparse
from pathlib import Path

from acceptance.common import DATA, GROUPS, BASELINE_REVISION, read, sha
from acceptance.evidence import load_evidence
from acceptance.oracle import score, summarize
from acceptance.common_facts import common_score, adjudicate
from acceptance.comparison import compare
from acceptance.usage_evidence import validate_recording, check_attribution
from acceptance.invariant_oracle import evaluate as evaluate_invariants

INVARIANTS = {"employee_identity", "role_and_tenant_isolation", "stale_approval", "duplicate_callback",
              "worker_restart", "redis_loss", "cancellation", "readonly_sql", "source_contract", "evidence_integrity"}


def verify(report, manifest=None):
    failures = []
    if report.get("schema_version") != 1 or report.get("kind") != "release_acceptance":
        failures.append("not_a_release_acceptance_report")
    if report.get("manifest_sha256") != sha((DATA / "manifest.json").read_bytes()):
        failures.append("corpus_identity_mismatch")
    artifacts = report.get("artifacts", {})
    if not artifacts.get("revision") or set(artifacts.get("images", {})) != {"backend", "frontend", "sql"}:
        failures.append("application_artifacts_missing")
    if manifest and any(artifacts.get(k) != manifest.get(k) for k in ("revision", "images")):
        failures.append("different_application_artifacts")
    rounds = report.get("rounds", [])
    if len(rounds) != 3:
        failures.append("three_rounds_required")
    sessions = set()
    harnesses = set()
    expected_ids = {s["id"] for s in read(DATA / "release.json")["scenarios"]}
    for i, round_ in enumerate(rounds):
        prefix = f"round_{i + 1}:"
        if round_.get("execution") != "immutable_containers" or not round_.get("harness_sha256"):
            failures.append(prefix + "actual_image_execution_not_proven")
        harnesses.add(round_.get("harness_sha256"))
        if not round_.get("real_provider") or not round_.get("fresh_database") or not round_.get("fresh_caches"):
            failures.append(prefix + "real_fresh_run_required")
        if not round_.get("stack_id") or round_["stack_id"] in sessions:
            failures.append(prefix + "stack_reused")
        sessions.add(round_.get("stack_id"))
        if round_.get("artifacts") != artifacts or not report.get("config_sha256") or round_.get("config_sha256") != report.get("config_sha256"):
            failures.append(prefix + "artifact_or_configuration_mismatch")
        results = round_.get("results", [])
        if {r.get("id") for r in results} != expected_ids or len(results) != 200:
            failures.append(prefix + "incomplete_or_duplicate_scenarios")
        if any(type(r.get("fact_correct")) is not bool or not r.get("evidence_sha256") for r in results):
            failures.append(prefix + "unscored_or_unbound_evidence")
        correct = sum(r.get("fact_correct") is True for r in results)
        cross = [r for r in results if r.get("group") == "cross_message"]
        if correct < 190 or len(cross) != 50 or sum(r.get("fact_correct") is True for r in cross) < 48:
            failures.append(prefix + "quality_below_95_percent")
        if any(r.get("invariant_violations") for r in results):
            failures.append(prefix + "invariant_violation")
        if any(sum(r.get("group") == g for r in results) != n for g, n in GROUPS.items()):
            failures.append(prefix + "group_denominator_mismatch")
        images = [r for r in results if r.get("group") == "images"]
        for field in ("order_ref", "model", "quantity", "date", "status"):
            if sum(any(f.get("field") == field for f in r.get("image_fields", [])) for r in images) != 40:
                failures.append(prefix + "image_denominator_missing:" + field)
    if len(harnesses) > 1:
        failures.append("acceptance_harness_changed_between_rounds")
    for name in ("invariants", "browser", "load", "load_real", "recovery_local", "recovery_s3", "rollback"):
        proof = report.get(name, {})
        if proof.get("passed") is not True or proof.get("artifacts") != artifacts or not proof.get("evidence_sha256"):
            failures.append(name + "_missing_failed_or_different_artifacts")
    inv = report.get("invariants", {}).get("checks", {})
    if set(inv) != INVARIANTS or any(value is not True for value in inv.values()):
        failures.append("invariant_coverage_incomplete")
    trials = report.get("invariants", {}).get("trials", {})
    if set(trials) != INVARIANTS or any(type(v.get("total")) is not int or v.get("total", 0) < 1 or v.get("violations") != 0 for v in trials.values()):
        failures.append("invariant_trial_denominators_missing_or_nonzero")
    browser = report.get("browser", {})
    if any(browser.get(k) is not True for k in ("employee_login", "upload", "multiple_turns", "approval", "failure_recovery", "real_provider")):
        failures.append("browser_full_chain_incomplete")
    load = report.get("load", {})
    if load.get("authenticated_accounts") != 50 or set(load.get("profiles", {})) != {"weekly_steady", "tenfold_burst", "fifty_simultaneous"}:
        failures.append("load_model_incomplete")
    real_load = report.get("load_real", {})
    if not load.get("infrastructure_only_labeled") or real_load.get("real_provider") is not True or real_load.get("scenario_count", 0) < 1 or not real_load.get("records"):
        failures.append("load_evidence_not_separated")
    if any(v.get("passed") is not True or not v.get("records") or v.get("queue", {}).get("final_queued") != 0
           or v.get("queue", {}).get("final_running") != 0 for v in load.get("profiles", {}).values()):
        failures.append("load_records_or_queue_drain_missing")
    for name in ("recovery_local", "recovery_s3"):
        proof = report.get(name, {})
        if not proof.get("nonempty_sql_audit") or not proof.get("nonempty_image_extractions") or not proof.get("case_count", 0) >= 200:
            failures.append(name + "_scale_or_content_missing")
        if any(type(proof.get(k)) not in (int, float) for k in ("rpo_seconds", "rto_seconds")):
            failures.append(name + "_rpo_rto_unmeasured")
    baseline = report.get("baseline", {})
    if baseline.get("revision") != "5310da9" or baseline.get("model") != "gpt-6.1-sol" or baseline.get("manifest_sha256") != report.get("manifest_sha256"):
        failures.append("comparable_baseline_missing")
    if baseline.get("provenance", {}).get("real_provider") is not True or baseline.get("provenance", {}).get("execution") != "immutable_containers":
        failures.append("baseline_real_image_execution_missing")
    baseline_artifacts = baseline.get("provenance", {}).get("artifacts", {})
    if baseline_artifacts.get("revision") != BASELINE_REVISION or set(baseline_artifacts.get("images", {})) != {"backend", "frontend", "sql"}:
        failures.append("baseline_image_identity_missing")
    if baseline.get("config_sha256") != report.get("config_sha256") or len(baseline.get("results", [])) != 200:
        failures.append("baseline_parameters_or_coverage_missing")
    if any(r.get("status") not in {"measured", "measured_failure", "unsupported"} or (r.get("status") == "unsupported" and not r.get("reason")) for r in baseline.get("results", [])):
        failures.append("baseline_unsupported_not_explicit")
    budget = report.get("budget", {})
    if budget.get("authorized") is not True or budget.get("exceeded") is not False:
        failures.append("release_budget_not_authorized_or_exceeded")
    if not budget.get("evidence_file") or not budget.get("evidence_sha256") or not budget.get("limits_sha256") or budget.get("reserved_requests", 0) < 1:
        failures.append("persistent_supplier_usage_evidence_missing")
    if baseline.get("results") and report.get("common_comparison") != compare(rounds, baseline):
        failures.append("common_comparison_totals_mismatch")
    return {"passed": not failures, "failures": sorted(set(failures))}


def require(path, manifest=None):
    report = read(path)
    outcome = verify(report, manifest)
    if not outcome["passed"]:
        raise ValueError("Unified release acceptance failed: " + ', '.join(outcome["failures"]))
    # A claimed hash/score is not proof. Open every referenced raw recording,
    # verify its bytes and provenance, then recompute all per-scenario results.
    directory = Path(path).resolve().parent
    corpus = read(DATA / "release.json")
    scenarios = {s["id"]: s for s in corpus["scenarios"]}
    artifacts, configuration = report["artifacts"], report["config_sha256"]
    usage = load_evidence(directory, report["budget"])
    validate_recording(usage, report["manifest_sha256"])
    if report["budget"]["limits_sha256"] != sha(usage["limits"]) or report["budget"]["reserved_requests"] != usage["reserved_requests"]:
        raise ValueError("Budget summary differs from persistent ledger export")
    contexts = {}
    for number, round_ in enumerate(report["rounds"], 1):
        checked = []
        for claimed in round_["results"]:
            data = load_evidence(directory, claimed, artifacts=artifacts, config_sha256=configuration)
            if data.get("manifest_sha256") != report["manifest_sha256"] or data.get("stack_id") != round_["stack_id"] or data.get("real_provider") is not True:
                raise ValueError("Per-scenario recording has a different corpus/stack/provider")
            if data.get("id") != claimed["id"] or data.get("observed", {}).get("id") != claimed["id"]:
                raise ValueError("Per-scenario recording identity mismatch")
            computed = score(scenarios[claimed["id"]], data["observed"], corpus)
            contexts[f"round-{number}:" + claimed["id"]] = {r["run_id"] for r in data["observed"].get("runs", [])}
            for key, value in computed.items():
                if claimed.get(key) != value:
                    raise ValueError("Declared score differs from independent evidence: " + claimed["id"] + "/" + key)
            checked.append(computed)
            body = next((d["body"] for d in data["observed"].get("case", {}).get("drafts", []) if d.get("valid")), "")
            common = common_score(scenarios[claimed["id"]], body, corpus) if body and data["observed"]["completed"] else {"status": "execution_failed", "fact_correct": False, "contract_checked": False}
            if claimed.get("common_score") != common:
                raise ValueError("Candidate common fact score was altered or omitted")
        if round_.get("summary") != summarize(corpus["scenarios"], checked):
            raise ValueError("Round totals differ from recomputed evidence")
    for name in ("invariants", "browser", "load", "load_real", "recovery_local", "recovery_s3", "rollback"):
        proof = report[name]
        data = load_evidence(directory, proof, artifacts=artifacts, config_sha256=configuration)
        # All component claims used by verify() must come from the bound raw
        # result, not an optimistic summary pasted onto an old recording.
        for key, value in proof.items():
            if key not in {"evidence_file", "evidence_sha256"} and data.get(key) != value:
                raise ValueError("Component summary differs from raw evidence: " + name + "/" + key)
        if data.get("source_revision") != artifacts["revision"]:
            raise ValueError("Component source revision differs from images")
        if name == "invariants":
            computed = evaluate_invariants(data.get("observations", {}))
            if any(data.get(k) != computed[k] for k in ("passed", "checks", "trials")):
                raise ValueError("Invariant counters differ from raw fault observations")
        elif name == "browser":
            contexts["browser:development-images-001"] = {r["run_id"] for r in data.get("withImage", {}).get("runs", [])}
        elif name == "load_real":
            records = data.get("records", [])
            if data.get("scenario_count") != len(records) or not records or any(not r.get("observed", {}).get("completed") for r in records):
                raise ValueError("Real load sample has no complete recorded consultations")
            for record in records:
                contexts["load:" + record["scenario_id"]] = {r["run_id"] for r in record["observed"].get("runs", [])}
                development = read(DATA / "development.json")
                scenario = next((s for s in development["scenarios"] if s["id"] == record["scenario_id"]), None)
                if scenario is None or record.get("score") != score(scenario, record["observed"], development) or record["score"]["fact_correct"] is not True:
                    raise ValueError("Real load score differs from independently checked fixed development data")
    baseline = report["baseline"]
    seen = set()
    for result in baseline["results"]:
        if result["id"] not in scenarios or result["id"] in seen:
            raise ValueError("Baseline scenario coverage mismatch")
        seen.add(result["id"])
        data = load_evidence(directory, result, artifacts=baseline["provenance"]["artifacts"], config_sha256=configuration)
        if data.get("source_revision") != "5310da9" or data.get("manifest_sha256") != report["manifest_sha256"]:
            raise ValueError("Baseline evidence belongs to another source/corpus")
        contexts["baseline:" + result["id"]] = set(data.get("run_ids", []))
        unsupported = scenarios[result["id"]]["group"] == "images" or (scenarios[result["id"]]["group"] == "cross_message" and scenarios[result["id"]]["subtype"] not in {"supplement", "reference"})
        if (result["status"] == "unsupported") != unsupported:
            raise ValueError("Baseline common capability was incorrectly excluded")
        if result["status"] == "unsupported":
            if data.get("status") != "unsupported" or not data.get("capability_evidence") or result.get("fact_correct") is not None:
                raise ValueError("Unsupported baseline capability not documented")
        elif result["status"] == "measured_failure":
            if data.get("status") != "measured_failure" or not data.get("reason") or result.get("fact_correct") is not False or data.get("real_provider") is not True:
                raise ValueError("Baseline execution failure cannot be dropped from denominator")
        else:
            if data.get("real_provider") is not True:
                raise ValueError("Measured baseline used a disabled or simulated provider")
            computed = common_score(scenarios[result["id"]], data.get("visible", ""), corpus)
            correct = computed["fact_correct"]
            if computed["status"] == "review_required":
                review = load_evidence(directory, result.get("review", {}))
                correct = adjudicate(review, result["evidence_sha256"])
            if result.get("fact_correct") != correct:
                raise ValueError("Baseline fact score differs from common adjudicator")
    # Every score feeding this comparison has now been recomputed above.
    if report.get("common_comparison") != compare(report["rounds"], baseline):
        raise ValueError("Common comparison differs from recomputed per-question evidence")
    counts = check_attribution(usage, contexts, {"round-1", "round-2", "round-3", "baseline", "browser", "load"})
    load_scopes = {"load:" + r["scenario_id"] for r in report["load_real"]["records"]}
    if any(counts.get(scope, 0) < 1 for scope in load_scopes):
        raise ValueError("A real load consultation has no successful supplier request")
    return {"sha256": sha(report), "artifacts": report["artifacts"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--manifest")
    args = parser.parse_args()
    print(require(args.report, read(args.manifest) if args.manifest else None))
