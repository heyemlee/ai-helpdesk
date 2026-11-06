"""四项落地指标的口径锁定测试。

指标定义一旦漂移，历史数字就不可比，「优化有没有效」也就无从判断。
这里把最容易被悄悄改坏的一条——**静默错误的判定**——用最小构造锁住。

静默错误 = 答案是错的 且 系统给出了一个看起来正常的答案 且 没有任何风险提示。
执行失败、降级答复、软异常告警都不算：用户看得见异常，会自己去核对。
"""

import json
import unittest
from pathlib import Path

from benchmark.runner import BenchmarkItemResult, BenchmarkReport

THRESHOLDS = Path(__file__).resolve().parents[1] / "benchmark" / "thresholds.json"


def _item(item_id, *, correct, answered=True, flagged=False, degraded=False):
    return BenchmarkItemResult(
        item_id=item_id,
        category="single_table",
        query="q",
        correct=correct,
        answered=answered,
        flagged=flagged,
        degraded=degraded,
    )


class SilentErrorDefinitionTest(unittest.TestCase):
    def test_wrong_answer_without_any_signal_is_silent(self):
        self.assertTrue(_item("a", correct=False).silent_error)

    def test_wrong_answer_with_validation_finding_is_not_silent(self):
        """软异常告警已经把风险摆到用户面前，不属于静默。"""
        self.assertFalse(_item("a", correct=False, flagged=True).silent_error)

    def test_execution_failure_is_not_silent(self):
        """SQL 跑不通时用户看到的是报错，不是一个假答案。"""
        self.assertFalse(_item("a", correct=False, answered=False).silent_error)

    def test_degraded_reply_is_not_silent(self):
        """降级答复会明确告诉用户发生了什么。"""
        self.assertFalse(
            _item("a", correct=False, answered=False, degraded=True).silent_error
        )

    def test_correct_answer_is_never_silent(self):
        self.assertFalse(_item("a", correct=True).silent_error)


class RateComputationTest(unittest.TestCase):
    def test_silent_error_rate_denominator_is_all_items(self):
        """
        分母是全部条目，不是错误条目。

        用户关心「问 100 次会被悄悄骗几次」，用错误条目做分母会随准确率提升
        而虚高，两个方向的改动混在一个数里，看不出来是哪边动的。
        """
        report = BenchmarkReport(total=4, correct=2, total_columns=86)
        report.items = [
            _item("a", correct=True),
            _item("b", correct=True),
            _item("c", correct=False),                 # 静默
            _item("d", correct=False, answered=False),  # 显性失败
        ]
        self.assertAlmostEqual(report.silent_error_rate, 0.25)

    def test_false_alarm_rate_counts_only_correct_answers(self):
        """误报率的分母是正确条目：只有「对了还被标可疑」才叫打扰用户。"""
        report = BenchmarkReport(total=4, correct=2, total_columns=86)
        report.items = [
            _item("a", correct=True, flagged=True),
            _item("b", correct=True),
            _item("c", correct=False, flagged=True),
            _item("d", correct=False),
        ]
        self.assertAlmostEqual(report.false_alarm_rate, 0.5)

    def test_flagging_everything_cannot_game_the_gate(self):
        """
        护栏有效性：把每条都标成可疑，静默错误率归零，但误报率会顶到 100%，
        门禁照样拦。这是两个指标必须成对存在的理由。
        """
        report = BenchmarkReport(total=2, correct=1, total_columns=86)
        report.items = [
            _item("a", correct=True, flagged=True),
            _item("b", correct=False, flagged=True),
        ]
        self.assertEqual(report.silent_error_rate, 0.0)
        self.assertEqual(report.false_alarm_rate, 1.0)


class OfflineSmokeHasNoFalseAlarmsTest(unittest.TestCase):
    """
    CI 的发布门禁跑的就是这 4 条冒烟样本，且没有模型凭证（走 Mock）。

    2026-10-14 那次：读不出结论的自检改记为「没做成」之后，Mock 的回答里从来没有
    「结论：」，4 条正确答案全被打上 self_check_not_run，误报率 100%，门禁在推送后
    才拦下来。这里在单测阶段就锁住。
    """

    def test_correct_offline_answers_carry_no_risk_flag(self):
        from ai_sql_agent.core import get_settings
        from benchmark.gate import SMOKE_IDS
        from benchmark.runner import BenchmarkRunner

        if get_settings().has_model_credentials:
            self.skipTest("配了模型凭证就不是离线冒烟")
        report = BenchmarkRunner().run(ids=SMOKE_IDS)
        reasons = {item.item_id: item.flag_reasons for item in report.items if item.flagged}
        self.assertEqual(report.false_alarm_rate, 0.0, reasons)
        for item in report.items:
            self.assertNotIn("self_check_not_run", item.flag_reasons, item.item_id)


class ThresholdsFileTest(unittest.TestCase):
    """门禁阈值文件的结构与方向，改错方向等于门禁失效。"""

    HIGHER_IS_BETTER = {"column_recall", "holdout_recall", "single_db", "cross_db"}
    LOWER_IS_BETTER = {"silent_error_rate", "false_alarm_rate", "avg_column_ratio", "recall_gap"}

    def setUp(self):
        self.config = json.loads(THRESHOLDS.read_text(encoding="utf-8"))

    def test_both_modes_cover_all_metrics(self):
        expected = self.HIGHER_IS_BETTER | self.LOWER_IS_BETTER
        for mode in ("full", "smoke"):
            self.assertEqual(set(self.config[mode]), expected, f"{mode} 缺指标")

    def test_direction_is_not_inverted(self):
        for mode in ("full", "smoke"):
            for metric, rule in self.config[mode].items():
                if metric in self.HIGHER_IS_BETTER:
                    self.assertIn("min", rule, f"{mode}.{metric} 应该卡下限")
                else:
                    self.assertIn("max", rule, f"{mode}.{metric} 应该卡上限")

    def test_full_thresholds_match_documented_line(self):
        """
        合理线写死在测试里：调阈值必须同时改这里，
        避免优化跑不到目标时顺手把线拉低，让门禁失去意义。
        """
        full = self.config["full"]
        self.assertEqual(full["column_recall"]["min"], 0.95)
        self.assertEqual(full["holdout_recall"]["min"], 0.85)
        self.assertEqual(full["single_db"]["min"], 0.85)
        self.assertEqual(full["cross_db"]["min"], 0.90)
        self.assertEqual(full["silent_error_rate"]["max"], 0.03)
        self.assertEqual(full["false_alarm_rate"]["max"], 0.20)
        self.assertEqual(full["avg_column_ratio"]["max"], 0.40)
        self.assertEqual(full["recall_gap"]["max"], 0.03)


if __name__ == "__main__":
    unittest.main()


class PromptVisibilityTest(unittest.TestCase):
    """
    字段召回率必须按「进 Prompt」算，不是按「被检索到」算。

    Schema 图受 Token 预算裁剪，超出预算的部分直接被截掉。
    检索命中但被裁掉的字段，模型压根看不到，对下游等于不存在——
    按检索输出算召回率会把这类字段算成命中，指标越高越危险，
    因为它掩盖的恰恰是「模型没拿到字段」这个真实失败原因。
    """

    SAMPLE = (
        "数据库：pm_db\n\n表名：project\n字段：\n"
        "- 字段名：project_code\n  数据类型：TEXT\n"
        "- 字段名：budget_amt\n  数据类型：REAL\n"
        "数据库：mes_db\n\n表名：work_order\n字段：\n"
        "- 字段名：job_no\n  数据类型：TEXT\n"
    )

    def test_parses_columns_out_of_rendered_context(self):
        from ai_sql_agent.pipeline.token_budget import columns_in_prompt

        self.assertEqual(
            columns_in_prompt(self.SAMPLE),
            ["pm_db.project.project_code", "pm_db.project.budget_amt",
             "mes_db.work_order.job_no"],
        )

    def test_truncated_tail_line_is_not_counted(self):
        """预算截断会把最后一行切成半截，半个字段名不能算命中。"""
        from ai_sql_agent.pipeline.token_budget import columns_in_prompt

        truncated = self.SAMPLE[: self.SAMPLE.index("- 字段名：job_no") + 12]
        self.assertNotIn("mes_db.work_order.job_no", columns_in_prompt(truncated))

    def test_column_recall_uses_prompt_columns_not_retrieved(self):
        from benchmark.runner import BenchmarkRunner

        class FakeResult:
            retrieved_columns = ["pm_db.project.budget_amt", "pm_db.project.actual_cost"]
            prompt_columns = ["pm_db.project.budget_amt"]  # actual_cost 被预算裁掉

        item = {"gold_columns": ["pm_db.project.budget_amt", "pm_db.project.actual_cost"]}
        in_prompt, retrieved, count = BenchmarkRunner._score_recall(item, FakeResult())
        self.assertFalse(in_prompt, "被裁掉的字段不能算命中")
        self.assertTrue(retrieved, "检索口径应该仍然算命中，用于定位瓶颈")
        self.assertEqual(count, 1)


class SchemaBudgetAllocationTest(unittest.TestCase):
    """
    Schema 预算必须按库分配，不能把拼好的整段做头部截断。

    整段截断等于优先把最后一个库整段砍掉，而跨库查询恰恰依赖第二个库。
    实测后果：规划模型写出「Schema 未提供该字段及完整表结构」，
    再生成 `SELECT job_no, NULL AS current_production_process`。
    """

    def setUp(self):
        from ai_sql_agent.pipeline.token_budget import PromptBudgetConfig, PromptBudgetManager

        self.manager = PromptBudgetManager(PromptBudgetConfig(schema_budget=60))

    @staticmethod
    def _section(tag, lines):
        return "\n".join(f"{tag}-line-{i}" for i in range(lines))

    def test_every_database_keeps_representation_when_over_budget(self):
        big = self._section("dbA", 40)
        small = self._section("dbB", 40)
        out = self.manager.fit_schema_sections([big, small])
        self.assertIn("dbA", out)
        self.assertIn("dbB", out, "第二个库被整段砍掉了——跨库查询必错")

    def test_single_database_still_gets_the_whole_budget(self):
        only = self._section("dbA", 40)
        out = self.manager.fit_schema_sections([only])
        self.assertGreater(len(out), len(self.manager.fit_schema_sections([only, only])))

    def test_noise_database_does_not_starve_the_main_one(self):
        """
        单库查询里第二个库常是检索捎带进来的噪声库。均分预算等于把主库砍掉一半
        去养一个用不上的库——实测单库准确率 100%→88%、进 Prompt 召回 99.3%→92.7%。
        按需求比例分配后，主库应当拿到远超一半的额度。
        """
        main = self._section("dbA", 200)
        noise = self._section("dbB", 200)
        out = self.manager.fit_schema_sections([main, noise])
        kept_main = out.count("dbA-line")
        kept_noise = out.count("dbB-line")
        self.assertGreater(kept_noise, 0, "第二个库仍然不能被整段砍掉")
        # 体量相同则接近对半；真正要防的是主库被砍到不足以回答问题
        self.assertGreaterEqual(kept_main, kept_noise)

    def test_budget_follows_need_not_headcount(self):
        """小库只占它需要的那点，剩下全给大库——而不是一人一半。"""
        big = self._section("dbA", 200)
        small = "dbB-line-0"
        out = self.manager.fit_schema_sections([big, small])
        self.assertIn(small, out, "小库应当被完整保留")
        # 均分会让两边行数相当；按需分配下大库应当占绝大多数
        self.assertGreater(out.count("dbA-line"), 10 * out.count("dbB-line"))

    def test_small_section_releases_slack_to_the_large_one(self):
        """用不满配额的库要把余量让出来，而不是各占一半。"""
        tiny = "dbB-only-line"
        big = self._section("dbA", 60)
        out = self.manager.fit_schema_sections([big, tiny])
        self.assertIn(tiny, out)
        self.assertGreater(out.count("dbA-line"), 10)

    def test_under_budget_input_is_untouched(self):
        parts = ["dbA-line-0", "dbB-line-0"]
        self.assertEqual(self.manager.fit_schema_sections(parts), "dbA-line-0\n\ndbB-line-0")

    def test_empty_sections_are_dropped(self):
        self.assertEqual(self.manager.fit_schema_sections(["", "  ", "dbA"]), "dbA")


class ResultMatchingTest(unittest.TestCase):
    """
    判定口径 C：允许模型顺带多输出一列，但不允许 SELECT * 通吃。

    严格相等口径会把语义完全正确的答案判错——问「采购总金额超过 5 万的工程号」，
    模型答 `J24001 | 636000`，工程号、筛选、分组、行数全对，只是把金额也显示了。
    更糟的是这条 SQL 跑通、没有告警，于是被计进**静默错误率**：
    把「答对了」算成「悄悄答错」，是纯粹的度量污染。
    """

    GOLD = [{"job_no": "J24001"}, {"job_no": "J24002"}]

    def _m(self, predicted):
        from benchmark.runner import result_matches
        return result_matches(self.GOLD, predicted)

    def test_exact_match_still_passes(self):
        self.assertTrue(self._m([{"job_no": "J24002"}, {"job_no": "J24001"}]))

    def test_one_extra_column_is_accepted(self):
        self.assertTrue(
            self._m([{"job_no": "J24001", "总额": 636000.0},
                     {"job_no": "J24002", "总额": 140000.0}])
        )

    def test_two_extra_columns_are_rejected(self):
        """留一列的余量对应「顺带显示聚合值」，再多就是 SELECT * 通吃。"""
        self.assertFalse(
            self._m([{"job_no": "J24001", "a": 1, "b": 2},
                     {"job_no": "J24002", "a": 3, "b": 4}])
        )

    def test_row_count_must_match(self):
        self.assertFalse(self._m([{"job_no": "J24001"}]))

    def test_wrong_value_still_fails(self):
        self.assertFalse(self._m([{"job_no": "J24001"}, {"job_no": "J99999"}]))

    def test_duplicate_values_use_multiset_semantics(self):
        """gold 一行是 (5,5)、模型是 (5,9) 时，集合口径会误判命中。"""
        from benchmark.runner import result_matches
        self.assertFalse(result_matches([{"a": 5, "b": 5}], [{"a": 5, "b": 9}]))

    def test_float_tolerance_is_preserved(self):
        from benchmark.runner import result_matches
        self.assertTrue(
            result_matches([{"x": 1.0000000001}], [{"x": 1.0}])
        )


class MismatchExplanationTest(unittest.TestCase):
    """
    答错时错在哪。**只用来看错因，不改判分**：判分仍然只看 result_matches。

    要分开的是「多输出了列」和「取值不对」——前者取值全对，调用方照着说不会说错；
    后者才是会被当成事实转述出去的那种。混在一个数里，看不出该修哪一头。
    """

    GOLD = [{"order_status": "配送中"}]

    def _why(self, gold, predicted):
        from benchmark.runner import explain_mismatch, result_matches

        reason = explain_mismatch(gold, predicted)
        # 解释和判分不许打架：答对就没有错因，答错就一定有
        self.assertEqual(reason == "", result_matches(gold, predicted))
        return reason

    def test_correct_answer_has_no_reason(self):
        self.assertEqual(self._why(self.GOLD, [{"order_no": "J26-00084", "order_status": "配送中"}]), "")

    def test_right_values_with_too_many_columns(self):
        predicted = [{"order_no": "J26-00084", "order_status": "配送中", "delivery_date": "2026-11-09"}]
        self.assertEqual(self._why(self.GOLD, predicted), "extra_columns")

    def test_wrong_value_is_not_mistaken_for_shape(self):
        self.assertEqual(self._why(self.GOLD, [{"order_status": "生产中"}]), "wrong_values")

    def test_wrong_value_with_extra_columns_is_still_wrong_values(self):
        predicted = [{"order_no": "J26-00084", "order_status": "生产中", "delivery_date": "2026-11-09"}]
        self.assertEqual(self._why(self.GOLD, predicted), "wrong_values")

    def test_empty_and_row_count(self):
        self.assertEqual(self._why(self.GOLD, []), "empty")
        self.assertEqual(
            self._why(self.GOLD, [{"order_status": "配送中"}, {"order_status": "配送中"}]), "row_count"
        )

    def test_every_reason_has_a_label(self):
        from benchmark.runner import MISMATCH_LABELS

        self.assertEqual(
            set(MISMATCH_LABELS),
            {"empty", "row_count", "wrong_values", "extra_columns", "matched_unusable"},
        )


class CustomerGroupStaysOutOfGateTest(unittest.TestCase):
    """客服转来的问法单独一组：不并进 single_db，门禁的线一条都不碰它。"""

    @staticmethod
    def _report(categories):
        report = BenchmarkReport(total=0, correct=0)
        for category, (total, correct) in categories.items():
            report.category_totals[category] = total
            report.category_correct[category] = correct
            report.total += total
            report.correct += correct
        return report

    def test_customer_is_its_own_group(self):
        from benchmark.runner import CUSTOMER_CATEGORY

        gate_only = self._report({"single_table": (10, 9), "cross_db": (5, 4)})
        mixed = self._report({"single_table": (10, 9), "cross_db": (5, 4), CUSTOMER_CATEGORY: (24, 3)})
        grouped = mixed.grouped_accuracy()
        self.assertEqual(grouped["single_db"], gate_only.grouped_accuracy()["single_db"])
        self.assertEqual(grouped["cross_db"], gate_only.grouped_accuracy()["cross_db"])
        self.assertEqual(grouped[CUSTOMER_CATEGORY], 3 / 24)

    def test_customer_alone_does_not_report_a_single_db_number(self):
        from benchmark.runner import CUSTOMER_CATEGORY

        grouped = self._report({CUSTOMER_CATEGORY: (24, 20)}).grouped_accuracy()
        self.assertEqual(list(grouped), [CUSTOMER_CATEGORY])

    def test_gate_has_no_customer_line(self):
        from benchmark.runner import CUSTOMER_CATEGORY

        config = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
        for mode in ("full", "smoke"):
            self.assertNotIn(CUSTOMER_CATEGORY, config[mode])
        # 门禁跑的评测集里也不许混进这一类
        dataset = json.loads((THRESHOLDS.parent / "dataset.json").read_text(encoding="utf-8"))
        self.assertNotIn(CUSTOMER_CATEGORY, {item["category"] for item in dataset})
