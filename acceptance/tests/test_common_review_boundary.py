"""Fictional evidence boundaries for the common three-valued comparison."""
from collections import Counter
from copy import deepcopy
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.common import read, sha, write
from acceptance.common_facts import common_score
from acceptance.gate import require_candidate_score


FIXTURE = Path(__file__).parent / "fixtures/common-format-v3/reproductions.json"

# These are test expectations, never scorer dispatch rules. Full fictional
# semantics remain contradicted; a text-only API cannot prove every verdict.
REVIEW_LIMITS = {
    "extra_charge": "The source_schema binding 额外费用 to extra_charge, its unit and closed scope is not an API argument.",
    "unauthorized_guarantee": "Applicable published rules and their scope are not API arguments; a boolean column cannot supply their meaning.",
    "wrong_row_count": "No trusted query projection, returned rows or row_count reaches this API.",
    "wrong_total": "The corpus does not declare a complete query result set or establish aggregate scope.",
    "swapped_roles_in_table": "Unknown table headers do not prove current assertion context; preserve the full table for review.",
    "affirmative_actual_role_change": "The anaphoric date and affirmative role change are outside the bounded scalar parser.",
    "wrong_short_date_restatement": "The partial date's role and antecedent are not fully parsed; raw text must be independently reviewed.",
}


class CommonReviewBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = read(FIXTURE)

    def source(self, family=0):
        f = self.fixture["families"][family]
        return deepcopy(f["scenario"]), f["plain_reference"], deepcopy(f["corpus"])

    def test_fictional_fixture_and_three_argument_interface_are_pinned(self):
        self.assertEqual(sha(FIXTURE.read_bytes()), "c5577e8ae1f81e1c8c1eaf1b1eb6d3d9aaa352086e07174deac56bf54bc4211f")
        self.assertEqual(self.fixture["source_sha256"], "c35088d2744ef098f5e4135f603c378c6000bf38b9af2b27f735305a3d185304")
        self.assertFalse(self.fixture["contains_real_answers"])
        self.assertEqual(str(inspect.signature(common_score)), "(scenario, visible, corpus=None)")

    def test_all_v3_inputs_keep_unknowns_pending_and_bound_contradictions_false(self):
        counts = Counter()
        for family in self.fixture["families"]:
            for section in ("positive_variants", "proven_negative_controls", "unresolved_controls"):
                for case in family[section]:
                    with self.subTest(family=family["id"], case=case["id"]):
                        override = case.get("overrides", {})
                        scenario = override.get("scenario", family["scenario"])
                        corpus = override.get("corpus", family["corpus"])
                        actual = common_score(scenario, case["visible"], corpus)
                        expected = "incorrect" if section == "proven_negative_controls" and case["id"] not in REVIEW_LIMITS else "review_required"
                        self.assertEqual(actual["status"], expected, actual)
                        self.assertIsNot(actual["fact_correct"], True)
                        self.assertEqual(actual, common_score(deepcopy(scenario), case["visible"], deepcopy(corpus)))
                        if actual["status"] == "review_required":
                            self.assertEqual(actual["review_context"]["original_text"], case["visible"])
                        counts[section, actual["status"]] += 1
        self.assertEqual(counts, {("positive_variants", "review_required"): 10,
                                  ("proven_negative_controls", "incorrect"): 21,
                                  ("proven_negative_controls", "review_required"): 15,
                                  ("unresolved_controls", "review_required"): 5})

    def test_unbound_number_alone_never_falls_through_to_correct(self):
        scenario, plain, corpus = self.source()
        for number in ("6147", "-6147", "6147.25", "9,147", "2031-02-03", "8月29日"):
            with self.subTest(number=number):
                text = plain + "\n" + number
                actual = common_score(scenario, text, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertIn("unbound_numeric_claim", actual["errors"])
                self.assertEqual(actual["review_context"]["original_text"], text)
                self.assertIn(number, actual["review_context"]["unresolved_text"])
                if number == "6147":
                    self.assertNotIn("unparsed_additional_prose", actual["errors"])

    def test_unbound_text_units_and_relations_never_gain_a_pass(self):
        scenario, plain, corpus = self.source()
        for extra in ("复核码6147", "参考批次ABC", "额外费用：61.70 USD", "补件数量：11 箱", "名称：Hinge BX-96003-24", "BX-96003-24"):
            with self.subTest(extra=extra):
                actual = common_score(scenario, plain + "\n" + extra, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertIn(extra, actual["review_context"]["original_text"])
        for prefix in ("历史示例：", "错误示例：", "并非", "不是", "未知限定语"):
            with self.subTest(prefix=prefix):
                text = plain + "\n" + prefix + "合同金额：613.80 USD"
                actual = common_score(scenario, text, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertIn("unparsed_claim_context:contract_amount", actual["errors"])

    def test_unknown_text_cannot_cancel_a_bound_contradiction_or_explicit_refusal(self):
        scenario, plain, corpus = self.source()
        for claim in ("合同金额：613.80 USD", "合同金额：612.80 CNY", "补件数量：11 kg", "实际送达日期：2029-08-29",
                      "名称：Hinge BX-96002-24", "订单 SO-2026-Q96002"):
            with self.subTest(claim=claim):
                actual = common_score(scenario, plain + "\n" + claim + "\n未知复核码6147", corpus)
                self.assertEqual(actual["status"], "incorrect", actual)
                self.assertIs(actual["fact_correct"], False)
        actual = common_score(scenario, "订单 SO-2026-Q96001\n补件状态：处理中；目前无法提供补件数量。\n复核码6147", corpus)
        self.assertEqual(actual["status"], "incorrect", actual)
        self.assertIn("required_fact_explicitly_unanswered:quantity", actual["errors"])

    def test_unit_completeness_and_unresolved_cross_span_binding_are_distinct(self):
        scenario, plain, corpus = self.source()
        bare = plain.replace("11 件", "11")
        missing = common_score(scenario, bare, corpus)
        self.assertEqual(missing["status"], "incorrect", missing)
        self.assertIn("required_unit_explicitly_missing:quantity", missing["errors"])
        for note in ("该行共11件。", "数量以个为单位。", "单位含义待确认。", "units remain unverified"):
            with self.subTest(note=note):
                actual = common_score(scenario, bare + "\n" + note, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertIn("unparsed_unit_binding:quantity", actual["errors"])

    def test_automatic_success_requires_every_source_field_not_only_known_labels(self):
        scenario, _, corpus = self.source()
        entity = scenario["truth"]["allowed_entities"][0]
        scenario["truth"]["facts"] = [{"entity": entity, "field": "unrecognized_field", "value": "ready", "unit": None}]
        actual = common_score(scenario, "订单 " + entity, corpus)
        self.assertEqual(actual["status"], "review_required", actual)
        self.assertIn("unparsed_required_fact:unrecognized_field", actual["errors"])
        scenario["truth"]["facts"] = [{"entity": entity, "field": "phone", "value": "5550199", "unit": None,
                                        "source": {"database": database, "table": table}}
                                       for database, table in (("erp_db", "orders"), ("svc_db", "delivery"))]
        actual = common_score(scenario, "订单 " + entity + "\nCustomer phone: 5550199", corpus)
        self.assertEqual(actual["status"], "review_required", actual)
        self.assertEqual(actual["claims"][0]["source"], {"database": "erp_db", "table": "orders"})

    def test_missing_relation_is_unknown_but_foreign_parent_is_counterevidence(self):
        scenario, plain, corpus = self.source()
        text = plain + "\n补件名称：Hinge BX-96001-24"
        no_join = deepcopy(corpus)
        no_join["records"]["svc_db"]["replacement_part"][0].pop("part_name")
        actual = common_score(scenario, text, no_join)
        self.assertEqual(actual["status"], "review_required", actual)
        self.assertIn("unparsed_entity_relation", actual["errors"])
        foreign = common_score(scenario, text.replace("BX-96001", "BX-96002"), corpus)
        self.assertEqual(foreign["status"], "incorrect", foreign)

    def test_unknown_vertical_headers_preserve_context_and_do_not_assert_example_rows(self):
        scenario, _, corpus = self.source(3)
        rows = "\n|---|---|\n| 约定交付日期 | 2029-08-12 |\n| 实际送达日期 | 2029-08-29 |"
        for header in ("历史示例", "错误示例", "以下不是当前事实", "核对项", "额外费用：61.70 USD", "保证于2029-08-30到货"):
            with self.subTest(header=header):
                text = "订单 SO-2026-Q96001\n| " + header + " | 记录值 |" + rows
                actual = common_score(scenario, text, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertIn("unparsed_table_header_context", actual["errors"])
                self.assertIn(header, actual["review_context"]["unresolved_text"])
                self.assertIn(rows, actual["review_context"]["unresolved_text"])
        # Closed header claims remain checkable even while the table body is
        # opaque. The source-bound counterexample is not merely a stray digit.
        for header in ("合同金额：613.80 USD", "合同金额：612.80 CNY", "实际送达日期：2029-08-29"):
            with self.subTest(header=header):
                text = "订单 SO-2026-Q96001\n| " + header + " | 记录值 |" + rows
                actual = common_score(scenario, text, corpus)
                self.assertEqual(actual["status"], "incorrect", actual)
                self.assertIn(header, actual["review_context"]["original_text"])
                historical = text.replace("| 记录值 |", "| 历史示例 |")
                self.assertEqual(common_score(scenario, historical, corpus)["status"], "review_required")

    def test_known_layout_headers_still_bind_date_roles_and_keep_unknown_rows(self):
        scenario, _, corpus = self.source(3)
        text = ("订单 SO-2026-Q96001\n| 项目 | 查询结果 |\n|---|---|\n"
                "| 约定交付日期 | 2029-08-29 |\n| 配送状态 | 已送达 |\n| 实际送达日期 | 2029-08-12 |")
        self.assertEqual(common_score(scenario, text, corpus)["status"], "correct")
        self.assertEqual(common_score(scenario, text.replace("2029-08-29", "2029-08-30"), corpus)["status"], "incorrect")
        extra = text + "\n| 复核码 | 6147 |"
        actual = common_score(scenario, extra, corpus)
        self.assertEqual(actual["status"], "review_required", actual)
        self.assertIn("6147", actual["review_context"]["unresolved_text"])

    def test_unknown_horizontal_header_or_row_context_cannot_assert_historical_values(self):
        scenario, plain, corpus = self.source(3)
        for title in ("历史示例", "错误示例", "以下不是当前事实", "额外费用：61.70 USD", "保证于2029-08-30到货"):
            with self.subTest(title=title):
                table = f"| {title} | 合同金额（USD） |\n|---|---:|\n| 示例 | 613.80 CNY |"
                actual = common_score(scenario, plain + "\n" + table, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertIn(table, actual["review_context"]["unresolved_text"])
        row = "| 历史示例中的合同金额 | 613.80 CNY |"
        actual = common_score(scenario, plain + "\n| 项目 | 查询结果 |\n|---|---|\n" + row, corpus)
        self.assertEqual(actual["status"], "review_required", actual)
        self.assertIn("unparsed_table_row_context", actual["errors"])

    def test_query_and_aggregate_provenance_is_not_invented_from_corpus_rows(self):
        scenario, plain, corpus = self.source()
        for extra in ("数据时点 2029-08-16T06:23:41Z", "查询时间：2029-08-16 06:23 UTC", "查得2条补件记录，合计99件。"):
            with self.subTest(extra=extra):
                actual = common_score(scenario, plain + "\n" + extra, corpus)
                self.assertEqual(actual["status"], "review_required", actual)
                self.assertEqual(actual["review_context"]["original_text"], plain + "\n" + extra)

    def test_gate_requires_bound_review_and_cannot_override_deterministic_errors(self):
        scenario, plain, corpus = self.source()
        text = plain + "\n复核码6147"
        with tempfile.TemporaryDirectory() as folder, patch("acceptance.gate.score", return_value={}):
            root = Path(folder)
            observed = {"completed": True, "case": {"drafts": [{"valid": True, "body": text}]}}
            raw = {"observed": observed}
            write(root / "raw.json", raw)
            raw_hash = sha((root / "raw.json").read_bytes())
            claimed = {"id": "fictional-review-binding", "evidence_sha256": raw_hash,
                       "common_score": common_score(scenario, text, corpus), "common_fact_correct": True}
            with self.assertRaisesRegex(ValueError, "Evidence file and SHA-256"):
                require_candidate_score(root, scenario, claimed, raw, corpus, common_required=True)
            review = {"status": "reviewed", "reviewed_by": "synthetic unit fixture, not an actual reviewer",
                      "reviewer_type": "ai", "reviewer_model": "fixture-only", "independent_of_system_under_test": True,
                      "rationale": "Synthetic fixture tests binding of a whole-answer review only, not business quality.",
                      "evidence_sha256": "wrong-original-hash", "fact_correct": True}
            write(root / "review.json", review)
            claimed["review"] = {"evidence_file": "review.json", "evidence_sha256": sha((root / "review.json").read_bytes())}
            with self.assertRaisesRegex(ValueError, "does not bind"):
                require_candidate_score(root, scenario, claimed, raw, corpus, common_required=True)
            review["evidence_sha256"] = raw_hash
            write(root / "review.json", review)
            claimed["review"]["evidence_sha256"] = sha((root / "review.json").read_bytes())
            self.assertEqual(require_candidate_score(root, scenario, claimed, raw, corpus, common_required=True), {})
            review["fact_correct"] = False
            write(root / "review.json", review)
            claimed["review"]["evidence_sha256"] = sha((root / "review.json").read_bytes())
            claimed["common_fact_correct"] = False
            self.assertEqual(require_candidate_score(root, scenario, claimed, raw, corpus, common_required=True), {})
            observed["case"]["drafts"][0]["body"] += "\n合同金额：613.80 USD"
            claimed["common_fact_correct"] = True
            claimed["common_score"] = common_score(scenario, observed["case"]["drafts"][0]["body"], corpus)
            self.assertEqual(claimed["common_score"]["status"], "incorrect")
            with self.assertRaisesRegex(ValueError, "lacks the same bound independent review"):
                require_candidate_score(root, scenario, claimed, raw, corpus, common_required=True)


if __name__ == "__main__":
    unittest.main()
