"""跨库链路的四项改动，离线锁定。

跨库准确率是四项落地指标里缺口最大的一项（54% vs 合理线 70%）。
失败样本归纳出的根因不是「模型不会写 SQL」，而是四个可以在工程侧堵住的漏洞：

1. 规划阶段只有文字规则、没有走通的范例 → 两个库压进一步，或第一步不输出关联键；
2. 步骤声明的库里根本没有它处理对象里的表 → SQL 必错，但只重生成 SQL 治标不治本；
3. 上一步结果以一段 JSON 交给模型 → 它照样去写跨库子查询；
4. 最后一步只做后半段，语义自检拿原问题去比 → 系统性误报。
"""

import unittest

from ai_sql_agent.pipeline import Text2SQLPipeline
from ai_sql_agent.pipeline.text2sql_pipeline import _as_sql_literals
from ai_sql_agent.planning import CotPlanningRequest, CotPromptBuilder, few_shots_for
from ai_sql_agent.planning.objects import CotPlanningResult, CotPlanningStep
from ai_sql_agent.validation.self_check import SemanticSelfChecker


def _plan(*steps):
    return CotPlanningResult(
        user_query="",
        prompt="",
        raw_output="",
        steps=[
            CotPlanningStep(
                step_no=index,
                database=database,
                processing_objects=objects,
                operation_instruction="",
                output_target=output,
            )
            for index, (database, objects, output) in enumerate(steps, start=1)
        ],
    )


class CrossDbFewShotTest(unittest.TestCase):
    def test_example_only_injected_when_multiple_databases_hit(self):
        """单库问题看到跨库范例会倾向于多拆一步，白花一次调用还多一次出错机会。"""
        self.assertEqual(few_shots_for(1), [])
        self.assertEqual(len(few_shots_for(2)), 1)

    def test_example_demonstrates_key_passing_not_just_rules(self):
        """范例必须真的走通一遍：第一步输出关联键，第二步用字面量。"""
        example = few_shots_for(2)[0]
        self.assertIn("步骤1", example.output)
        self.assertIn("步骤2", example.output)
        self.assertIn("project_code", example.output)
        self.assertIn("字面量", example.output)

    def test_prompt_asks_for_expected_cardinality(self):
        """
        输出格式规范必须包含「预期输出量级」。

        此前不包含，模型自然不会给，基数校验拿到的永远是空字符串——
        一整类「join 扇出导致重复计数」的静默错误因此从来没被检出过。
        """
        prompt = CotPromptBuilder().build(
            CotPlanningRequest(user_query="Q", schema_context="S")
        )
        self.assertIn("预期输出量级", prompt)


class PlanDatabaseConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Text2SQLPipeline()

    def test_consistent_plan_has_no_conflict(self):
        plan = _plan(("pm_db", "project表（字段：project_code, budget_amt）", "project_code"))
        self.assertEqual(self.pipeline._plan_database_conflicts(plan), [])

    def test_foreign_table_in_processing_objects_is_flagged(self):
        plan = _plan(("pm_db", "project表, purchase_order表（字段：po_amt）", "总金额"))
        conflicts = self.pipeline._plan_database_conflicts(plan)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("purchase_order", conflicts[0])
        self.assertIn("erp_db", conflicts[0])

    def test_unknown_database_is_flagged(self):
        plan = _plan(("crm_db", "project表", "x"))
        self.assertIn("不存在", self.pipeline._plan_database_conflicts(plan)[0])

    def test_substring_table_names_do_not_false_positive(self):
        """work_order 里含 order，不能因此被判成引用了别的库的表。"""
        plan = _plan(("mes_db", "work_order表（字段：job_no, work_status）", "job_no"))
        self.assertEqual(self.pipeline._plan_database_conflicts(plan), [])

    def test_two_step_cross_db_plan_is_clean(self):
        plan = _plan(
            ("pm_db", "project表（字段：project_code, actual_cost, budget_amt）", "project_code"),
            ("erp_db", "purchase_order表（字段：job_no, po_amt）", "采购总金额"),
        )
        self.assertEqual(self.pipeline._plan_database_conflicts(plan), [])


class BackfillLiteralTest(unittest.TestCase):
    def test_strings_are_quoted_and_escaped(self):
        self.assertEqual(
            _as_sql_literals(["P-001", "O'Brien"]), ["'P-001'", "'O''Brien'"]
        )

    def test_numbers_are_not_quoted(self):
        self.assertEqual(_as_sql_literals([1, 2.5]), ["1", "2.5"])

    def test_duplicates_removed_and_order_kept(self):
        self.assertEqual(_as_sql_literals(["a", "b", "a"]), ["'a'", "'b'"])

    def test_none_is_skipped(self):
        """IN (NULL) 永远不匹配，放进去只会把条件写死成空结果。"""
        self.assertEqual(_as_sql_literals([None, "a"]), ["'a'"])

    def test_serialized_result_contains_ready_to_paste_in_clause(self):
        class FakeResult:
            success = True
            columns = ["project_code"]
            rows = [{"project_code": "P1"}, {"project_code": "P2"}]

        text = Text2SQLPipeline._serialize_step_result(FakeResult())
        self.assertIn("project_code IN ('P1', 'P2')", text)
        self.assertIn("完整结果（JSON）", text)


class MultiStepSelfCheckTest(unittest.TestCase):
    """最后一步只做后半段，比对时必须告诉审阅者前面已经做完了什么。"""

    class RecordingClient:
        def __init__(self):
            self.prompts = []

        def generate(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if "复述" in prompt and "结论" not in prompt:
                return "复述：统计了指定编号工程的采购金额合计"
            return "结论：一致\n理由：符合"

    def test_preceding_summary_switches_to_multi_step_prompt(self):
        client = self.RecordingClient()
        finding, _ = SemanticSelfChecker(client).check(
            "查询实际成本超过预算的项目的采购总金额",
            "SELECT SUM(po_amt) FROM purchase_order WHERE job_no IN ('P1')",
            preceding_summary="步骤1（在 pm_db）：筛选超预算项目，输出 project_code",
        )
        self.assertIsNone(finding)
        compare_prompt = client.prompts[-1]
        self.assertIn("前置步骤已完成", compare_prompt)
        self.assertIn("不是遗漏", compare_prompt)

    def test_single_step_keeps_the_plain_prompt(self):
        client = self.RecordingClient()
        SemanticSelfChecker(client).check("问题", "SELECT 1")
        self.assertNotIn("前置步骤已完成", client.prompts[-1])

    def test_preceding_summary_is_built_from_earlier_steps_only(self):
        plan = _plan(
            ("pm_db", "project表", "project_code"),
            ("erp_db", "purchase_order表", "采购总金额"),
        )
        first = Text2SQLPipeline._preceding_summary(plan.steps, 1)
        second = Text2SQLPipeline._preceding_summary(plan.steps, 2)
        self.assertEqual(first, "")
        self.assertIn("pm_db", second)
        self.assertNotIn("erp_db", second)


if __name__ == "__main__":
    unittest.main()
