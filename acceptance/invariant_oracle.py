"""Recompute fault outcomes from raw observations, not pasted pass counters."""


def evaluate(observations):
    conditions = {}
    def complete_once(value):
        case, run = value["case"], value["run"]
        return [run["status"] == "succeeded", run["result"].get("case_applied") is True,
                len(case["drafts"]) == 1, sum(m["role"] == "assistant" for m in case["messages"]) == 1,
                value["effects"]["notification"] == 0, value["effects"]["handoff"] == 0]
    for name, value in observations.items():
        if name == "employee_identity":
            checks = [v == 401 for v in value["anonymous_statuses"]]
            checks += [value["revoked_session_status"] == 401,
                       value["run"]["result"].get("actor_access_changed") is True and value["run"]["result"].get("case_applied") is False,
                       not value["case"]["drafts"]]
        elif name == "role_and_tenant_isolation":
            checks = [value["admin_status"] == 403, value["policy_status"] == 403, value["tenant_probe"]["status"] == 404]
        elif name == "stale_approval":
            checks = [value["http_status"] == 409,
                      not next(d for d in value["case"]["drafts"] if d["id"] == value["old_draft_id"])["valid"],
                      sum(d["valid"] for d in value["case"]["drafts"]) == 1]
        elif name == "duplicate_callback":
            checks = [value["before"] == value["after"]]
            checks += [r[0] == value["original_result"] and r[1] is True for r in value["probe"]["results"]]
        elif name in {"worker_restart", "redis_loss"}:
            checks = complete_once(value) + [value["run"]["run_id"] == value["submitted_run_id"]]
            if name == "worker_restart": checks += [value["before_container"] != value["after_container"]]
            else: checks += [value["redis_flush_reply"].strip() == "OK"]
        elif name == "cancellation":
            checks = [value["case"]["status"] == "cancelled", not value["case"]["drafts"],
                      value["run"]["result"].get("case_applied") is False and value["run"]["result"].get("case_rejection") in {"case_closed", "case_version_changed", "case_control_changed"},
                      value["effects"]["notification"] == 0]
        elif name == "readonly_sql":
            checks = [value["before"]["success"] is True, value["after"]["success"] is True,
                      value["before"]["rows"] == value["after"]["rows"], value["database_connection_write_denied"] is True]
            checks += [r["success"] is False for r in value["write_results"]]
        elif name == "source_contract":
            checks = [bool(r["verdict"]["usable_as_source"]) == (r["mutation"] == "original") for r in value]
        elif name == "evidence_integrity":
            checks = [bool(r["cells"]) == (r["mutation"] == "original") for r in value]
        else:
            raise ValueError("Unexpected invariant observation")
        conditions[name] = checks
    trials = {k: {"total": len(v), "violations": sum(x is not True for x in v)} for k, v in conditions.items()}
    checks = {k: bool(v) and all(v) for k, v in conditions.items()}
    return {"checks": checks, "trials": trials, "trial_unit": "observed_assertions",
            "fault_scenario_count": len(observations), "passed": bool(checks) and all(checks.values())}
