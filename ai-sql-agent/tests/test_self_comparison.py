"""
「字段和它自己比较」的筛选条件，和多步查询里上一步为空时的回填。

2026-11-06 客服问法那一轮的 c18：「J26-00024 现在什么进度了？」规划成 订单 → 生产 → 配送
三步。这张单刚签、生产库里没有，第 2 步返回 0 行；以前空结果的回填是空串，和「没有上一步」
一样，第 3 步手里没有单号，写出 `WHERE order_no IN (order_no)`，把配送表 936 行全查了出来。

两道防线：回填明说「上一步 0 行」；执行前拦下这种恒为真的条件，当成执行失败交给修复。
"""
import unittest

from ai_sql_agent.execution.objects import MCPExecutionResult
from ai_sql_agent.generation.self_comparison import find_self_comparisons
from ai_sql_agent.pipeline import Text2SQLPipeline


class FindSelfComparisonsTest(unittest.TestCase):
    def test_the_c18_shape_is_caught(self):
        sql = "SELECT DISTINCT order_no, delivery_status FROM delivery WHERE order_no IN (order_no);"
        self.assertEqual(find_self_comparisons(sql, "sqlite"), ["order_no IN (order_no)"])

    def test_equality_with_itself_is_caught_case_insensitively(self):
        self.assertTrue(find_self_comparisons("SELECT * FROM t WHERE x = x", "sqlite"))
        self.assertTrue(find_self_comparisons("SELECT * FROM t WHERE T.X = t.x", "postgres"))

    def test_real_filters_and_joins_pass(self):
        for sql in (
            "SELECT a.x FROM a JOIN b ON a.order_no = b.order_no",
            "SELECT * FROM t WHERE order_no IN ('J26-00024', 'J26-00025')",
            "SELECT * FROM t WHERE order_no IN (SELECT order_no FROM u)",
            "SELECT * FROM t WHERE order_no = 'order_no'",
            # 一边带表名、一边不带：多表查询里分不清，不拦
            "SELECT * FROM t o JOIN u ON o.id = u.id WHERE o.x = x",
            "SELECT * FROM t WHERE 1 = 0",
        ):
            self.assertEqual(find_self_comparisons(sql, "sqlite"), [], sql)

    def test_unparseable_sql_is_left_to_the_executor(self):
        self.assertEqual(find_self_comparisons("not sql ((", "sqlite"), [])


class EmptyBackfillTest(unittest.TestCase):
    @staticmethod
    def _result(success, rows):
        class FakeResult:
            pass

        result = FakeResult()
        result.success, result.rows, result.columns = success, rows, ["order_no"]
        return result

    def test_successful_empty_step_says_so(self):
        text = Text2SQLPipeline._serialize_step_result(self._result(True, []))
        # 不能是空串：空串和「没有上一步」一样，下一步的 Prompt 里就没有这一节了
        self.assertIn("返回 0 行", text)
        self.assertIn("1 = 0", text)
        self.assertIn("order_no IN (order_no)", text)

    def test_failed_step_still_backfills_nothing(self):
        self.assertEqual(Text2SQLPipeline._serialize_step_result(self._result(False, [])), "")


class SelfComparisonIsNotExecutedTest(unittest.TestCase):
    class RecordingRouter:
        def __init__(self):
            self.requests = []

        def execute(self, request):
            self.requests.append(request)
            return MCPExecutionResult(
                database=request["database"], sql=request["sql"], success=True,
                columns=["order_no"], rows=[{"order_no": "J26-00024"}], row_count=1,
            )

    def setUp(self):
        # 只用到 mcp_router：不必建整条流水线
        self.pipeline = object.__new__(Text2SQLPipeline)
        self.pipeline.mcp_router = self.RecordingRouter()

    def test_self_comparison_becomes_a_visible_failure(self):
        result = self.pipeline._execute_unless_self_comparison(
            "svc_db", "SELECT order_no FROM delivery WHERE order_no IN (order_no)", "sqlite"
        )
        self.assertFalse(result.success)
        self.assertIn("恒为真", result.error)
        self.assertEqual(self.pipeline.mcp_router.requests, [])

    def test_ordinary_sql_is_executed_unchanged(self):
        sql = "SELECT order_no FROM delivery WHERE order_no IN ('J26-00024')"
        result = self.pipeline._execute_unless_self_comparison("svc_db", sql, "sqlite")
        self.assertTrue(result.success)
        self.assertEqual(self.pipeline.mcp_router.requests, [{"database": "svc_db", "sql": sql}])


if __name__ == "__main__":
    unittest.main()
