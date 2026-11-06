"""Validate actual mixed-workload records and independently rescore each Case."""
import math

from acceptance.oracle import score

RATES = {"weekly_steady": 1.25, "tenfold_burst": 12.5, "fifty_simultaneous": None}


def structure(data):
    errors = []
    if data.get("kind") != "authenticated_mixed_infrastructure_load" or data.get("real_provider") is not False or data.get("authenticated_accounts") != 50:
        errors.append("mixed_authenticated_load_required")
    profiles = data.get("profiles", {})
    if set(profiles) != set(RATES):
        errors.append("three_complete_load_profiles_required")
    for name, rate in RATES.items():
        profile = profiles.get(name, {})
        records = profile.get("records", [])
        duration = data.get("configured_duration_seconds", {}).get(name, 0)
        if rate and (type(duration) not in (int, float) or duration <= 0):
            errors.append(name + ":invalid_duration")
            continue
        expected = 50 if rate is None else math.floor(duration / 60 * rate) + 1
        if profile.get("count") != expected or len(records) != expected or {r.get("index") for r in records if isinstance(r, dict)} != set(range(expected)):
            errors.append(name + ":record_count_mismatch")
        if any(not isinstance(r, dict) for r in records):
            errors.append(name + ":invalid_record")
            continue
        actors = {r.get("actor_id") for r in records}
        if None in actors or (name == "fifty_simultaneous" and len(actors) != 50):
            errors.append(name + ":actor_count_mismatch")
        if {r.get("group") for r in records} != {"orders", "policies", "images"} or any(not r.get("observed") for r in records):
            errors.append(name + ":missing_business_observations")
        starts = [r.get("started_at") for r in records]
        if starts and all(type(t) in (int, float) for t in starts):
            span = max(starts) - min(starts)
            if rate and abs(span - (expected - 1) * 60 / rate) > max(1, duration * .03):
                errors.append(name + ":arrival_schedule_differs")
            if rate is None and span > 5:
                errors.append(name + ":not_simultaneous")
        else:
            errors.append(name + ":arrival_timestamps_missing")
        if rate and profile.get("elapsed_seconds", 0) < duration:
            errors.append(name + ":duration_not_observed")
    return errors


def require_mixed(data, corpus):
    errors = structure(data)
    if errors:
        raise ValueError("Invalid mixed load: " + ", ".join(errors))
    scenarios = {s["id"]: s for s in corpus["scenarios"]}
    allowed = {"development-orders-001", "development-policies-001", "development-images-001"}
    for name, profile in data["profiles"].items():
        for record in profile["records"]:
            sid, observed = record.get("scenario_id"), record["observed"]
            if sid not in allowed or observed.get("id") != sid or record.get("group") != scenarios[sid]["group"] or observed.get("case", {}).get("created_by") != record["actor_id"]:
                raise ValueError("Load observation scenario or employee differs")
            if not score(scenarios[sid], observed, corpus)["fact_correct"] or record.get("passed") is not True:
                raise ValueError("Load consultation did not independently complete its business work")
        queue = profile.get("queue", {})
        samples = queue.get("samples", [])
        if (not samples or queue.get("errors") or samples[-1].get("queued", 0) != 0 or samples[-1].get("running", 0) != 0
                or queue.get("max_queued") != max(s.get("queued", 0) for s in samples)
                or queue.get("max_running") != max(s.get("running", 0) for s in samples)):
            raise ValueError("Load queue measurements are missing or inconsistent")
    return True
