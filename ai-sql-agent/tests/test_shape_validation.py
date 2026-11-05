"""输出形状检查：静默错误的头号形态。

实测 21 条静默错误里，最大的一类是**形状不符**——问「台面类供应商的采购金额」，
模型答一个 `SUM(po_amt)`。数字本身算得没错，但用户以为看到的是每一单的金额、
拿到的是总额，而且全程没有任何迹象提示这一点。

两条检查都是静态的（SQL 结构 + 计划文本），不调模型、不看结果，成本接近零，
所以可以对每条查询都跑——这正是「覆盖不足」的解法：
语义自检准（误报率 1.7%）但贵且抓得少，静态检查便宜到可以全量铺开。
"""

import unittest

from ai_sql_agent.validation import (
    check_shape_against_plan,
    check_unrequested_aggregation,
    wants_aggregation,
)
from ai_sql_agent.validation.objects import AnomalyType
from ai_sql_agent.validation.shape import is_structurally_single_row

LATEST_SNAPSHOT_SQL = """
WITH latest_snapshot AS (
  SELECT ss.material_code, ss.on_hand_qty, ss.locked_qty
  FROM stock_snapshot ss
  WHERE ss.snapshot_date = (
    SELECT MAX(ss2.snapshot_date) FROM stock_snapshot ss2
    WHERE ss2.material_code = ss.material_code
  )
)
SELECT ls.material_code, (ls.on_hand_qty - ls.locked_qty) AS available_qty, m.safety_stock, m.unit
FROM latest_snapshot ls
JOIN material m ON ls.material_code = m.material_code
WHERE (ls.on_hand_qty - ls.locked_qty) < m.safety_stock;
"""
"""「每种物料取最新一张快照」的标准写法：全文有 MAX、没有 GROUP BY，但外层一行不少。"""


class UnrequestedAggregationTest(unittest.TestCase):
    def test_flags_sum_when_question_asks_for_detail(self):
        finding = check_unrequested_aggregation(
            "查询台面类供应商的采购金额",
            "SELECT SUM(p.po_amt) FROM purchase_order p JOIN supplier s ON ...",
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.AGGREGATION_UNREQUESTED)
        self.assertEqual(finding.severity, "warn", "汇总有时正是用户想要的，只提示不阻断")

    def test_silent_when_question_explicitly_asks_to_aggregate(self):
        self.assertIsNone(check_unrequested_aggregation(
            "统计每个供应商的采购单数量",
            "SELECT supplier_id, COUNT(*) FROM purchase_order GROUP BY supplier_id",
        ))

    def test_silent_on_plain_detail_query(self):
        self.assertIsNone(check_unrequested_aggregation(
            "查询准时率低于0.9的供应商名称",
            "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9",
        ))

    def test_grouped_aggregation_is_not_flagged(self):
        """
        按维度分组时模型多半在按用户提到的实体分组，那通常正是他要的。
        实测 10 条误报里 8 条是分组聚合——报它们只会把误报率顶过护栏线，
        而提示一旦被当噪音忽略，真正的静默错误也跟着漏过去。
        """
        self.assertIsNone(check_unrequested_aggregation(
            "查询板材类供应商的采购金额",
            "SELECT s.supplier_name, SUM(p.po_amt) FROM x GROUP BY s.supplier_name",
        ))

    def test_aggregation_intent_words(self):
        for query in ("统计订单总额", "平均准时率是多少", "各项目的采购额", "每家供应商的单量"):
            self.assertTrue(wants_aggregation(query), query)
        for query in ("查询超预算的项目名称", "台面类供应商的采购金额"):
            self.assertFalse(wants_aggregation(query), query)

    def test_english_aggregation_intent_words(self):
        for query in (
            "What are the purchase total, supplier payments and unpaid balance for PO-24-0103?",
            "How many work orders are in progress?",
            "Average on-time rate by supplier category",
            "What is the sum of payments for each purchase order?",
            "Which supplier has the most purchase orders?",
            "Number of projects over budget",
        ):
            self.assertTrue(wants_aggregation(query), query)

    def test_english_cues_match_whole_words_only(self):
        # summary / person / countertop 里藏着 sum / per / top；at least 是筛选条件不是「最少」
        for query in (
            "List the purchase amount for countertop suppliers",
            "Show the summary note for each person",  # each 才是这句里唯一的真信号
            "Which suppliers have an on-time rate of at least 0.9?",
            "Show purchase orders with at most 2 payments",
        ):
            expected = "each" in query
            self.assertEqual(wants_aggregation(query), expected, query)

    def test_english_question_asking_for_totals_is_not_flagged(self):
        sql = (
            "SELECT po.po_amt, (SELECT SUM(p.paid_amt) FROM payment p WHERE p.po_no = po.po_no) "
            "AS total_paid_amt FROM purchase_order po WHERE po.po_no = 'PO-24-0103'"
        )
        self.assertIsNone(check_unrequested_aggregation(
            "What are the purchase total, supplier payments and unpaid balance in USD for PO-24-0103?",
            sql,
        ))

    def test_english_detail_question_is_still_flagged(self):
        finding = check_unrequested_aggregation(
            "List the purchase amount for countertop suppliers",
            "SELECT SUM(p.po_amt) FROM purchase_order p JOIN supplier s ON s.supplier_id = p.supplier_id",
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.AGGREGATION_UNREQUESTED)


class ShapeAgainstPlanTest(unittest.TestCase):
    """
    与基数校验的区别：基数校验比「实际跑出多少行」，会被数据量影响；
    这里比「这条 SQL 结构上最多能出几行」，与数据无关，因此更确定。
    计划说要出数条、SQL 却是无分组聚合，那不是数据少，是形状写错了。
    """

    def test_flags_single_row_sql_when_plan_expects_many(self):
        finding = check_shape_against_plan("数条", "SELECT SUM(x) FROM t WHERE y > 1")
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.SHAPE_CONTRADICTS_PLAN)

    def test_silent_when_plan_expects_one_row(self):
        self.assertIsNone(check_shape_against_plan("单条", "SELECT SUM(x) FROM t"))

    def test_silent_when_sql_groups(self):
        self.assertIsNone(
            check_shape_against_plan("数条", "SELECT a, SUM(x) FROM t GROUP BY a")
        )

    def test_silent_without_a_plan_estimate(self):
        self.assertIsNone(check_shape_against_plan("", "SELECT SUM(x) FROM t"))

    def test_aggregate_inside_a_filter_subquery_does_not_collapse_the_result(self):
        """
        这条 SQL 在练习库上返回 8 行。按全文搜聚合函数时它被报成
        「结构上只能返回 1 行」——提示和它旁边的结果表自相矛盾。
        """
        self.assertFalse(is_structurally_single_row(LATEST_SNAPSHOT_SQL))
        self.assertIsNone(check_shape_against_plan("数条", LATEST_SNAPSHOT_SQL))
        self.assertIsNone(check_unrequested_aggregation("查询库存低于安全库存的物料", LATEST_SNAPSHOT_SQL))


class OuterQueryShapeTest(unittest.TestCase):
    """「结构上只能返回一行」只由最外层查询决定。"""

    def test_scalar_subquery_in_the_select_list_keeps_every_row(self):
        self.assertFalse(is_structurally_single_row(
            "SELECT po.po_no, (SELECT SUM(p.paid_amt) FROM payment p WHERE p.po_no = po.po_no) AS paid "
            "FROM purchase_order po"
        ))

    def test_window_functions_do_not_collapse_rows(self):
        self.assertFalse(is_structurally_single_row(
            "SELECT po_no, SUM(po_amt) OVER (PARTITION BY supplier_id) AS supplier_total FROM purchase_order"
        ))

    def test_row_wise_max_is_not_an_aggregate(self):
        # SQLite 的 MAX(a, b) 是逐行取大的标量函数
        self.assertFalse(is_structurally_single_row(
            "SELECT project_code, MAX(actual_cost - budget_amt, 0) AS over_budget FROM project"
        ))

    def test_outer_aggregate_collapses_even_when_a_subquery_groups(self):
        # 按全文搜 GROUP BY 会放过这一条；外层确实只出一行
        sql = "SELECT COUNT(*) FROM (SELECT supplier_id FROM purchase_order GROUP BY supplier_id) AS s"
        self.assertTrue(is_structurally_single_row(sql))
        self.assertIsNotNone(check_shape_against_plan("数条", sql))

    def test_passing_a_single_row_source_through_is_still_one_row(self):
        self.assertTrue(is_structurally_single_row(
            "WITH total AS (SELECT SUM(po_amt) AS amount FROM purchase_order) SELECT amount FROM total"
        ))
        self.assertTrue(is_structurally_single_row(
            "SELECT t.amount FROM (SELECT SUM(po_amt) AS amount FROM purchase_order) AS t"
        ))

    def test_joining_a_single_row_source_to_a_table_is_not_one_row(self):
        self.assertFalse(is_structurally_single_row(
            "WITH total AS (SELECT SUM(po_amt) AS amount FROM purchase_order) "
            "SELECT po.po_no, po.po_amt / total.amount AS share FROM purchase_order po JOIN total ON 1 = 1"
        ))

    def test_set_operations_are_not_one_row(self):
        self.assertFalse(is_structurally_single_row(
            "SELECT MAX(po_amt) FROM purchase_order UNION ALL SELECT MIN(po_amt) FROM purchase_order"
        ))

    def test_having_without_group_by_is_still_one_group(self):
        self.assertTrue(is_structurally_single_row(
            "SELECT 'many' AS verdict FROM purchase_order HAVING COUNT(*) > 10"
        ))

    def test_unparseable_sql_falls_back_to_the_text_check(self):
        # 解析失败不能把提示悄悄关掉：照旧按「全文有聚合、没有 GROUP BY」判断
        self.assertTrue(is_structurally_single_row("SELECT SUM(x) FROM t JOIN u ON ..."))
        self.assertFalse(is_structurally_single_row("SELECT a, SUM(x) FROM t JOIN u ON ... GROUP BY a"))


class IntermediateSelfCheckTest(unittest.TestCase):
    """中间步骤要拿**该步骤的目标**比对，不能拿用户原问题。"""

    class Recorder:
        def __init__(self):
            self.prompts = []

        def generate(self, prompt):
            self.prompts.append(prompt)
            return "复述：筛选了超预算项目" if "复述" in prompt and "结论" not in prompt \
                else "结论：一致\n理由：符合"

    def test_intermediate_step_is_compared_against_its_own_objective(self):
        from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker

        client = self.Recorder()
        ResultValidator(SemanticSelfChecker(client)).validate(
            query="查询超预算项目的采购总金额",
            sql="SELECT project_code FROM project WHERE actual_cost > budget_amt",
            row_count=3,
            is_final_step=False,
            step_objective="筛选超预算项目并输出 project_code（输出：project_code）",
        )
        compare = client.prompts[-1]
        self.assertIn("project_code", compare)
        self.assertNotIn("采购总金额", compare, "中间步骤不该拿整个问题去比")

    def test_intermediate_check_is_skipped_when_objective_missing(self):
        """开关关掉时传空目标，自检应当整个跳过，而不是退回用原问题比。"""
        from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker

        client = self.Recorder()
        ResultValidator(SemanticSelfChecker(client)).validate(
            query="查询超预算项目的采购总金额",
            sql="SELECT project_code FROM project",
            row_count=3,
            is_final_step=False,
            step_objective="",
        )
        self.assertEqual(client.prompts, [])
