import asyncio
import contextvars
import copy
import json
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

from acceptance.budget import Exhausted, Ledger
from acceptance.common import DATA, dataset, read, write, sha
from acceptance.common_facts import common_score
from acceptance.evidence import load_evidence
from acceptance.gate import INVARIANTS, require, verify
from acceptance.gateway import create_app
from acceptance.generate import image_gold, validate
from acceptance.oracle import score, summarize
from acceptance.runtime import parent_run, request_ids
from acceptance.cost_plan import plan
from acceptance.comparison import compare
from acceptance.usage_evidence import check_attribution, validate_recording
from acceptance import baseline
from unittest.mock import patch

LIMITS = {"authorization": "test-only", "requests": 7, "scenarios": 2, "max_output_tokens": 2048, "model": "gpt-6.1-sol"}
BODY = {"model": "gpt-6.1-sol", "max_output_tokens": 2048, "input": "synthetic"}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test-budget.sqlite"
        self.ledger = Ledger(self.path, LIMITS)

    def test_concurrent_reservation_never_overspends(self):
        def reserve(_):
            try:
                return self.ledger.reserve("sql", "responses", BODY)
            except Exhausted:
                return None
        with ThreadPoolExecutor(max_workers=10) as workers:
            attempts = list(workers.map(reserve, range(50)))
        self.assertEqual(sum(a is not None for a in attempts), 7)
        self.assertEqual(Ledger(self.path, LIMITS).report()["remaining_requests"], 0)
        self.assertEqual(self.ledger.report()["unknown_usage_requests"], 7)

    def test_immutable_limits_and_output_cap(self):
        with self.assertRaises(ValueError):
            Ledger(self.path, {**LIMITS, "requests": 100})
        for value in (0, 2049, None, True):
            with self.assertRaises(ValueError):
                self.ledger.reserve("sql", "responses", {**BODY, "max_output_tokens": value})
        self.assertEqual(self.ledger.report()["reserved_requests"], 0)

    def test_request_correlation_late_background_and_restart(self):
        self.ledger.register_scenario("one")
        self.ledger.register_scenario("two")
        for run in ("r1", "r2", "r1", None):
            self.ledger.reserve("sql", "responses", BODY, run_id="s-" + str(run), parent_run_id=run)
        self.ledger.bind_run("helpdesk", "r2", "two")
        self.ledger.bind_run("helpdesk", "r1", "one")
        reopened = Ledger(self.path, LIMITS)
        self.assertEqual([r["scenario"] for r in reopened.report()["attempts"]], ["one", "two", "one", "unknown/background"])
        with self.assertRaises(ValueError):
            reopened.bind_run("helpdesk", "r1", "two")
        with self.assertRaises(Exhausted):
            reopened.register_scenario("three")

    def test_failed_attempt_not_refunded_or_double_finished(self):
        attempt = self.ledger.reserve("helpdesk", "responses", BODY)
        self.ledger.finish(attempt, error="TimeoutError")
        with self.assertRaises(ValueError):
            self.ledger.finish(attempt, usage={"input_tokens": 1})
        self.assertEqual(self.ledger.report()["remaining_requests"], 6)
        self.assertIsNone(self.ledger.report()["billing"]["estimated_usd"])

    def test_gateway_counts_actual_failures_and_forbids_redirect_retry(self):
        async def exercise():
            calls = []
            def supplier(request):
                calls.append(request)
                return httpx.Response(503, json={"error": "synthetic provider failure"})
            app = create_app(self.ledger, "local", {"base_url": "https://supplier.invalid/v1", "api_key": "synthetic"}, httpx.MockTransport(supplier))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                response = await client.post("/helpdesk/v1/responses", json=BODY, headers={"Authorization": "Bearer local", "X-Acceptance-Run": "run-a"})
                self.assertEqual(response.status_code, 503)
                bad = await client.post("/helpdesk/v1/responses", json={**BODY, "max_output_tokens": 5000}, headers={"Authorization": "Bearer local"})
                self.assertEqual(bad.status_code, 400)
            self.assertEqual(len(calls), 1)
        asyncio.run(exercise())
        self.assertEqual(self.ledger.report()["reserved_requests"], 1)

    def test_gateway_disabled_never_reserves_or_sends(self):
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.ledger, "local")), base_url="http://gateway") as client:
                result = await client.post("/sql/v1/responses", json=BODY, headers={"Authorization": "Bearer local"})
                self.assertEqual(result.status_code, 503)
        asyncio.run(exercise())
        self.assertEqual(self.ledger.report()["reserved_requests"], 0)

    def test_stream_usage_is_measured(self):
        async def exercise():
            payload = b'data: {"type":"response.completed","response":{"model":"supplier-alias","usage":{"input_tokens":111,"output_tokens":22}}}\n\n'
            transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"}))
            app = create_app(self.ledger, "local", {"base_url": "https://supplier.invalid/v1", "api_key": "synthetic"}, transport)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                await client.post("/sql/v1/responses", json=BODY, headers={"Authorization": "Bearer local"})
        asyncio.run(exercise())
        row = self.ledger.report()["attempts"][0]
        self.assertEqual(row["usage"]["input_tokens"], 111)
        self.assertEqual(row["response_model"], "supplier-alias")

    def test_cost_plan_requires_measurements_and_never_authorizes_spend(self):
        empty = plan(None)
        self.assertEqual(empty["status"], "insufficient_calibration")
        self.assertIsNone(empty["proposed_request_cap_for_review"])
        measured = plan({"attempts": [{"scenario": "s1", "usage": {"input_tokens": 10, "output_tokens": 2}},
                                       {"scenario": "s1", "usage": None}, {"scenario": "unknown/background", "usage": None}]})
        self.assertFalse(measured["authorized"])
        self.assertIsNone(measured["estimated_cost"])
        self.assertEqual(measured["calibration_actual_http_attempts"], 3)
        self.assertEqual(measured["unknown_usage_attempts"], 2)
        self.assertEqual(measured["observed_calls_per_scenario"], {"s1": 2})
        self.assertIsNone(empty["known_usage_tokens"]["input_tokens"])
        self.assertIsNone(measured["usage"]["input_tokens"]["total"])
        self.assertEqual(measured["usage"]["input_tokens"]["known_sum"], 10)

    def test_release_ledger_rejects_mock_receipts_forged_counts_and_run_reassignment(self):
        limits = {**LIMITS, "scope": "release_and_baseline", "confirmed": True, "id": "unit-budget-001",
                  "user_approval_reference": "unit-test-only-never-real", "corpus_sha256": "test-corpus",
                  "supplier_base_url": "https://supplier.invalid/v1"}
        ledger = Ledger(self.path.parent / "release-test.sqlite", limits)
        ledger.register_scenario("round-1:one")
        ledger.bind_run("helpdesk", "run-one", "round-1:one")
        attempt = ledger.reserve("sql", "responses", BODY, run_id="sql-one", parent_run_id="run-one")
        ledger.finish(attempt, status=200, model="gpt-6.1-sol", receipt={"transport": "supplier_http",
                      "supplier_base_url": limits["supplier_base_url"], "endpoint": "responses", "requested_model": "gpt-6.1-sol",
                      "response_id": "test-fixture-only", "response_sha256": "a" * 64})
        recorded = ledger.report()
        self.assertEqual(validate_recording(recorded, "test-corpus"), {1})
        self.assertEqual(check_attribution(recorded, {"round-1:one": {"run-one"}}, {"round-1"}), {"round-1:one": 1})
        for alteration in ("count", "model", "simulation", "unknown", "correlation", "empty", "cap"):
            changed = copy.deepcopy(recorded)
            if alteration == "count": changed["reserved_requests"] += 1
            if alteration == "model": changed["limits"]["model"] = "other-model"
            if alteration == "simulation": changed["attempts"][0]["receipt"]["transport"] = "mock_http"
            if alteration == "unknown": changed["unknown_usage_requests"] = 0
            if alteration == "correlation": changed["attempts"][0]["scenario"] = "round-1:two"
            if alteration == "empty": changed["attempts"] = []
            if alteration == "cap": changed["limits"]["requests"] = 0
            with self.assertRaises(ValueError, msg=alteration): validate_recording(changed, "test-corpus")
        with self.assertRaises(ValueError): check_attribution(recorded, {"round-1:one": {"some-other-run"}}, {"round-1"})
        with self.assertRaises(ValueError): check_attribution(recorded, {"round-1:one": {"run-one"}}, {"baseline"})


class MethodTests(unittest.TestCase):
    def test_noncomparable_questions_keep_contract_checks_without_common_review(self):
        from acceptance.gate import require_candidate_score
        from acceptance.review import attach
        _, corpus, release = dataset()
        recording = read(Path(__file__).parent / "fixtures" / "images.json")
        scenario = next(s for s in corpus["scenarios"] if s["id"] == recording["observed"]["id"])
        unresolved = {"status": "review_required", "fact_correct": None, "contract_checked": False}
        claimed = {**score(scenario, recording["observed"], corpus), "common_score": unresolved}
        with tempfile.TemporaryDirectory() as tmp, patch("acceptance.gate.common_score", return_value=unresolved):
            root = Path(tmp)
            self.assertTrue(require_candidate_score(root, scenario, claimed, recording, corpus, common_required=False)["fact_correct"])
            with self.assertRaisesRegex(ValueError, "Evidence file and SHA-256"):
                require_candidate_score(root, scenario, claimed, recording, corpus, common_required=True)
            damaged = copy.deepcopy(recording)
            damaged["observed"]["case"]["drafts"][0]["valid"] = False
            with self.assertRaisesRegex(ValueError, "Declared score differs"):
                require_candidate_score(root, scenario, claimed, damaged, corpus, common_required=False)
            unsupported = next(s["id"] for s in release["scenarios"] if s["group"] == "images")
            write(root / "report.json", {"baseline": {"results": [{"id": unsupported, "status": "unsupported"}]}})
            with self.assertRaisesRegex(ValueError, "supported by both versions"):
                attach(root / "report.json", "round-1", unsupported, root / "absent-review.json")

    def test_both_versions_require_bound_independent_review_and_pending_blocks_gate(self):
        from acceptance.review import attach
        from acceptance.common_facts import adjudicate
        scenario = next(s for s in dataset()[2]["scenarios"] if s["group"] == "policies")
        sid = scenario["id"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "new.json", {"observed": {"completed": True, "case": {"drafts": [{"valid": True, "body": "Unit fixture requiring independent review"}]}}})
            write(root / "old.json", {"status": "measured", "visible": "Unit fixture requiring independent review"})
            def entry(name): return {"id": sid, "status": "measured", "fact_correct": None, "common_score": {"status": "review_required", "fact_correct": None}, "evidence_file": name + ".json", "evidence_sha256": sha((root / (name + ".json")).read_bytes())}
            report = {"rounds": [{"results": [entry("new")]}, {"results": []}, {"results": []}], "baseline": {"results": [entry("old")]}}
            write(root / "report.json", report)
            review = {"status": "reviewed", "reviewed_by": "unit-test fixture, not an actual reviewer", "reviewer_type": "human", "independent_of_system_under_test": True,
                      "rationale": "Tests review binding and symmetry only; this fixture is not a business-quality judgment", "fact_correct": True, "evidence_sha256": entry("new")["evidence_sha256"]}
            write(root / "review.json", review)
            first = attach(root / "report.json", "round-1", sid, root / "review.json")
            self.assertTrue(read(first["report"])["rounds"][0]["results"][0]["common_fact_correct"])
            review.update(reviewer_type="ai", reviewer_model="explicit-unit-test-model", evidence_sha256=entry("old")["evidence_sha256"])
            write(root / "review.json", review)
            second = attach(first["report"], "baseline", sid, root / "review.json")
            checked = read(second["report"])
            self.assertTrue(checked["baseline"]["results"][0]["fact_correct"])
            self.assertIn("common_comparison_has_unmeasured_or_unreviewed_questions", verify(checked)["failures"])
            for field in ("reviewer_model", "independent_of_system_under_test", "rationale"):
                broken = {k: v for k, v in review.items() if k != field}
                with self.assertRaises(ValueError): adjudicate(broken, review["evidence_sha256"])
            with self.assertRaises(ValueError): adjudicate(review, "different-evidence")

    def test_baseline_preserves_conversation_context_and_counts_execution_failure(self):
        _, development, _ = dataset()
        corpus = {**development, "scenarios": [next(s for s in development["scenarios"] if s["group"] == "cross_message" and s["subtype"] == kind)
                                                 for kind in ("supplement", "reference", "multiple_cases")]}
        submitted = []
        def respond(request):
            if request.url.path == "/openapi.json":
                return httpx.Response(200, json={"paths": {"/chat": {}}, "components": {"schemas": {"ChatRequest": {"properties": {"message": {}, "conv_id": {}}}}}})
            if request.url.path == "/knowledge/add": return httpx.Response(200, json={})
            if request.url.path == "/chat":
                submitted.append(json.loads(request.content))
                return httpx.Response(202, json={"run_id": "fixture-" + str(len(submitted))})
            failed = len(submitted) > len(corpus["scenarios"][0]["steps"])
            return httpx.Response(200, json={"status": "failed" if failed else "succeeded", "result": {"response": "unmeasured fixture wording"}})
        original_client = httpx.Client
        def client(**kwargs): return original_client(**kwargs, transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as tmp, patch("acceptance.baseline.httpx.Client", client), patch("acceptance.baseline.time.sleep"):
            stack = types.SimpleNamespace(cfg={"api": "https://fixture.invalid", "gateway_token": "fake", "manifest_sha256": "test"}, corpus=corpus,
                provenance={"config_sha256": "test", "artifacts": {}}, provider="simulated")
            result = baseline.run(stack, Path(tmp) / "baseline")
            self.assertEqual([r["status"] for r in result["results"]], ["measured", "measured_failure", "unsupported"])
            self.assertFalse(result["results"][1]["fact_correct"])
            self.assertEqual(result["coverage"]["measured"], 2)
            n = len(corpus["scenarios"][0]["steps"])
            self.assertGreater(n, 1)
            self.assertEqual(len({r["conv_id"] for r in submitted[:n]}), 1)
            self.assertNotEqual(submitted[0]["conv_id"], submitted[-1]["conv_id"])

    def test_bundle_preserves_raw_bytes_and_rejects_tampering_and_traversal(self):
        from acceptance.bundle import pack, receive
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = {"revision": "frozen", "images": {}}
            write(root / "raw.json", {"synthetic": True})
            write(root / "assembled.json", {"artifacts": artifacts, "proof": {"evidence_file": "raw.json", "evidence_sha256": sha((root / "raw.json").read_bytes())}})
            write(root / "manifest.json", artifacts)
            # This isolates archive mechanics; the real gate separately rejects
            # incomplete reports and is never bypassed by the CLI.
            with patch("acceptance.bundle.require", return_value={"artifacts": artifacts}):
                packaged = pack(root / "assembled.json", root / "manifest.json", root / "bundle.zip")
                receive(root / "bundle.zip", packaged["sha256"], root / "received", "frozen")
            self.assertEqual((root / "received/report.json").read_bytes(), (root / "assembled.json").read_bytes())
            self.assertEqual((root / "received/raw.json").read_bytes(), (root / "raw.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                receive(root / "bundle.zip", "0" * 64, root / "bad", "frozen")
            with zipfile.ZipFile(root / "unsafe.zip", "w") as archive: archive.writestr("../escape.json", "{}")
            with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                receive(root / "unsafe.zip", sha((root / "unsafe.zip").read_bytes()), root / "escaped", "frozen")

    def test_evidence_bytes_presence_and_artifact_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = {"revision": "candidate", "images": {"backend": "sha256:one"}}
            write(root / "record.json", {"artifacts": artifacts, "config_sha256": "same", "passed": True})
            reference = {"evidence_file": "record.json", "evidence_sha256": sha((root / "record.json").read_bytes())}
            self.assertTrue(load_evidence(root, reference, artifacts=artifacts)["passed"])
            for bad in ({**reference, "evidence_file": "missing.json"}, {**reference, "evidence_file": "../record.json"}, {**reference, "evidence_sha256": "x"}, {**reference, "evidence_sha256": "0" * 64}):
                with self.assertRaises(ValueError):
                    load_evidence(root, bad)
            with self.assertRaises(ValueError):
                load_evidence(root, reference, artifacts={"revision": "new", "images": artifacts["images"]})
            write(root / "record.json", {"artifacts": artifacts, "config_sha256": "same", "passed": False})
            with self.assertRaises(ValueError):
                load_evidence(root, reference)
    def correct_order(self):
        _, dev, _ = dataset()
        scenario = dev["scenarios"][0]
        order = scenario["entities"][0]
        row = next(r for r in dev["records"]["erp_db"]["orders"] if r["order_no"] == order)
        now = "2026-11-06T20:00:00+00:00"
        returned = {k: row[k] for k in ("order_no", "contract_amount", "order_status")}
        evidence = [{"field": k, "value": v, "database": "erp_db", "step_index": 0, "row_index": 0, "as_of": now, "as_of_source": "query_time", "unit": "USD" if k == "contract_amount" else None} for k, v in returned.items()]
        query = {"source": "business_query", "run_id": "sql-run", "as_of": now, "observed_at": now, "evidence": evidence,
            "steps": [{"final_sql": f"SELECT order_no,contract_amount,order_status FROM orders WHERE order_no='{order}'", "rows": [returned], "success": True, "row_count": 1, "truncated": False,
                       "as_of": now, "as_of_source": "query_time", "observed_at": now}]}
        body = f"订单 {order} [1]\n数据时点 {now}\n- contract amount: {row['contract_amount']} USD\n- 订单状态: {row['order_status']}"
        observed = {"completed": True, "runs": [{"status": "succeeded"}], "case": {"status": "in_progress", "control_mode": "assistant", "pending_fields": [],
            "drafts": [{"valid": True, "body": body, "evidence": [query], "created_at": datetime.fromisoformat(now).timestamp()}]}, "side_effects": {"handoff": 0, "notification": 0}}
        return scenario, observed, dev

    def test_correct_answer_plus_extra_wrong_fact_cannot_pass(self):
        scenario, observed, corpus = self.correct_order()
        self.assertTrue(score(scenario, observed, corpus)["fact_correct"])
        for extra in ("\n> 合同金额 999999 USD", "\n图片已核实 数量99999 pcs", "\n- contract amount: 99999 USD", "\n政策摘录 [P99]：没有来源", "\n订单 SO-2026-99999 [1]", "\n订单 " + scenario["entities"][0] + " [99]"):
            changed = copy.deepcopy(observed)
            changed["case"]["drafts"][0]["body"] += extra
            self.assertFalse(score(scenario, changed, corpus)["fact_correct"], extra)

    def test_common_baseline_scoring_accepts_natural_language(self):
        scenario, observed, corpus = self.correct_order()
        facts = scenario["truth"]["facts"]
        text = f"订单 {scenario['entities'][0]}，合同金额为 {facts[0]['value']} 美元。订单状态是{facts[1]['value']}。"
        self.assertTrue(common_score(scenario, text)["fact_correct"])
        self.assertFalse(common_score(scenario, text + "合同金额为 999999 美元。") ["fact_correct"])
        self.assertIsNone(common_score(scenario, f"订单 {scenario['entities'][0]}，这笔业务已经排入工厂计划。") ["fact_correct"])
        delivery = next(r["delivery_status"] for r in corpus["records"]["svc_db"]["delivery"] if r["order_no"] == scenario["entities"][0])
        wrong = "已送达" if delivery != "已送达" else "待配送"
        self.assertFalse(common_score(scenario, text + "配送状态是" + wrong + "。", corpus)["fact_correct"])
        inventory = next(s for s in corpus["scenarios"] if s["subtype"] == "inventory")
        f = inventory["truth"]["facts"]
        natural = f"物料 {f[0]['entity']}，现有库存为 {f[0]['value']} pcs。已占用库存为 {f[1]['value']} pcs。"
        self.assertTrue(common_score(inventory, natural, corpus)["fact_correct"])

    def test_unknown_requires_successful_query_evidence(self):
        _, dev, _ = dataset()
        scenario = next(s for s in dev["scenarios"] if s["subtype"] == "no_record")
        observed = {"completed": True, "runs": [{"status": "succeeded"}], "case": {"status": "in_progress", "control_mode": "assistant", "pending_fields": [],
            "drafts": [{"valid": True, "body": "查询成功，未找到记录。", "evidence": []}]}, "side_effects": {"handoff": 0, "notification": 0}}
        self.assertIn("no_record_query_not_proven", score(scenario, observed, dev)["errors"])
        query = {"source": "business_query", "steps": [{"final_sql": "SELECT order_no FROM orders WHERE order_no='" + scenario["truth"]["allowed_entities"][0] + "'", "success": True, "row_count": 0, "rows": [], "truncated": False}]}
        observed["case"]["drafts"][0]["evidence"] = [query]
        self.assertTrue(score(scenario, observed, dev)["fact_correct"])
        query["steps"][0]["success"] = False
        self.assertIn("no_record_query_not_proven", score(scenario, observed, dev)["errors"])
    def test_frozen_corpus_and_actual_lineage_isolation(self):
        result = validate()
        self.assertEqual(result["release"], 200)
        self.assertEqual(result["development"], 54)

    def test_image_gold_requires_one_joint_row_and_visible_model(self):
        _, dev, _ = dataset()
        image = next(s for s in dev["scenarios"] if s["group"] == "images")
        expected = image["truth"]["image_fields"]
        pixels = {name: {k: v for k, v in spec.items() if k in {"value", "readability", "kind", "unit"}}
                  for name, spec in expected.items()}
        order = image["entities"][0]
        self.assertTrue(all(r["outcome"] == "matched" for r in image_gold(dev["records"], order, pixels).values()))
        for field, wrong in (("quantity", "999"), ("model", "wrong"), ("order_ref", "SO-wrong")):
            changed = copy.deepcopy(pixels)
            changed[field]["value"] = wrong
            verdicts = image_gold(dev["records"], order, changed)
            self.assertEqual(verdicts[field]["outcome"], "conflict")
            self.assertFalse(any("matched" in r["acceptable_outcomes"] for r in verdicts.values()))
        pixels["model"].update(value=None, readability="missing")
        verdicts = image_gold(dev["records"], order, pixels)
        self.assertEqual(verdicts["model"]["outcome"], "missing")
        for field in ("quantity", "date", "status"):
            self.assertEqual(verdicts[field]["verification_rule"], "missing_model_anchor")
            self.assertNotIn("matched", verdicts[field]["acceptable_outcomes"])
        for s in dev["scenarios"]:
            if s["group"] == "images":
                self.assertEqual(set(s["truth"]["pending_fields"]), {"image:" + k for k, v in s["truth"]["image_fields"].items() if v["outcome"] != "matched"})

    def test_real_service_recordings_are_transport_compatible(self):
        _, corpus, _ = dataset()
        for group in ("orders", "policies", "images", "missing_field"):
            recording = read(Path(__file__).parent / "fixtures" / (group + ".json"))
            self.assertFalse(recording["model_quality_claim"])
            self.assertEqual(recording["provenance"]["provider_mode"], "simulated")
            observed = recording["observed"]
            scenario = next(s for s in corpus["scenarios"] if s["id"] == observed["id"])
            result = score(scenario, observed, corpus)
            self.assertTrue(result["fact_correct"], (group, result["errors"]))
            if group == "orders":
                # The raw payload lives beneath the actual persisted wrapper.
                business = next(e for e in observed["case"]["drafts"][0]["evidence"] if e.get("source") == "business_query")
                business["evidence_payload"]["steps"][0]["rows"][0]["contract_amount"] += 1
                self.assertFalse(score(scenario, observed, corpus)["fact_correct"])

    def test_corrupted_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "manifest.json").write_text(json.dumps({"files": {"../escape": "bad"}}))
            with self.assertRaises(ValueError):
                dataset(Path(root))

    def test_concurrent_contexts_and_thread_propagation(self):
        variable = contextvars.ContextVar("test_run", default=None)
        previous = sys.modules.get("app.runs.memo")
        sys.modules["app.runs.memo"] = types.SimpleNamespace(current_run=lambda: variable.get())
        async def one(index):
            token = variable.set(types.SimpleNamespace(run_id=str(index)))
            parent_token = parent_run.set("parent-" + str(index))
            await asyncio.sleep(0.001 * (index % 3))
            try:
                return await asyncio.to_thread(request_ids)
            finally:
                variable.reset(token)
                parent_run.reset(parent_token)
        async def all_():
            return await asyncio.gather(*(one(i) for i in range(20)))
        try:
            self.assertEqual(asyncio.run(all_()), [(str(i), "parent-" + str(i)) for i in range(20)])
        finally:
            if previous is None:
                sys.modules.pop("app.runs.memo", None)
            else:
                sys.modules["app.runs.memo"] = previous

    def test_no_unrun_scenario_dropped_from_denominator(self):
        _, dev, _ = dataset()
        result = summarize(dev["scenarios"], [])
        self.assertEqual(result["total"], 54)
        self.assertEqual(result["accuracy"], 0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["image_fields"]["status/all"]["total"], 15)

    def test_release_refuses_stage5_only_and_calibration(self):
        for partial in ({}, {"kind": "development_calibration", "summary": {"accuracy": 1}}, {"completed_at": "today", "resource_isolation_verified": True}):
            self.assertFalse(verify(partial)["passed"])

    def test_optimistic_release_summary_cannot_replace_recordings(self):
        _, _, corpus = dataset()
        artifacts = {"revision": "unit-test-forgery", "images": {k: "sha256:test" for k in ("backend", "frontend", "sql")}}
        manifest = sha((DATA / "manifest.json").read_bytes())
        results = [{"id": s["id"], "group": s["group"], "fact_correct": True, "evidence_sha256": "0" * 64,
                    "evidence_file": "absent.json", "image_fields": [{"field": f} for f in ("order_ref", "model", "quantity", "date", "status")]} for s in corpus["scenarios"]]
        component = {"passed": True, "artifacts": artifacts, "evidence_sha256": "0" * 64}
        report = {"schema_version": 1, "kind": "release_acceptance", "manifest_sha256": manifest,
                  "artifacts": artifacts, "config_sha256": "test-config", "rounds": [
                      {"stack_id": str(i), "real_provider": True, "fresh_database": True, "fresh_caches": True,
                       "execution": "immutable_containers", "harness_sha256": "test-harness",
                       "artifacts": artifacts, "config_sha256": "test-config", "results": copy.deepcopy(results)} for i in range(3)],
                  "invariants": {**component, "checks": dict.fromkeys(INVARIANTS, True), "trials": {k: {"total": 1, "violations": 0} for k in INVARIANTS}},
                  "browser": {**component, **dict.fromkeys(("employee_login", "upload", "multiple_turns", "approval", "failure_recovery", "real_provider"), True)},
                  "load": {**component, "authenticated_accounts": 50, "profiles": {k: {"passed": True, "records": ["invented"], "queue": {"final_queued": 0, "final_running": 0}} for k in ("weekly_steady", "tenfold_burst", "fifty_simultaneous")}, "real_provider_sample": True, "infrastructure_only_labeled": True},
                  "rollback": component, "baseline": {"revision": "5310da9", "model": "gpt-6.1-sol", "manifest_sha256": manifest,
                      "provenance": {"real_provider": True, "execution": "immutable_containers"},
                      "config_sha256": "test-config", "results": [{"id": s["id"], "status": "unsupported", "reason": "invented"} for s in corpus["scenarios"]]},
                  "budget": {"authorized": True, "exceeded": False}}
        for name in ("recovery_local", "recovery_s3"):
            report[name] = {**component, "nonempty_sql_audit": True, "nonempty_image_extractions": True, "case_count": 200, "rpo_seconds": 1, "rto_seconds": 1}
        self.assertIn("persistent_supplier_usage_evidence_missing", verify(report)["failures"])
        self.assertIn("load_evidence_not_separated", verify(report)["failures"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            write(path, report)
            with self.assertRaisesRegex(ValueError, "Unified release acceptance failed"):
                require(path)
        changed = copy.deepcopy(report)
        changed["rounds"][0]["artifacts"] = {**artifacts, "revision": "another-candidate"}
        self.assertFalse(verify(changed)["passed"])
        changed = copy.deepcopy(report)
        changed["invariants"]["trials"]["worker_restart"]["total"] = 0
        self.assertIn("invariant_trial_denominators_missing_or_nonzero", verify(changed)["failures"])
        report["common_comparison"] = compare(report["rounds"], report["baseline"])
        report["common_comparison"]["baseline"]["correct"] += 1
        self.assertIn("common_comparison_totals_mismatch", verify(report)["failures"])

    def test_safe_unanswered_cannot_count_as_order_answer(self):
        _, dev, _ = dataset()
        scenario = dev["scenarios"][0]
        observed = {"completed": True, "runs": [{"status": "succeeded"}], "case": {"status": "in_progress", "control_mode": "assistant", "pending_fields": [],
            "drafts": [{"valid": True, "body": "请提供需要核实的问题及相关材料。业务事实需要有效的政策或业务记录支持。", "evidence": []}]},
            "side_effects": {"handoff": 0, "notification": 0}}
        result = score(scenario, observed, dev)
        self.assertTrue(result["run_terminal"])
        self.assertFalse(result["business_resolved"])
        self.assertFalse(result["fact_correct"])


if __name__ == "__main__":
    unittest.main()
