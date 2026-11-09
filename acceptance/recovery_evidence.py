"""Derive recovery/rollback component claims from full fresh drill recordings."""
from acceptance.configuration import CONFIGURATION
from acceptance.common import sha

FLAGS = ("resource_isolation_verified", "public_ingress_auth_verified", "maintenance_public_port_closed_verified",
         "corrupt_snapshot_fail_closed", "partial_restore_fail_closed_after_pg_commit", "sessions_revoked_and_accounts_closed",
         "rotated_sql_key_fail_closed_through_employee_gateway", "failed_release_rolled_back_to_safe_images", "persisted_release_matches_running")


def component(kind, raw):
    artifacts = raw.get("artifacts", {})
    if (raw.get("execution") != "immutable_containers" or raw.get("source_revision") != artifacts.get("revision")
            or raw.get("tested_image_ids") != artifacts.get("images") or raw.get("config_sha256") != sha(CONFIGURATION)
            or raw.get("acceptance_configuration") != CONFIGURATION):
        raise ValueError("Recovery must be freshly executed with these images and frozen acceptance parameters")
    scale = raw.get("fixture_scale", {})
    business = raw.get("backup", {}).get("business", {})
    case_count = business.get("ticket_case_state", {}).get("count", 0)
    attachment_count = business.get("attachments", {}).get("count", 0)
    audit = raw.get("sql_state_before_backup", {})
    sql_nonempty = bool(audit.get("audit")) and any(audit["audit"].values()) and bool(audit.get("runs")) and any(audit["runs"].values())
    extraction = business.get("attachment_extractions", {}).get("count", 0)
    valid = (all(raw.get(k) is True for k in FLAGS) and raw.get("completed_at") and not raw.get("failed_with")
             and raw.get("sql_state_after_restore") == audit and sql_nonempty
             and extraction > 0 and raw.get("restored_extraction_count") == extraction and scale.get("extraction_count") == extraction
             and case_count >= 200 and scale.get("case_count") == case_count and business.get("tickets", {}).get("count") == case_count
             and attachment_count >= 100 and scale.get("attachment_count") == attachment_count and raw.get("backup", {}).get("objects") == attachment_count
             and business == raw.get("recovery", {}).get("business")
             and raw.get("rpo_observation", {}).get("post_snapshot_writes_observed_lost") == raw.get("rpo_observation", {}).get("post_snapshot_writes_expected_lost") == 1)
    for field in ("initial_release", "reopened_release"):
        result = raw.get(field, {})
        valid = valid and result.get("images") == artifacts.get("images") and result.get("revision") == artifacts.get("revision") and result.get("checks", {}).get("passed") is True
    if kind == "recovery_s3": valid = valid and raw.get("object_backend") == "s3-compatible-local-fixture" and raw.get("actual_s3_target_corruption_fail_closed") is True
    elif kind == "recovery_local": valid = valid and raw.get("object_backend") == "local-private-files"
    elif kind != "rollback": raise ValueError("Unknown recovery component")
    return {"kind": kind, "passed": bool(valid), "execution": "immutable_containers", "artifacts": artifacts,
            "config_sha256": raw["config_sha256"], "source_revision": raw["source_revision"], "real_provider": False,
            "case_count": case_count, "nonempty_sql_audit": bool(sql_nonempty), "nonempty_image_extractions": extraction > 0,
            "rpo_seconds": raw.get("rpo_observation", {}).get("snapshot_to_simulated_loss_seconds"),
            "rto_seconds": raw.get("rto_seconds_including_review_and_http_smoke"), "raw_recording": raw, "quality_claim": False}
