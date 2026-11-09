"""Offline regressions bound to unmodified real records and their original scores."""
import copy
import unittest
from pathlib import Path

from acceptance.common import ROOT, read, sha
from acceptance.oracle import score, stamp


FIXTURES = Path(__file__).parent / "fixtures/oracle-recordings-61d88ef"
V2_DATA = ROOT / "acceptance/data/v2"
IMAGE_IDS = ("development-images-006", "development-images-011")
IMAGE_COUNTEREXAMPLES = (
    "same_line_bad_value", "same_line_verified_assertion", "new_line_bad_value",
    "wrong_request", "wrong_case_pending_and_request", "duplicate_request",
    "unverified_promoted", "missing_material", "clarification_false",
    "missing_pending", "wrong_status", "human_control", "wrong_image_hash",
    "material_not_ready", "missing_reviews", "review_other_case", "wrong_attachment",
    "wrong_draft_run", "wrong_draft_id", "not_applied", "unrelated_clarification_run",
    "wrong_extracted_value", "unmeasured_side_effects", "unauthorized_handoff",
)


def current_draft(observed):
    return next(d for d in observed["case"]["drafts"] if d["valid"])


class OracleRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = read(V2_DATA / "development.json")
        cls.scenarios = {s["id"]: s for s in cls.corpus["scenarios"]}
        cls.provenance = read(FIXTURES / "provenance.json")
        cls.recordings = {Path(name).stem: read(FIXTURES / name) for name in cls.provenance["recordings"]}

    def evaluate(self, sid, observed):
        return score(self.scenarios[sid], observed, self.corpus)

    def test_original_recording_bytes_and_frozen_manifest_remain_bound(self):
        self.assertEqual(sha((V2_DATA / "manifest.json").read_bytes()), self.provenance["manifest_sha256"])
        for name, reference in self.provenance["recordings"].items():
            with self.subTest(recording=name):
                self.assertEqual(sha((FIXTURES / name).read_bytes()), reference["sha256"])
                self.assertFalse(self.recordings[Path(name).stem]["score"]["fact_correct"])

    def test_real_clarifications_only_remove_the_unproven_prose_error(self):
        for sid in IMAGE_IDS:
            with self.subTest(scenario=sid):
                recording = self.recordings[sid]
                original = recording["score"]
                self.assertEqual(original["errors"], ["unproven_visible_prose:" + current_draft(recording["observed"])["body"]])
                revised = self.evaluate(sid, recording["observed"])
                self.assertEqual(revised, {**original, "errors": [], "fact_correct": True})
                self.assertFalse(revised["business_resolved"])

    def test_complete_field_list_is_order_independent(self):
        sid = IMAGE_IDS[1]
        observed = copy.deepcopy(self.recordings[sid]["observed"])
        draft = current_draft(observed)
        labels = draft["body"].removeprefix("材料已保存，请补充：").removesuffix("。").split("、")
        draft["body"] = "材料已保存，请补充：" + "、".join(reversed(labels)) + "。"
        self.assertTrue(self.evaluate(sid, observed)["fact_correct"])

    def test_clarification_does_not_hide_false_claims_or_invalid_state(self):
        # The first nine mutations reproduce the 18 independently reviewed counterexamples.
        for sid in IMAGE_IDS:
            for change in IMAGE_COUNTEREXAMPLES:
                with self.subTest(scenario=sid, change=change):
                    observed = copy.deepcopy(self.recordings[sid]["observed"])
                    case, draft = observed["case"], current_draft(observed)
                    result = observed["runs"][-1]["result"]
                    body = draft["body"]
                    if change == "same_line_bad_value":
                        draft["body"] = body[:-1] + "，数量为999 pcs。"
                    elif change == "same_line_verified_assertion":
                        draft["body"] += "订单号已核实。"
                    elif change == "new_line_bad_value":
                        draft["body"] += "\n数量为999 pcs。"
                    elif change in {"wrong_request", "wrong_case_pending_and_request"}:
                        draft["body"] = "材料已保存，请补充：图片核实：日期。"
                        if change == "wrong_case_pending_and_request":
                            case["pending_fields"] = ["image:date"]
                    elif change == "duplicate_request":
                        draft["body"] = body[:-1] + "、图片核实：订单号。"
                    elif change == "unverified_promoted":
                        case["facts"]["order_ref"] = {"source": "verified_image", "value": "wrong"}
                    elif change == "missing_material":
                        case["materials"] = []
                    elif change == "clarification_false":
                        result["needs_clarification"] = False
                    elif change == "missing_pending":
                        case["pending_fields"] = []
                    elif change == "wrong_status":
                        case["status"] = "in_progress"
                    elif change == "human_control":
                        case["control_mode"] = "human"
                    elif change == "wrong_image_hash":
                        for material in case["materials"]:
                            material["sha256"] = material["attachment"]["sha256"] = "0" * 64
                        for review in case["image_reviews"]:
                            review["sha256"] = "0" * 64
                    elif change == "material_not_ready":
                        case["materials"][0]["attachment"]["state"] = "pending"
                    elif change == "missing_reviews":
                        case["image_reviews"] = []
                    elif change == "review_other_case":
                        case["image_reviews"][0]["case_id"] = "unrelated"
                    elif change == "wrong_attachment":
                        case["image_reviews"][0]["attachment_id"] = "unrelated"
                    elif change == "wrong_draft_run":
                        draft["run_id"] = "unrelated"
                    elif change == "wrong_draft_id":
                        result["draft_id"] = "unrelated"
                    elif change == "not_applied":
                        result["case_applied"] = False
                    elif change == "unrelated_clarification_run":
                        result["needs_clarification"] = False
                        observed["runs"].append({"run_id": "unrelated", "status": "succeeded",
                                                 "result": {"needs_clarification": True}})
                    elif change == "wrong_extracted_value":
                        case["image_reviews"][0]["result"]["fields"][0]["value"] = "wrong"
                    elif change == "unmeasured_side_effects":
                        observed.pop("side_effects")
                    elif change == "unauthorized_handoff":
                        observed["side_effects"]["handoff"] = 1
                    verdict = self.evaluate(sid, observed)
                    self.assertFalse(verdict["fact_correct"])
                    self.assertTrue(verdict["errors"] or verdict["invariant_violations"])
                    if change == "new_line_bad_value":
                        self.assertEqual(verdict["errors"], ["unproven_visible_prose:数量为999 pcs。"])

    def test_policy_recordings_keep_their_original_failed_scores(self):
        for sid in ("development-policies-001", "development-policies-002"):
            with self.subTest(scenario=sid):
                recording = self.recordings[sid]
                self.assertEqual(self.evaluate(sid, recording["observed"]), recording["score"])
                self.assertEqual(recording["score"]["invariant_violations"], ["unauthorized_or_duplicate_handoff"])

    def test_delivery_recording_keeps_real_handoff_failure_after_clock_correction(self):
        sid = "development-orders-003"
        recording = self.recordings[sid]
        self.assertEqual(recording["score"]["errors"], ["incorrect_data_time", "required_fact_missing:delivery_status",
            "unsupported_visible_claim:delivery status", "visible_data_time_mismatch", "wrong_entity_or_citation_header"])
        self.assertEqual(self.evaluate(sid, recording["observed"]), {**recording["score"], "errors": []})
        self.assertFalse(self.evaluate(sid, recording["observed"])["fact_correct"])

    def delivery_without_handoff(self):
        # Synthetic control only. The original recording and its handoff are never edited.
        sid = "development-orders-003"
        observed = copy.deepcopy(self.recordings[sid]["observed"])
        observed["side_effects"] = {"actions": [], "notifications": [], "handoff": 0, "notification": 0}
        draft = current_draft(observed)
        query = next(q for q in draft["evidence"] if q.get("source") == "business_query")
        return sid, observed, draft, query, query["evidence_payload"]

    def test_separate_step_clocks_allow_empty_primary_key_lineage(self):
        sid, observed, _, query, payload = self.delivery_without_handoff()
        first, second = payload["steps"]
        self.assertAlmostEqual(stamp(second["observed_at"]) - stamp(first["observed_at"]), 7.922, places=3)
        self.assertEqual(stamp(payload["observed_at"]), stamp(first["observed_at"]))
        self.assertEqual(second["source_records"], [])
        self.assertTrue(payload["trace_id"] and second["final_sql"])
        self.assertTrue(all(c["observed_at"] == payload["observed_at"] for c in query["evidence"]))
        self.assertTrue(self.evaluate(sid, observed)["fact_correct"])

    def test_delivery_timing_value_and_source_counterexamples_still_fail(self):
        changes = ("cell_other_step_time", "step_other_observation", "wrong_step_index",
                   "wrong_aggregate_observation", "wrong_aggregate_time", "missing_step_observation",
                   "missing_publication_time", "expired_at_publication", "oldest_expired_latest_fresh",
                   "publication_before_observation", "wrong_visible_status", "wrong_row_and_cell_status",
                   "missing_source_ids", "missing_sql", "missing_source_ids_and_sql")
        for change in changes:
            with self.subTest(change=change):
                sid, observed, draft, query, payload = self.delivery_without_handoff()
                first, second = payload["steps"]
                if change == "cell_other_step_time":
                    for cell in query["evidence"]:
                        if cell["step_index"] == 1:
                            cell["as_of"] = first["as_of"]
                elif change == "step_other_observation":
                    second["observed_at"] = first["observed_at"]
                elif change == "wrong_step_index":
                    query["evidence"][-1]["step_index"] = 0
                elif change == "wrong_aggregate_observation":
                    payload["observed_at"] = second["observed_at"]
                elif change == "wrong_aggregate_time":
                    payload["as_of"] = second["as_of"]
                elif change == "missing_step_observation":
                    second.pop("observed_at")
                elif change == "missing_publication_time":
                    draft.pop("created_at")
                elif change == "expired_at_publication":
                    draft["created_at"] = stamp(second["observed_at"]) + 301
                elif change == "oldest_expired_latest_fresh":
                    draft["created_at"] = stamp(second["observed_at"]) + 299
                elif change == "publication_before_observation":
                    draft["created_at"] = stamp(first["observed_at"]) + 1
                elif change == "wrong_visible_status":
                    draft["body"] = draft["body"].replace("delivery status: 已送达", "delivery status: 待配送")
                elif change == "wrong_row_and_cell_status":
                    second["rows"][0]["delivery_status"] = "待配送"
                    query["evidence"][-1]["value"] = "待配送"
                    draft["body"] = draft["body"].replace("delivery status: 已送达", "delivery status: 待配送")
                if change in {"missing_source_ids", "missing_source_ids_and_sql"}:
                    payload.pop("trace_id")
                    payload.pop("run_id")
                if change in {"missing_sql", "missing_source_ids_and_sql"}:
                    second["final_sql"] = ""
                verdict = self.evaluate(sid, observed)
                self.assertFalse(verdict["fact_correct"])
                self.assertTrue(verdict["errors"])
                self.assertEqual(verdict["invariant_violations"], [])

    def test_five_minute_window_uses_historical_publication_time(self):
        sid, observed, draft, _, payload = self.delivery_without_handoff()
        earliest = min(stamp(s["as_of"]) for s in payload["steps"])
        draft["created_at"] = earliest + 300
        self.assertTrue(self.evaluate(sid, observed)["fact_correct"])
        draft["created_at"] += .001
        self.assertIn("incorrect_data_time", self.evaluate(sid, observed)["errors"])


if __name__ == "__main__":
    unittest.main()
