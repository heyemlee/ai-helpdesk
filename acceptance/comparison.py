"""Derive common capability totals from the same scenario subset on each side."""

def comparable_ids(baseline):
    return {r["id"] for r in baseline["results"] if r["status"] != "unsupported"}


def compare(rounds, baseline):
    ids = comparable_ids(baseline)
    def totals(values):
        return {"total": len(ids), "correct": sum(v is True for v in values), "incorrect": sum(v is False for v in values),
                "pending_or_unmeasured": sum(v is None for v in values),
                "accuracy": sum(v is True for v in values) / len(ids) if ids and all(type(v) is bool for v in values) else None}
    old = {r["id"]: r.get("fact_correct") for r in baseline["results"]}
    result = {"scenario_ids": sorted(ids), "scorer": "common_facts.common_score on both versions", "case_contract_checked": False,
              "baseline": totals([old.get(sid) for sid in ids]), "candidate_rounds": []}
    for measured in rounds:
        new = {r["id"]: r.get("common_fact_correct", r.get("common_score", {}).get("fact_correct")) for r in measured["results"]}
        result["candidate_rounds"].append(totals([new.get(sid) for sid in ids]))
    return result
