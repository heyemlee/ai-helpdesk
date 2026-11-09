"""Fictional, version-neutral text grammar and source-binding regressions."""
from copy import deepcopy
import inspect
import json
from pathlib import Path
import unittest

from acceptance.common import read, sha
from acceptance.common_facts import common_score


FIXTURE = Path(__file__).parent / "fixtures/common-format-v2/reproductions.json"


class CommonFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = read(FIXTURE)
        cls.families = {f["id"]: f for f in cls.fixture["families"]}

    def case(self, family, section="positive_variants", index=0):
        f = self.families["v2_" + family]
        v = f[section][index]
        return deepcopy(v.get("scenario_override", f["scenario"])), v["visible"], deepcopy(v.get("corpus_override", f["corpus"]))

    def test_fixture_is_independent_fiction_and_public_signature_is_unchanged(self):
        self.assertFalse(self.fixture["contains_real_answers"])
        self.assertEqual(sha(FIXTURE.read_bytes()), "31030c435aff5e02c5391968e8f81cd090eb4215e22fa6b9b44b0574b3e4be5e")
        self.assertEqual(self.fixture["source_sha256"], "465c91add21d63ad63ed9ac48faf6c88fd26a01779c4cac2473af62ba57b8e0e")
        self.assertEqual(len(self.families), 12)
        self.assertEqual(sum(len(f["positive_variants"]) for f in self.families.values()), 23)
        self.assertEqual(sum(len(f["negative_controls"]) for f in self.families.values()), 31)
        self.assertEqual(str(inspect.signature(common_score)), "(scenario, visible, corpus=None)")

    def test_all_handoff_formats_keep_correct_scalars_and_both_versions_share_rules(self):
        for f in self.families.values():
            for variant in f["positive_variants"]:
                with self.subTest(family=f["id"], variant=variant["name"]):
                    result = common_score(f["scenario"], variant["visible"], f["corpus"])
                    self.assertIn(result["status"], {"correct", "review_required"}, result)
                    required = {(fact["entity"], fact["field"]) for fact in f["scenario"]["truth"]["facts"]}
                    answered = {(claim["entity"], claim["field"]) for claim in result["claims"] if claim["correct"]}
                    self.assertLessEqual(required, answered, result)
                    # No product/version argument or version-dependent branch.
                    self.assertEqual(result, common_score(deepcopy(f["scenario"]), variant["visible"], deepcopy(f["corpus"])))
                    self.assertFalse(result["contract_checked"])

    def test_all_negative_controls_remain_blocked(self):
        for f in self.families.values():
            for variant in f["negative_controls"]:
                with self.subTest(family=f["id"], variant=variant["name"]):
                    result = common_score(variant.get("scenario_override", f["scenario"]), variant["visible"],
                                          variant.get("corpus_override", f["corpus"]))
                    # Unknown charges/joins and query aggregates have no bound
                    # contradiction in this API. The v3 migration ledger lists
                    # each former numeric-fallback assertion and its reason.
                    pending = {"wrong_query_day", "wrong_timezone", "extra_charge_under_ordinal",
                               "metadata_followed_by_business_number", "extra_charge_in_parenthesis", "unproved_extra_column",
                               "nonexistent_part", "extra_unsupported_row", "wrong_record_count", "wrong_total"}
                    expected = "review_required" if variant["name"] in pending else "incorrect"
                    self.assertEqual(result["status"], expected, result)
                    self.assertIsNot(result["fact_correct"], True)

    def test_query_metadata_always_requires_provenance_even_if_fixture_knows_it(self):
        for family in ("inline_query_metadata", "localized_query_time", "prose_entity_status_binding", "null_field_with_time"):
            for variant in self.families["v2_" + family]["positive_variants"]:
                with self.subTest(family=family, variant=variant["name"]):
                    f = self.families["v2_" + family]
                    result = common_score(f["scenario"], variant["visible"], f["corpus"])
                    self.assertEqual(result["status"], "review_required", result)
                    self.assertIn("unparsed_query_time_provenance", result["errors"])

    def test_numbered_lists_preserve_every_business_item(self):
        scenario, text, corpus = self.case("ordered_list")
        for marker in ("8. ", "8) ", "(8) ", "（8） ", "  8. "):
            with self.subTest(marker=marker):
                variant = text.replace("1. ", marker).replace("2. ", marker)
                self.assertEqual(common_score(scenario, variant, corpus)["status"], "correct")
                self.assertEqual(common_score(scenario, variant + "\n3. 加收款：61.25 USD", corpus)["status"], "review_required")
                self.assertEqual(common_score(scenario, variant + "\n3. 合同金额：18 USD", corpus)["status"], "incorrect")
                self.assertIsNot(common_score(scenario, variant + "\n(3) 数量：91 件", corpus)["fact_correct"], True)
        self.assertEqual(common_score(scenario, text + "\n3. 免收全部费用", corpus)["status"], "review_required")

    def test_parenthetical_prefix_suffix_and_nested_notes_keep_numeric_assertions(self):
        scenario, text, corpus = self.case("parenthetical_amount")
        for amount in ("（订单记录）328.40 USD", "328.40 USD（订单记录）", "328.40 USD（来源（订单记录））"):
            with self.subTest(amount=amount):
                variant = text.replace("328.40 USD（订单记录）", amount)
                self.assertEqual(common_score(scenario, variant, corpus)["status"], "review_required")
                self.assertEqual(common_score(scenario, variant.replace("328.40", "327.40"), corpus)["status"], "incorrect")
                self.assertEqual(common_score(scenario, variant.replace("订单记录", "另收61.25 USD"), corpus)["status"], "review_required")
        variant = text.replace("合同金额：328.40 USD（订单记录）", "合同金额（订单记录）：328.40 USD")
        self.assertEqual(common_score(scenario, variant, corpus)["status"], "review_required")

    def test_header_units_are_explicit_and_conflicts_are_preserved(self):
        scenario, text, corpus = self.case("contextual_parts_table")
        for header_unit in ("pcs", "件", "个"):
            with self.subTest(unit=header_unit):
                variant = text.replace("| 数量 |", f"| 数量（{header_unit}） |").replace("7 件", "7")
                self.assertEqual(common_score(scenario, variant, corpus)["status"], "correct")
                self.assertEqual(common_score(scenario, variant.replace("| 7 |", "| 8 |"), corpus)["status"], "incorrect")
                self.assertEqual(common_score(scenario, variant.replace("| 7 |", "| 7 USD |"), corpus)["status"], "incorrect")
                same = variant.replace("| 7 |", "| 7 pcs |")
                self.assertEqual(common_score(scenario, same, corpus)["status"], "correct")
        self.assertEqual(common_score(scenario, text.replace("| 数量 |", "| 数量（USD） |"), corpus)["status"], "incorrect")

    def test_generic_status_requires_source_identity_and_relationship_scope(self):
        scenario, text, corpus = self.case("contextual_parts_table")
        for change in ("missing_source", "other_order", "ambiguous_source", "unknown_column", "duplicate_rows"):
            with self.subTest(change=change):
                c, variant = deepcopy(corpus), text
                if change == "missing_source":
                    c = None
                elif change == "other_order":
                    c["records"]["svc_db"]["replacement_part"][0]["order_no"] = "SO-2029-Q99117"
                elif change == "ambiguous_source":
                    c["records"]["erp_db"]["order_item"][0]["part_name"] = "Hinge BX-88001-24"
                elif change == "unknown_column":
                    variant = text.replace("当前状态", "内部断言")
                else:
                    extra = deepcopy(c["records"]["svc_db"]["replacement_part"][0])
                    extra["quantity"] = 8
                    c["records"]["svc_db"]["replacement_part"].append(extra)
                self.assertIsNot(common_score(scenario, variant, c)["fact_correct"], True)

    def test_currency_prefix_and_suffix_agree_with_table_header_units(self):
        scenario, _, corpus = self.case("ordered_list")
        for amount in ("USD 328.40", "328.40 USD", "美元328.40", "328.40 US dollars", "USD 328.40美元"):
            with self.subTest(amount=amount):
                text = "订单 SO-2026-Z88001\n| 合同金额 (USD) | 订单状态 |\n|---:|---|\n| " + amount + " | 待发货 |"
                self.assertEqual(common_score(scenario, text, corpus)["status"], "correct")
                self.assertEqual(common_score(scenario, text.replace("(USD)", "(CNY)"), corpus)["status"], "incorrect")

    def test_multiple_allowed_orders_cannot_prove_associations_by_global_existence(self):
        scenario, text, corpus = self.case("source_linked_part")
        scenario["truth"]["allowed_entities"].append("SO-2026-Z88002")
        text += "\n订单 SO-2026-Z88002\n补件名称：Hinge BX-88002-24"
        for variant in (text, text.replace("BX-88001", "BX-TEMP").replace("BX-88002", "BX-88001").replace("BX-TEMP", "BX-88002")):
            result = common_score(scenario, variant, corpus)
            self.assertEqual(result["status"], "review_required", result)
            self.assertIn("unparsed_association_scope", result["errors"])

    def test_unfamiliar_ids_values_and_row_order_do_not_change_grammar(self):
        for f in self.families.values():
            # A metamorphic fixture independent of every original business ID,
            # monetary value, date and record order; no new production fixture.
            raw = json.dumps(f, ensure_ascii=False)
            for before, after in (("SO-2026-Z880", "SO-2029-Q991"), ("BX-880", "BX-991"),
                                  ("328.4", "564.2"), ("2028", "2032")):
                raw = raw.replace(before, after)
            changed = json.loads(raw)
            for tables in changed["corpus"]["records"].values():
                for rows in tables.values():
                    rows.reverse()
            for v in changed["positive_variants"]:
                with self.subTest(family=f["id"], variant=v["name"]):
                    result = common_score(changed["scenario"], v["visible"], changed["corpus"])
                    self.assertIn(result["status"], {"correct", "review_required"}, result)

    def test_query_and_date_reference_parsing_does_not_eat_extra_assertions(self):
        scenario, text, corpus = self.case("localized_query_time")
        for suffix in ("；另收61.25 USD", "。另收61.25 USD", "，另收61.25 USD", "\n另收61.25 USD"):
            with self.subTest(suffix=suffix):
                self.assertEqual(common_score(scenario, text + suffix, corpus)["status"], "review_required")
        for value in ("25:17", "08:71", "08:17:99", "08:17（UTC+15:00）"):
            with self.subTest(time=value):
                variant = text.replace("08:17（UTC）", value)
                self.assertEqual(common_score(scenario, variant, corpus)["status"], "incorrect")
        scenario, text, corpus = self.case("key_value_table", index=1)
        self.assertEqual(common_score(scenario, text + "\n另收61.25 USD", corpus)["status"], "review_required")
        self.assertEqual(common_score(scenario, text.replace("实际送达为4月9日", "实际送达为4月10日"), corpus)["status"], "incorrect")

    def test_shared_total_unit_requires_one_matching_source_row(self):
        scenario, text, corpus = self.case("record_count_and_total", index=1)
        self.assertEqual(common_score(scenario, text, corpus)["status"], "review_required")
        for cell, total in (("7.0", "7"), ("7", "7.00"), ("7.000", "7.0")):
            with self.subTest(cell=cell, total=total):
                variant = text.replace("| 7 |", "| " + cell + " |").replace("合计为7件", "合计为" + total + "件")
                result = common_score(scenario, variant, corpus)
                self.assertEqual(result["status"], "review_required", result)
                self.assertTrue(any(c["field"] == "quantity" and c["correct"] for c in result["claims"]))
        for change in ("wrong_total", "wrong_unit", "missing_unit", "no_source", "another_order"):
            with self.subTest(change=change):
                variant, c = text, deepcopy(corpus)
                if change == "wrong_total":
                    variant = text.replace("合计为7件", "合计为8件")
                elif change == "wrong_unit":
                    variant = text.replace("合计为7件", "合计为7 USD")
                elif change == "missing_unit":
                    variant = text.replace("合计为7件", "合计为7")
                elif change == "no_source":
                    c = None
                else:
                    variant += "\n订单 SO-2026-Z88002"
                self.assertIsNot(common_score(scenario, variant, c)["fact_correct"], True)

    def test_unreturned_null_field_is_not_inferred_from_wording(self):
        scenario, text, corpus = self.case("null_field_with_time")
        corpus["records"]["svc_db"]["delivery"][0].pop("delivered_date")
        result = common_score(scenario, text, corpus)
        self.assertEqual(result["status"], "review_required", result)
        self.assertIn("unparsed_or_ambiguous_claim:delivered_date", result["errors"])


if __name__ == "__main__":
    unittest.main()
