"""反馈回路：用户说「对/错」之后，系统真的会因此变化。

三条必须成立的性质：

1. **服务端说了算**：反馈只带 trace_id 与判定，SQL 从审计记录回填。
   否则「这条 SQL 是对的」就成了客户端的一句话，而认证 SQL 会进后续 Prompt。
2. **不越权**：不能替别人的查询表态，也不能看到别人的认证范例——
   范例里有表名，等于绕过数据权限看 Schema。
3. **改主意不双计**：同一条查询改判后，统计与范例都以最新一次为准。
"""

import tempfile
import unittest

from ai_sql_agent.audit import AuditRecord, SQLiteAuditStore
from ai_sql_agent.feedback import (
    CertifiedQuery,
    CertifiedSqlLibrary,
    FeedbackRecord,
    SQLiteFeedbackStore,
    Verdict,
    render_certified_examples,
)


def _feedback(**overrides) -> FeedbackRecord:
    payload = dict(
        trace_id="t1",
        user_id="u1",
        verdict=Verdict.CORRECT,
        query="查询准时率低于0.9的供应商名称",
        databases=["erp_db"],
        tables=["supplier"],
        sqls=[
            {
                "database": "erp_db",
                "sql": "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9",
            }
        ],
    )
    payload.update(overrides)
    return FeedbackRecord(**payload)


class FeedbackStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteFeedbackStore(tempfile.mktemp(suffix=".db"))

    def test_records_and_reads_back(self):
        self.store.record(_feedback())
        rows = self.store.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "correct")
        self.assertEqual(rows[0]["sqls"][0]["database"], "erp_db")

    def test_revote_replaces_instead_of_appending(self):
        """用户先点对又改成错是常事；留两条会让统计双计。"""
        self.store.record(_feedback(verdict=Verdict.CORRECT))
        self.store.record(_feedback(verdict=Verdict.WRONG, comment="其实错了"))

        rows = self.store.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "wrong")
        self.assertEqual(rows[0]["comment"], "其实错了")

        stats = self.store.stats()
        self.assertEqual(stats.total, 1)
        self.assertEqual(stats.correct, 0)
        self.assertEqual(stats.wrong, 1)

    def test_revote_to_wrong_drops_it_from_certified_examples(self):
        """被否掉的 SQL 不能继续当范例注入别的查询。"""
        self.store.record(_feedback(verdict=Verdict.CORRECT))
        self.assertEqual(len(self.store.certified(user_id="u1")), 1)

        self.store.record(_feedback(verdict=Verdict.WRONG))
        self.assertEqual(self.store.certified(user_id="u1"), [])

    def test_certified_is_scoped_to_the_user(self):
        self.store.record(_feedback(trace_id="a", user_id="u1"))
        self.store.record(_feedback(trace_id="b", user_id="u2"))
        self.assertEqual(len(self.store.certified(user_id="u1")), 1)
        self.assertEqual(len(self.store.certified(user_id="u2")), 1)

    def test_stats_separate_silent_errors_from_flagged_ones(self):
        """答错但被提示过 ≠ 悄悄答错。后者才是最贵的那一类。"""
        self.store.record(_feedback(trace_id="a", verdict=Verdict.CORRECT))
        self.store.record(_feedback(trace_id="b", verdict=Verdict.WRONG, had_risk=True))
        self.store.record(_feedback(trace_id="c", verdict=Verdict.WRONG, had_risk=False))

        stats = self.store.stats()
        self.assertEqual(stats.wrong_with_risk, 1)
        self.assertEqual(stats.wrong_without_risk, 1)
        self.assertAlmostEqual(stats.accuracy, 1 / 3)
        self.assertAlmostEqual(stats.silent_error_rate, 1 / 3)

    def test_unsure_does_not_count_as_right_or_wrong(self):
        self.store.record(_feedback(verdict=Verdict.UNSURE))
        stats = self.store.stats()
        self.assertEqual(stats.unsure, 1)
        self.assertEqual(stats.accuracy, 0.0)

    def test_mark_resolved(self):
        self.store.record(_feedback(verdict=Verdict.WRONG))
        self.assertTrue(self.store.mark_resolved("t1"))
        self.assertEqual(self.store.stats().unresolved_wrong, 0)
        self.assertFalse(self.store.mark_resolved("nope"))

    def test_feedback_without_sql_is_never_certified(self):
        """对话问答链路没有 SQL，点「对」也不该产生范例。"""
        self.store.record(_feedback(sqls=[]))
        self.assertEqual(self.store.certified(user_id="u1"), [])


class CertifiedLibraryTest(unittest.TestCase):
    CANDIDATES = [
        CertifiedQuery(
            query="查询准时率低于0.8的供应商名称",
            database="erp_db",
            sql="SELECT supplier_name FROM supplier WHERE on_time_rate < 0.8",
        ),
        CertifiedQuery(
            query="统计每个物料的在库数量",
            database="wms_db",
            sql="SELECT material_id, SUM(qty) FROM stock GROUP BY material_id",
        ),
    ]

    def _library(self, **kwargs) -> CertifiedSqlLibrary:
        return CertifiedSqlLibrary(loader=lambda user_id: self.CANDIDATES, **kwargs)

    def test_returns_similar_query_from_the_same_database(self):
        hits = self._library().lookup(
            query="查询准时率低于0.95的供应商名称", database="erp_db", user_id="u1"
        )
        self.assertEqual([hit.query for hit in hits], ["查询准时率低于0.8的供应商名称"])

    def test_does_not_leak_examples_across_databases(self):
        hits = self._library().lookup(
            query="查询准时率低于0.95的供应商名称", database="wms_db", user_id="u1"
        )
        self.assertEqual(hits, [])

    def test_identical_question_is_excluded(self):
        """完全相同的问题走降级重放；当范例给出去等于把答案抄给模型。"""
        hits = self._library().lookup(
            query="查询准时率低于0.8的供应商名称", database="erp_db", user_id="u1"
        )
        self.assertEqual(hits, [])

    def test_unrelated_question_gets_no_examples(self):
        hits = self._library().lookup(
            query="今年三月的生产工单良率", database="erp_db", user_id="u1"
        )
        self.assertEqual(hits, [])

    def test_anonymous_user_gets_no_examples(self):
        hits = self._library().lookup(
            query="查询准时率低于0.95的供应商名称", database="erp_db", user_id=""
        )
        self.assertEqual(hits, [])

    def test_permission_filter_hides_examples_touching_denied_tables(self):
        """权限收紧后，老范例不能继续把无权表的表名带进 Prompt。"""

        class DenySupplier:
            def allows_table(self, database, table):
                return table != "supplier"

        hits = self._library().lookup(
            query="查询准时率低于0.95的供应商名称",
            database="erp_db",
            user_id="u1",
            access=DenySupplier(),
        )
        self.assertEqual(hits, [])

    def test_broken_loader_does_not_break_generation(self):
        def explode(user_id):
            raise RuntimeError("反馈库挂了")

        library = CertifiedSqlLibrary(loader=explode)
        self.assertEqual(
            library.lookup(query="随便问问", database="erp_db", user_id="u1"), []
        )

    def test_top_k_limits_how_much_prompt_budget_examples_take(self):
        many = [
            CertifiedQuery(
                query=f"查询准时率低于0.{n}的供应商名称",
                database="erp_db",
                sql=f"SELECT supplier_name FROM supplier WHERE on_time_rate < 0.{n}",
            )
            for n in range(1, 9)
        ]
        library = CertifiedSqlLibrary(loader=lambda user_id: many, top_k=2)
        hits = library.lookup(
            query="查询准时率低于0.95的供应商名称", database="erp_db", user_id="u1"
        )
        self.assertEqual(len(hits), 2)

    def test_rendered_section_warns_against_copying_filters(self):
        """范例越相关，模型越容易连筛选条件一起抄——这句提示不能少。"""
        rendered = render_certified_examples(self.CANDIDATES[:1])
        self.assertIn("不要照抄筛选条件", rendered)
        self.assertIn("SELECT supplier_name", rendered)

    def test_no_examples_render_to_empty_string(self):
        self.assertEqual(render_certified_examples([]), "")


class CertifiedPromptTest(unittest.TestCase):
    """认证范例必须真的出现在 SQL 生成 Prompt 里，否则前面都是白做。"""

    def test_examples_reach_the_generation_prompt(self):
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        request = SqlGenerationRequest(
            cot_step=CotStep(
                database="erp_db",
                processing_objects="supplier.supplier_name",
                operation_instruction="筛选准时率低于0.95的供应商",
                output_target="supplier_name",
            ),
            local_schema=LocalSchema(),
            certified_examples=render_certified_examples(
                [
                    CertifiedQuery(
                        query="查询准时率低于0.8的供应商名称",
                        database="erp_db",
                        sql="SELECT supplier_name FROM supplier WHERE on_time_rate < 0.8",
                    )
                ]
            ),
        )
        prompt = SqlPromptBuilder().build(request)
        self.assertIn("已确认正确的相似查询", prompt)
        self.assertIn("on_time_rate < 0.8", prompt)

    def test_prompt_is_unchanged_when_there_are_no_examples(self):
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        request = SqlGenerationRequest(
            cot_step=CotStep(
                database="erp_db",
                processing_objects="supplier.supplier_name",
                operation_instruction="筛选",
                output_target="supplier_name",
            ),
            local_schema=LocalSchema(),
        )
        prompt = SqlPromptBuilder().build(request)
        self.assertNotIn("已确认正确的相似查询", prompt)


class PipelineCertifiedTest(unittest.TestCase):
    def test_pipeline_renders_examples_from_the_lookup(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline(
            certified_lookup=lambda **kwargs: [
                CertifiedQuery(
                    query="查询准时率低于0.8的供应商名称",
                    database="erp_db",
                    sql="SELECT supplier_name FROM supplier WHERE on_time_rate < 0.8",
                )
            ]
        )
        rendered = pipeline._certified_examples(
            query="查询准时率低于0.95的供应商名称",
            database="erp_db",
            user_id="u1",
            access=None,
        )
        self.assertIn("on_time_rate < 0.8", rendered)

    def test_no_lookup_configured_means_no_examples(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()
        self.assertEqual(
            pipeline._certified_examples(
                query="q", database="erp_db", user_id="u1", access=None
            ),
            "",
        )

    def test_lookup_failure_does_not_break_the_pipeline(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        def explode(**kwargs):
            raise RuntimeError("反馈库挂了")

        pipeline = Text2SQLPipeline(certified_lookup=explode)
        self.assertEqual(
            pipeline._certified_examples(
                query="q", database="erp_db", user_id="u1", access=None
            ),
            "",
        )


class AuditTraceLookupTest(unittest.TestCase):
    """反馈按 trace_id 回填 SQL，所以审计必须能按 trace_id 取回记录。"""

    def setUp(self):
        self.store = SQLiteAuditStore(tempfile.mktemp(suffix=".db"))

    def test_round_trip_including_risk_flag(self):
        self.store.record(
            AuditRecord(
                trace_id="t_abc",
                caller="c",
                user_id="u1",
                session_id="s",
                query="问题",
                sqls=[{"database": "erp_db", "sql": "SELECT 1"}],
                has_risk=True,
            )
        )
        row = self.store.get_by_trace("t_abc")
        self.assertEqual(row["query"], "问题")
        self.assertTrue(row["has_risk"])
        self.assertEqual(row["sqls"][0]["sql"], "SELECT 1")

    def test_unknown_trace_returns_none(self):
        self.assertIsNone(self.store.get_by_trace("nope"))


if __name__ == "__main__":
    unittest.main()


class FeedbackApiTest(unittest.TestCase):
    """接口层：只收判定，SQL 从审计回填；不能替别人表态。"""

    @classmethod
    def setUpClass(cls):
        import json
        import os
        from pathlib import Path

        from fastapi.testclient import TestClient

        from ai_sql_agent.core import reload_settings

        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(
            json.dumps(
                {
                    "keys": [
                        {"key": "k-li", "caller": "li", "user_id": "li"},
                        {"key": "k-wang", "caller": "wang", "user_id": "wang"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        cls.saved = {
            key: os.environ.get(key)
            for key in (
                "AI_SQL_AUTH_MODE",
                "AI_SQL_API_KEYS_FILE",
                "AI_SQL_AUDIT_DB_PATH",
            )
        }
        os.environ["AI_SQL_AUTH_MODE"] = "api_key"
        os.environ["AI_SQL_API_KEYS_FILE"] = str(key_file)
        os.environ["AI_SQL_AUDIT_DB_PATH"] = str(Path(cls.tmpdir.name) / "audit.db")
        reload_settings()

        from ai_sql_agent.api.app import app

        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        import os

        from ai_sql_agent.core import reload_settings

        cls.client_ctx.__exit__(None, None, None)
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        cls.tmpdir.cleanup()

    def _ask(self, key="k-li", user_id="li"):
        response = self.client.post(
            "/query",
            headers={"X-API-Key": key},
            json={
                "user_id": user_id,
                "session_id": "s_fb",
                "query": "查询准时率低于0.9的供应商名称",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["trace_id"]

    def test_feedback_backfills_sql_from_the_audit_record(self):
        """客户端只报 trace_id 与判定，SQL 由服务端从审计取。"""
        trace = self._ask()
        response = self.client.post(
            "/feedback",
            headers={"X-API-Key": "k-li"},
            json={"trace_id": trace, "verdict": "correct"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["recorded"])

        recorded = self.client.get(
            "/feedback/stats", headers={"X-API-Key": "k-li"}
        )
        # 这套 Key 不是管理员，统计接口应当拒绝
        self.assertEqual(recorded.status_code, 403)

    def test_cannot_submit_feedback_for_someone_elses_query(self):
        """反馈会影响认证范例，替别人表态等于替别人认证 SQL。"""
        trace = self._ask(key="k-li", user_id="li")
        response = self.client.post(
            "/feedback",
            headers={"X-API-Key": "k-wang"},
            json={"trace_id": trace, "verdict": "correct"},
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_trace_is_404(self):
        response = self.client.post(
            "/feedback",
            headers={"X-API-Key": "k-li"},
            json={"trace_id": "no-such-trace", "verdict": "wrong"},
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_verdict_is_rejected(self):
        trace = self._ask()
        response = self.client.post(
            "/feedback",
            headers={"X-API-Key": "k-li"},
            json={"trace_id": trace, "verdict": "maybe"},
        )
        self.assertEqual(response.status_code, 422)

    def test_feedback_requires_authentication(self):
        trace = self._ask()
        response = self.client.post(
            "/feedback", json={"trace_id": trace, "verdict": "correct"}
        )
        self.assertEqual(response.status_code, 401)


class CertifiedGeneratorLookupTest(unittest.TestCase):
    def test_generator_returning_lookup_is_materialized_once(self):
        """lookup 返回生成器时也要正常工作，且日志计数与实际注入一致。"""
        from ai_sql_agent.pipeline import Text2SQLPipeline

        def generator_lookup(**kwargs):
            yield CertifiedQuery(
                query="查询准时率低于0.8的供应商名称",
                database="erp_db",
                sql="SELECT supplier_name FROM supplier WHERE on_time_rate < 0.8",
            )

        pipeline = Text2SQLPipeline(certified_lookup=generator_lookup)
        rendered = pipeline._certified_examples(
            query="查询准时率低于0.95的供应商名称",
            database="erp_db",
            user_id="u1",
            access=None,
        )
        self.assertIn("on_time_rate < 0.8", rendered)
