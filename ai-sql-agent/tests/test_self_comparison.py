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

    def test_exclusion_forms_are_caught_and_reported_as_written(self):
        # 恒为假的方向：一行都查不出来，同样不是按取值筛选
        self.assertEqual(
            find_self_comparisons("SELECT * FROM t WHERE order_no NOT IN (order_no)", "sqlite"),
            ["NOT order_no IN (order_no)"],
        )
        self.assertEqual(find_self_comparisons("SELECT * FROM t WHERE x != x", "sqlite"), ["x <> x"])
        self.assertEqual(
            find_self_comparisons("SELECT * FROM t WHERE order_no NOT IN ('J1')", "sqlite"), []
        )

    def test_unparseable_sql_is_left_to_the_executor(self):
        self.assertEqual(find_self_comparisons("not sql ((", "sqlite"), [])


class _OfflinePipeline:
    """演示库 + Mock 模型；不碰 pgvector，在临时目录里跑。"""

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


class DependentStepTest(_OfflinePipeline, unittest.TestCase):
    """
    c18 的形状离线重放：第 1 步 0 行（或没查成），第 2 步按计划要用第 1 步的工程号。
    生成器在第 2 步把筛选条件丢掉（「条件被吞掉」——不是字段自比较，那道检查拦不住），
    所以第 2 步能不能跑，只能看它依不依赖第 1 步。
    """

    FIRST = ("erp_db", "sales_order.order_no，sales_order.job_no",
             "在sales_order表中筛选order_no为'SO-NOPE'的订单，输出job_no", "sales_order.job_no")
    USES_IT = ("mes_db", "work_order.job_no，work_order.workshop",
               "先读取上一步结果中的job_no列表；再筛选这些job_no的工单；最后输出workshop",
               "work_order.workshop")
    EXCLUDES_IT = ("mes_db", "work_order.job_no，work_order.workshop",
                   "先读取上一步结果中的job_no列表；再排除这些job_no的工单；最后输出workshop",
                   "work_order.workshop")
    INDEPENDENT = ("mes_db", "work_order.job_no，work_order.workshop",
                   "在work_order表中筛选work_status为finished的工单，输出workshop", "work_order.workshop")

    def _plan(self, pipeline_self, prepared):
        steps = [
            CotPlanningStep(step_no=index, database=database, processing_objects=objects,
                            operation_instruction=instruction, output_target=target,
                            expected_cardinality="数条")
            for index, (database, objects, instruction, target) in enumerate(self.plan, start=1)
        ]
        return CotPlanningResult(user_query="q", prompt="", raw_output="", steps=steps)

    def _run(self, plan, first_sql="SELECT job_no FROM sales_order WHERE order_no = 'SO-NOPE';"):
        self.plan = plan
        generated = []

        def generate_sql(client_self, prompt):
            database = "mes_db" if "work_order" in prompt and "sales_order" not in prompt else "erp_db"
            generated.append(database)
            # 第 2 步手里没有工程号：把条件整个丢了
            return first_sql if database == "erp_db" else "SELECT job_no, workshop FROM work_order;"

        pipeline = Text2SQLPipeline()
        with mock.patch.object(Text2SQLPipeline, "_plan", lambda pipe, prepared: self._plan(pipe, prepared)), \
             mock.patch.object(CoderModelClient, "generate_sql", generate_sql):
            result = pipeline.run("SO-NOPE 这单的工单在哪个车间")
        return generated, result

    @staticmethod
    def _summary(result):
        """和 /query、A2A 同一套汇总：execution_status、failed_steps 都从这里出。"""
        from ai_sql_agent.api.result_summary import summarize_result
        from ai_sql_agent.memory import ConversationMemoryContext, ShortTermContext
        from ai_sql_agent.pipeline import RoutedSqlAgentResult
        from ai_sql_agent.pipeline.routing import RouteDecision, RouteType

        routed = RoutedSqlAgentResult(
            decision=RouteDecision(route=RouteType.DATABASE_QUERY, confidence=1.0, reason="test"),
            answer="",
            memory_context=ConversationMemoryContext(
                short_term=ShortTermContext(session_id="s", user_id="u")
            ),
            pipeline_result=result,
        )
        return summarize_result(routed)

    def test_step_using_an_empty_step_answers_no_rows_without_querying(self):
        generated, result = self._run([self.FIRST, self.USES_IT])
        self.assertNotIn("mes_db", generated)
        first, second = result.step_logs
        self.assertEqual(first.execution_result["row_count"], 0)
        # 只看那些记录、那些记录一条都没有：答案就是没有，不算出错（no_data，不是 partial_success）
        self.assertTrue(second.execution_result["success"])
        self.assertEqual(second.execution_result["rows"], [])
        self.assertIsNone(second.execution_result["error"])
        self.assertEqual(second.sql, "")
        self.assertIn("第 1 步查到 0 行", second.execution_result["held_reason"])
        # 查过了、答案是没有：客服据实说没有记录，而不是「部分结果」
        summary = self._summary(result)
        self.assertEqual(summary.execution_status, "no_data")
        self.assertEqual(summary.failed_steps, 0)
        # 审计里留着这一步的空位：模型不可用时不能只重放第 1 步、当成完整答案
        from ai_sql_agent.audit.store import _parse_steps

        self.assertEqual([item["sql"] for item in summary.audit_sqls][1:], [""])
        self.assertEqual(len(summary.audit_sqls), 2)
        self.assertEqual(_parse_steps(summary.selected_databases, summary.audit_sqls), [])
        # 返回给调用方的 sqls 不变：只列真的执行过的 SQL
        self.assertEqual(len(summary.sqls), 1)

    def test_exclusion_step_is_held_and_not_reported_as_empty(self):
        """「排除上一步那些工程号」，上一步 0 行时正确答案是全部工单：不能说成没有。"""
        generated, result = self._run([self.FIRST, self.EXCLUDES_IT])
        self.assertNotIn("mes_db", generated)
        second = result.step_logs[1]
        self.assertFalse(second.execution_result["success"])
        self.assertIn("全部记录", second.execution_result["error"])
        self.assertEqual(self._summary(result).execution_status, "partial_success")

    def test_unrecognised_or_annotating_wording_is_held_not_answered_empty(self):
        """
        说不清是不是「只取这些值」的，前一步 0 行时不能答「没有」：排除、标注的正确答案是
        其余全部。认不出来就按没执行算（partial_success）。
        """
        for instruction in (
            "先读取上一步结果中的job_no列表；再结合这些job_no处理工单；最后输出workshop",
            "列出全部工单，并标出是否属于上一步的job_no；最后输出workshop",
            "先读取上一步结果中的job_no列表；再过滤掉这些job_no的工单；最后输出workshop",
        ):
            step = (self.USES_IT[0], self.USES_IT[1], instruction, self.USES_IT[3])
            generated, result = self._run([self.FIRST, step])
            self.assertNotIn("mes_db", generated, instruction)
            second = result.step_logs[1]
            self.assertFalse(second.execution_result["success"], instruction)
            self.assertEqual(self._summary(result).execution_status, "partial_success", instruction)

    def test_step_using_a_failed_step_is_held(self):
        generated, result = self._run(
            [self.FIRST, self.USES_IT], first_sql="SELECT job_no FROM no_such_table;"
        )
        self.assertNotIn("mes_db", generated)
        first, second = result.step_logs
        self.assertFalse(first.execution_result["success"])
        self.assertFalse(second.execution_result["success"])
        self.assertIn("第 1 步没有查成", second.execution_result["error"])

    def test_held_step_clears_the_backfill_for_later_steps(self):
        """
        1 空 → 2 独立有行 → 3 依赖 1（拦下）→ 4 依赖 3、说法认不出来。第 4 步生成时不能看到
        第 2 步的取值被当成「上一步结果」：模型会照抄进 IN 列表，筛错单号。
        """
        plan = [
            self.FIRST,
            self.INDEPENDENT,
            ("mes_db", "work_order.job_no，work_order.workshop",
             "先把步骤1输出的job_no取值当作字面量列表，筛选job_no落在该列表内的工单，输出job_no",
             "work_order.job_no"),
            ("erp_db", "sales_order.job_no，sales_order.order_no",
             "用这些工程号找订单，输出order_no", "sales_order.order_no"),
        ]
        self.plan = plan
        prompts = []
        replies = iter([
            "SELECT job_no FROM sales_order WHERE order_no = 'SO-NOPE';",
            "SELECT job_no, workshop FROM work_order WHERE work_status = 'finished';",
            "SELECT order_no FROM sales_order;",
        ])

        def generate_sql(client_self, prompt):
            prompts.append(prompt)
            return next(replies)

        with mock.patch.object(Text2SQLPipeline, "_plan", lambda pipe, prepared: self._plan(pipe, prepared)), \
             mock.patch.object(CoderModelClient, "generate_sql", generate_sql):
            result = Text2SQLPipeline().run("SO-NOPE 这单的工单在哪个车间")

        self.assertEqual(len(prompts), 3)  # 第 3 步没生成
        self.assertTrue(result.step_logs[1].execution_result["rows"])  # 第 2 步有取值可回填
        self.assertNotIn("# 上一步结果", prompts[2])  # 第 4 步看不到它

    def test_independent_step_still_runs_after_an_empty_step(self):
        generated, result = self._run([self.FIRST, self.INDEPENDENT])
        self.assertEqual(generated.count("mes_db"), 1)
        second = result.step_logs[1]
        self.assertTrue(second.execution_result["success"])
        self.assertGreater(second.execution_result["row_count"], 0)
        self.assertEqual(self._summary(result).execution_status, "success")

class ReplayedHistoryIsGuardedTest(_OfflinePipeline, unittest.TestCase):
    """
    模型不可用时重放「同一问题上次成功的 SQL」。c18 那条 `IN (order_no)` 当时是执行成功的，
    就在历史里；修复之前存下的这种 SQL 重放时也要拦，拦下就整体放弃重放。
    """

    def _replay(self, sql):
        from ai_sql_agent.auth import authorization_scope
        from ai_sql_agent.retrieval.access import ALLOW_ALL

        pipeline = Text2SQLPipeline(sql_history_lookup=mock.Mock(return_value=[("mes_db", sql)]))
        # 和 run() 一样在授权作用域里重放：作用域外执行器一律拒绝，两条用例就都成了空结果
        with authorization_scope(ALLOW_ALL, user_id="u", trace_id="t", views=pipeline.view_catalog):
            return pipeline._replay_history(query="q", user_id="u", access=ALLOW_ALL)

    def test_poisoned_history_row_is_not_replayed(self):
        self.assertEqual(self._replay("SELECT job_no, workshop FROM work_order WHERE job_no IN (job_no)"), [])

    def test_ordinary_history_row_still_replays(self):
        steps = self._replay("SELECT job_no, workshop FROM work_order WHERE job_no = 'J24001'")
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].execution_result["success"])


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
