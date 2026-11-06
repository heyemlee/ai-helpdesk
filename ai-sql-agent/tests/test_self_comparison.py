"""
「字段和它自己比较」的筛选条件，和多步查询里上一步为空时的回填。

2026-11-06 客服问法那一轮的 c18：「J26-00024 现在什么进度了？」规划成 订单 → 生产 → 配送
三步。这张单刚签、生产库里没有，第 2 步返回 0 行；以前空结果的回填是空串，和「没有上一步」
一样，第 3 步手里没有单号，写出 `WHERE order_no IN (order_no)`，把配送表 936 行全查了出来。

两道防线：上一步没有交出取值，后面的步骤就不生成、不执行；执行前（首次、每次修复、
降级重放）拦下这种恒为真的条件，当成执行失败。
"""
import os
import tempfile
import unittest
from unittest import mock

from ai_sql_agent.core import reload_settings
from ai_sql_agent.execution.objects import MCPExecutionResult
from ai_sql_agent.generation.coder_client import CoderModelClient
from ai_sql_agent.generation.self_comparison import find_self_comparisons
from ai_sql_agent.pipeline import Text2SQLPipeline
from ai_sql_agent.planning.objects import CotPlanningResult, CotPlanningStep


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


class EmptyEarlierStepStopsTheChainTest(unittest.TestCase):
    """
    c18 的形状离线重放：两步计划，第 1 步 0 行，生成器在第 2 步把筛选条件丢掉
    （「条件被吞掉」——不是字段自比较，那道检查拦不住）。第 2 步必须不生成、不执行。
    """

    PLAN = [
        ("erp_db", "sales_order.order_no，sales_order.job_no",
         "在sales_order表中筛选order_no为'SO-NOPE'的订单，输出job_no", "sales_order.job_no"),
        ("mes_db", "work_order.job_no，work_order.workshop",
         "先读取上一步结果中的job_no列表；再筛选这些job_no的工单；最后输出workshop", "work_order.workshop"),
    ]
    STEP_SQL = {
        "erp_db": "SELECT job_no FROM sales_order WHERE order_no = 'SO-NOPE';",
        # 手里没有工程号：把条件整个丢了
        "mes_db": "SELECT job_no, workshop FROM work_order;",
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        self._env = mock.patch.dict(os.environ, {"AI_SQL_PGVECTOR_DSN": ""})
        self._env.start()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore)
        reload_settings()

    def _restore(self):
        os.chdir(self._cwd)
        self._env.stop()
        reload_settings()
        self._tmp.cleanup()

    def _plan(self, pipeline_self, prepared):
        steps = [
            CotPlanningStep(step_no=index, database=database, processing_objects=objects,
                            operation_instruction=instruction, output_target=target,
                            expected_cardinality="数条")
            for index, (database, objects, instruction, target) in enumerate(self.PLAN, start=1)
        ]
        return CotPlanningResult(user_query="q", prompt="", raw_output="", steps=steps)

    def test_second_step_is_not_generated_or_executed(self):
        generated = []

        def generate_sql(client_self, prompt):
            database = "mes_db" if "work_order" in prompt and "sales_order" not in prompt else "erp_db"
            generated.append(database)
            return self.STEP_SQL[database]

        pipeline = Text2SQLPipeline()
        with mock.patch.object(Text2SQLPipeline, "_plan", lambda pipe, prepared: self._plan(pipe, prepared)), \
             mock.patch.object(CoderModelClient, "generate_sql", generate_sql):
            result = pipeline.run("SO-NOPE 这单的工单在哪个车间")

        self.assertEqual(generated, ["erp_db"])
        first, second = result.step_logs
        self.assertTrue(first.execution_result["success"])
        self.assertEqual(first.execution_result["row_count"], 0)
        self.assertFalse(second.execution_result["success"])
        self.assertEqual(second.sql, "")
        self.assertEqual(second.execution_result["rows"], [])
        self.assertIn("第 1 步查到 0 行", second.execution_result["error"])


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
