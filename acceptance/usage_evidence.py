"""Validate the persistent request export, including real HTTP receipts and Runs.

Reservations with unknown usage still consume the budget. A successful HTTP
receipt is required to prove a real sample; simulated or uncompleted requests
cannot turn a report's real_provider boolean into evidence.
"""
import re
from urllib.parse import urlsplit

from acceptance.common import sha, write


def export_budget(directory, ledger):
    recording = ledger.report()
    path = directory / "usage" / (sha(recording) + ".json")
    write(path, recording)
    return {"authorized": recording["limits"].get("confirmed") is True,
            "exceeded": recording["reserved_requests"] > recording["limits"]["requests"],
            "reserved_requests": recording["reserved_requests"], "limits_sha256": sha(recording["limits"]),
            "evidence_file": str(path.relative_to(directory)), "evidence_sha256": sha(path.read_bytes())}


def validate_recording(data, corpus):
    limits = data.get("limits", {})
    if (data.get("kind") != "persistent_provider_ledger" or data.get("format") != 1
            or limits.get("scope") != "release_and_baseline" or limits.get("confirmed") is not True
            or not limits.get("user_approval_reference") or not re.fullmatch(r"[a-z0-9-]{8,64}", limits.get("id", ""))
            or limits.get("corpus_sha256") != corpus or limits.get("model") != "gpt-6.1-sol"
            or limits.get("max_output_tokens") != 2048 or urlsplit(limits.get("supplier_base_url", "")).scheme != "https"
            or any(type(limits.get(k)) is not int or limits[k] < 1 for k in ("requests", "scenarios"))):
        raise ValueError("Ledger lacks the distinct approved destination/model/corpus/limits")
    attempts, admitted = data.get("attempts", []), data.get("admitted_scenarios", [])
    if (not attempts or len(attempts) > limits["requests"] or data.get("reserved_requests") != len(attempts)
            or data.get("remaining_requests") != limits["requests"] - len(attempts)
            or len(set(admitted)) != len(admitted) or len(admitted) > limits["scenarios"]
            or [r.get("id") for r in attempts] != list(range(1, len(attempts) + 1))):
        raise ValueError("Ledger request/scenario totals are missing, altered, or over budget")
    links = {}
    for link in data.get("correlations", []):
        identity = (link.get("service"), link.get("run_id"))
        if not identity[1] or identity in links or link.get("scenario") not in admitted:
            raise ValueError("Invalid ledger Run correlation")
        links[identity] = link["scenario"]
    positives = set()
    for row in attempts:
        if (row.get("service") not in {"helpdesk", "sql"} or row.get("purpose") != "responses"
                or type(row.get("max_output_tokens")) is not int or not 0 < row["max_output_tokens"] <= limits["max_output_tokens"]
                or not re.fullmatch(r"[0-9a-f]{64}", row.get("request_sha256", ""))):
            raise ValueError("Malformed or unbounded provider request")
        own, parent = links.get((row["service"], row.get("run_id"))), links.get(("helpdesk", row.get("parent_run_id")))
        if (own and parent and own != parent) or row.get("scenario") != (own or parent or "unknown/background"):
            raise ValueError("Ledger scenario attribution differs from immutable Run links")
        usage = row.get("usage")
        if usage is not None and (not isinstance(usage, dict) or any(type(usage[k]) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens") if k in usage)):
            raise ValueError("Malformed supplier usage")
        receipt = row.get("receipt")
        if receipt:
            if (receipt.get("transport") != "supplier_http" or receipt.get("supplier_base_url", "").rstrip("/") != limits["supplier_base_url"].rstrip("/")
                    or receipt.get("endpoint") != "responses" or receipt.get("requested_model") != limits["model"]):
                raise ValueError("Simulated or different supplier receipt cannot prove real usage")
            if row.get("finished") is not None and row.get("error") is None and type(row.get("status")) is int and 200 <= row["status"] < 300:
                if (not re.fullmatch(r"[0-9a-f]{64}", receipt.get("response_sha256") or "")
                        or not receipt.get("response_id") or row.get("response_model") != limits["model"]):
                    raise ValueError("Successful supplier response lacks a bound receipt/model")
                positives.add(row["id"])
    if data.get("unknown_usage_requests") != sum(r.get("usage") is None for r in attempts):
        raise ValueError("Unknown usage was altered or treated as zero")
    if not positives:
        raise ValueError("No completed real supplier request")
    return positives


def check_attribution(data, contexts, required_scopes):
    """Contexts come from independently opened scenario/Run recordings."""
    positives = validate_recording(data, data["limits"]["corpus_sha256"])
    counts = {}
    for row in data["attempts"]:
        scope = row["scenario"]
        if scope == "unknown/background":
            continue  # Still included in the global cap and unknown-use totals.
        roots = contexts.get(scope)
        root_run = row.get("run_id") if row["service"] == "helpdesk" else row.get("parent_run_id")
        if roots is None or root_run not in roots:
            raise ValueError("Provider request is not bound to the recorded scenario Run: " + scope)
        if row["id"] in positives:
            counts[scope] = counts.get(scope, 0) + 1
    for prefix in required_scopes:
        if not any(n > 0 and (scope == prefix or scope.startswith(prefix + ":")) for scope, n in counts.items()):
            raise ValueError("Real supplier evidence absent for " + prefix)
    return counts
