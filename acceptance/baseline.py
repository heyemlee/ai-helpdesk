"""Pre-Case baseline over the same authored inputs and model parameters."""
import time
import uuid

import httpx

from acceptance.common import sha, write
from acceptance.common_facts import common_score


def run(stack, directory, *, limit=None, ledger=None):
    directory.mkdir(parents=True, exist_ok=False)
    cfg, corpus = stack.cfg, stack.corpus
    result = {"revision": "5310da9", "model": "gpt-6.1-sol", "manifest_sha256": cfg["manifest_sha256"],
              "config_sha256": stack.provenance["config_sha256"], "provenance": stack.provenance,
              "common_fact_accuracy": None, "results": [], "quality_claim": stack.provider == "real"}
    with httpx.Client(base_url=cfg["api"], headers={"X-API-Key": cfg["gateway_token"]}, timeout=300, trust_env=False) as client:
        spec = client.get("/openapi.json").json()
        capabilities = {"openapi_sha256": sha(spec), "paths": sorted(spec["paths"]),
                        "chat_fields": sorted(spec["components"]["schemas"]["ChatRequest"]["properties"])}
        docs = [{k: d[k] for k in ("title", "content", "category")} for d in corpus["policies"]]
        response = client.post("/knowledge/add", json={"documents": docs})
        response.raise_for_status()
        measured = 0
        for scenario in corpus["scenarios"]:
            sid = scenario["id"]
            recording = {"id": sid, "source_revision": "5310da9", "artifacts": stack.provenance["artifacts"],
                         "config_sha256": result["config_sha256"], "manifest_sha256": result["manifest_sha256"],
                         "real_provider": stack.provider == "real", "capability_evidence": capabilities}
            if scenario["group"] in {"cross_message", "images"}:
                recording.update(status="unsupported", reason="The baseline API has no explicit Case/version/approval contract" if scenario["group"] == "cross_message" else "The baseline ChatRequest has no attachment/image field or upload workflow", fact_correct=None)
            elif limit is not None and measured >= limit:
                recording.update(status="unmeasured", reason="Explicit offline smoke limit", fact_correct=None)
            elif ledger and ledger.report()["remaining_requests"] <= 0:
                recording.update(status="unmeasured", reason="Authorized request budget exhausted", fact_correct=None)
            else:
                measured += 1
                if ledger:
                    ledger.register_scenario(sid)
                started = time.monotonic()
                replies = []
                try:
                    for step in scenario["steps"]:
                        prompt = step["message"]
                        if step.get("policy_scope"):
                            prompt += "\n适用条件：" + __import__("json").dumps(step["policy_scope"], ensure_ascii=False)
                        response = client.post("/chat", json={"message": prompt, "user_id": "baseline-employee", "conv_id": "baseline-" + uuid.uuid4().hex}, headers={"Idempotency-Key": uuid.uuid4().hex})
                        response.raise_for_status()
                        body = response.json()
                        run_id = body.get("run_id") or (body.get("run") or {}).get("run_id")
                        if ledger and run_id:
                            ledger.bind_run("helpdesk", run_id, sid)
                        deadline = time.monotonic() + 300
                        while response.status_code == 202 and time.monotonic() < deadline:
                            time.sleep(.25)
                            current = client.get("/runs/" + run_id)
                            current.raise_for_status()
                            state = current.json()
                            if state["status"] in {"succeeded", "failed", "dead", "cancelled"}:
                                body = state.get("result") or {}
                                break
                        replies.append(body)
                    visible = replies[-1].get("response", "") if replies else ""
                    adjudication = common_score(scenario, visible, corpus)
                    recording.update(status="measured", visible=visible, responses=replies, adjudication=adjudication, fact_correct=adjudication["fact_correct"])
                except Exception as exc:
                    recording.update(status="unmeasured", reason=str(exc), responses=replies, fact_correct=None)
                recording["elapsed_seconds"] = time.monotonic() - started
            path = directory / (sid + ".json")
            write(path, recording)
            result["results"].append({"id": sid, "status": recording["status"], "reason": recording.get("reason"), "fact_correct": recording["fact_correct"],
                                      "evidence_file": str(path.relative_to(directory.parent)), "evidence_sha256": sha(path.read_bytes())})
        eligible = [r for r in result["results"] if r["status"] == "measured"]
        result["coverage"] = {"measured": len(eligible), "unsupported": sum(r["status"] == "unsupported" for r in result["results"]),
                              "unmeasured": sum(r["status"] == "unmeasured" for r in result["results"]), "review_required": sum(r["fact_correct"] is None for r in eligible)}
        if stack.provider == "real" and eligible and result["coverage"]["review_required"] == 0:
            result["common_fact_accuracy"] = sum(r["fact_correct"] is True for r in eligible) / len(eligible)
    write(directory / "summary.json", result)
    return result
