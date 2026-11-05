import tempfile
import unittest

from ai_sql_agent.audit import AuditRecord, SQLiteAuditStore
from ai_sql_agent.auth import DataPolicy
from ai_sql_agent.core.errors import ModelServerError, ModelTimeoutError
from ai_sql_agent.pipeline import Text2SQLPipeline

QUERY = "查询准时率低于0.9的供应商名称"
CACHED_SQL = (
    "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9"
)


def break_planner(pipeline, error=None):
    """
    让规划阶段的模型调用失败。

    同时关闭短路径：短路径直接按检索结果构造单步计划、不调规划模型，
    留着它会让「规划阶段失败」这个前提不成立（简单查询根本不经过规划）。
    真实场景下模型不可用时，短路径会在 SQL 生成阶段触发降级。
    """
    error = error or ModelServerError("模型服务错误：502", component="thinking")

    def failing(**kwargs):
        raise error

    pipeline.cot_planner.plan = failing
    pipeline.config.enable_short_path = False


class DegradationTest(unittest.TestCase):
    def test_replays_last_successful_sql_with_fresh_data(self):
        pipeline = Text2SQLPipeline(
            sql_history_lookup=lambda user_id, query: [("erp_db", CACHED_SQL)]
        )
        break_planner(pipeline)

        result = pipeline.run(QUERY, user_id="li")

        self.assertTrue(result.degraded)
        self.assertEqual(result.degrade_reason, "model_unavailable_replayed_sql")
        self.assertEqual(len(result.step_logs), 1)
        # 数据是重新查出来的，不是缓存的结果集
        self.assertTrue(result.step_logs[0].execution_result["success"])
        self.assertGreater(result.step_logs[0].execution_result["row_count"], 0)

    def test_without_history_returns_degraded_not_exception(self):
        pipeline = Text2SQLPipeline()
        break_planner(pipeline, ModelTimeoutError("超时", component="thinking"))

        result = pipeline.run(QUERY, user_id="li")

        self.assertTrue(result.degraded)
        self.assertEqual(result.degrade_reason, "model_unavailable")
        self.assertEqual(result.step_logs, [])

    def test_replay_is_rechecked_against_current_permissions(self):
        """权限可能在上次查询之后收紧，重放不能绕过它。"""
        pipeline = Text2SQLPipeline(
            sql_history_lookup=lambda user_id, query: [("erp_db", CACHED_SQL)]
        )
        break_planner(pipeline)

        result = pipeline.run(
            QUERY,
            user_id="li",
            access=DataPolicy(allow_tables=("erp_db.sales_order",)),
        )

        self.assertTrue(result.degraded)
        self.assertEqual(result.degrade_reason, "model_unavailable")
        self.assertEqual(result.step_logs, [])

    def test_replay_gives_up_when_cached_sql_no_longer_runs(self):
        pipeline = Text2SQLPipeline(
            sql_history_lookup=lambda user_id, query: [
                ("erp_db", "SELECT * FROM table_dropped_yesterday")
            ]
        )
        break_planner(pipeline)

        result = pipeline.run(QUERY, user_id="li")

        self.assertEqual(result.degrade_reason, "model_unavailable")
        self.assertEqual(result.step_logs, [])

    def test_history_lookup_failure_does_not_break_degradation(self):
        def broken_lookup(user_id, query):
            raise RuntimeError("审计库连不上")

        pipeline = Text2SQLPipeline(sql_history_lookup=broken_lookup)
        break_planner(pipeline)

        result = pipeline.run(QUERY, user_id="li")
        self.assertTrue(result.degraded)

    def test_anonymous_user_has_no_replay_history(self):
        pipeline = Text2SQLPipeline(
            sql_history_lookup=lambda user_id, query: [("erp_db", CACHED_SQL)]
        )
        break_planner(pipeline)

        result = pipeline.run(QUERY)  # 不传 user_id
        self.assertEqual(result.step_logs, [])


class AuditReplayLookupTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteAuditStore(tempfile.mktemp(suffix=".db"))

    def _record(self, **overrides):
        payload = dict(
            trace_id="t",
            caller="li",
            user_id="li",
            session_id="s",
            query=QUERY,
            databases=["erp_db"],
            sqls=[{"database": "erp_db", "sql": CACHED_SQL}],
        )
        payload.update(overrides)
        self.store.record(AuditRecord(**payload))

    def test_returns_latest_successful_run(self):
        self._record(sqls=[{"database": "erp_db", "sql": "SELECT 1"}])
        self._record(sqls=[{"database": "erp_db", "sql": "SELECT 2"}])
        self.assertEqual(
            self.store.last_successful_sqls("li", QUERY), [("erp_db", "SELECT 2")]
        )

    def test_scoped_to_the_same_user(self):
        self._record()
        self.assertEqual(self.store.last_successful_sqls("someone", QUERY), [])

    def test_failed_and_degraded_runs_are_not_candidates(self):
        self._record(success=False)
        self._record(degraded=True, sqls=[{"database": "erp_db", "sql": "SELECT 9"}])
        self.assertEqual(self.store.last_successful_sqls("li", QUERY), [])

    def test_multi_step_cross_database_pairs_are_preserved(self):
        # 检索命中的库（databases）和每步实际执行的库可能不同，
        # 重放必须按步骤自带的库名，否则会把 SQL 打到错误的库上
        self._record(
            query="跨库问题",
            databases=["erp_db"],
            sqls=[
                {"database": "pm_db", "sql": "SELECT a"},
                {"database": "erp_db", "sql": "SELECT b"},
            ],
        )
        self.assertEqual(
            self.store.last_successful_sqls("li", "跨库问题"),
            [("pm_db", "SELECT a"), ("erp_db", "SELECT b")],
        )

    def test_legacy_rows_are_handled(self):
        """升级前写入的老记录：单库单步还能重放，跨库无法还原就放弃。"""
        import json
        import sqlite3

        conn = sqlite3.connect(self.store.db_path)
        conn.execute(
            "INSERT INTO ai_sql_audit_log "
            "(trace_id, created_at, caller, user_id, session_id, query, "
            " databases, sqls, success) VALUES (?,?,?,?,?,?,?,?,1)",
            (
                "legacy-1",
                "2026-08-18T00:00:00+00:00",
                "li",
                "li",
                "s",
                "老单库",
                json.dumps(["erp_db"]),
                json.dumps(["SELECT 1"]),
            ),
        )
        conn.execute(
            "INSERT INTO ai_sql_audit_log "
            "(trace_id, created_at, caller, user_id, session_id, query, "
            " databases, sqls, success) VALUES (?,?,?,?,?,?,?,?,1)",
            (
                "legacy-2",
                "2026-08-18T00:00:00+00:00",
                "li",
                "li",
                "s",
                "老跨库",
                json.dumps(["erp_db"]),
                json.dumps(["SELECT 1", "SELECT 2"]),
            ),
        )
        conn.commit()
        conn.close()

        self.assertEqual(
            self.store.last_successful_sqls("li", "老单库"), [("erp_db", "SELECT 1")]
        )
        self.assertEqual(self.store.last_successful_sqls("li", "老跨库"), [])


class DataQaDegradationTest(unittest.TestCase):
    def test_falls_back_to_rule_based_answer(self):
        import tempfile as tf
        from pathlib import Path

        from ai_sql_agent.memory import ConversationMemoryService, MemoryServiceConfig
        from ai_sql_agent.pipeline import DynamicSqlAgentService

        class BrokenQaClient:
            def answer(self, query, context):
                raise ModelServerError("模型服务错误：503", component="thinking")

        with tf.TemporaryDirectory() as temp_dir:
            memory = ConversationMemoryService(
                MemoryServiceConfig(
                    db_path=Path(temp_dir) / "memory.db",
                    embedding_dimensions=128,
                    vector_backend="none",
                    use_model_summarizer_when_available=False,
                    use_reranker_when_available=False,
                )
            )
            try:
                service = DynamicSqlAgentService(
                    Text2SQLPipeline(),
                    memory,
                    data_qa_client=BrokenQaClient(),
                    use_model_when_available=False,
                )
                result = service.run(
                    user_id="li", session_id="s1", query="什么是供应商名称？"
                )
                # 没有抛异常，而是给出了规则版答复
                self.assertTrue(result.answer)
                self.assertFalse(result.queried_database)
            finally:
                memory.close()


if __name__ == "__main__":
    unittest.main()
