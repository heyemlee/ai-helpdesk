from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.memory import (
    ConversationMemoryContext,
    ConversationMemoryService,
    MemoryMessage,
    MemoryServiceConfig,
    MessageRole,
    ShortTermContext,
)
from ai_sql_agent.pipeline import (
    DynamicSqlAgentService,
    DynamicIntentRouter,
    ModelIntentClassifier,
    PipelineResult,
    RouteType,
    RuleBasedDataQAClient,
    RuleBasedIntentClassifier,
    StepExecutionLog,
)


def build_context(*messages: MemoryMessage) -> ConversationMemoryContext:
    return ConversationMemoryContext(
        short_term=ShortTermContext(
            session_id="s1",
            user_id="u1",
            messages=list(messages),
        )
    )


def sql_result_message() -> MemoryMessage:
    return MemoryMessage(
        id=1,
        session_id="s1",
        user_id="u1",
        role=MessageRole.ASSISTANT,
        content="已完成数据库查询",
        message_type="sql_result",
        payload={
            "step_logs": [
                {
                    "execution_result": {
                        "success": True,
                        "columns": ["supplier_name"],
                        "rows": [
                            {"supplier_name": 4.58},
                            {"supplier_name": 4.95},
                        ],
                    }
                }
            ]
        },
    )


class InvalidModelClient:
    def generate(self, prompt: str) -> str:
        return "这不是合法 JSON"


class FakePipeline:
    def __init__(self) -> None:
        self.call_count = 0
        self.contexts: list[str] = []
        self.access_values: list[object] = []

    def run(
        self,
        query: str,
        keywords=None,
        conversation_context: str = "",
        access=None,
        user_id: str = "",
    ) -> PipelineResult:
        self.call_count += 1
        self.contexts.append(conversation_context)
        self.access_values.append(access)
        return PipelineResult(
            query=query,
            keywords=["供应商名称"],
            schema_context="数据库：erp_db\n表名：supplier\n- 字段名：supplier_name",
            cot_output="查询供应商名称",
            step_logs=[
                StepExecutionLog(
                    database="erp_db",
                    cot_step=object(),
                    local_schema="supplier.supplier_name",
                    sql="SELECT supplier_name FROM supplier;",
                    execution_request={
                        "database": "erp_db",
                        "sql": "SELECT supplier_name FROM supplier;",
                    },
                    execution_result={
                        "success": True,
                        "columns": ["supplier_name"],
                        "rows": [{"supplier_name": 4.58}, {"supplier_name": 4.95}],
                    },
                )
            ],
        )


class RoutingTestCase(unittest.TestCase):
    def test_new_business_data_request_routes_to_database(self) -> None:
        decision = RuleBasedIntentClassifier().classify(
            "查询准时率低于0.9的供应商名称",
            build_context(),
        )
        self.assertEqual(decision.route, RouteType.DATABASE_QUERY)
        self.assertTrue(decision.needs_database)

    def test_metric_definition_routes_to_data_qa(self) -> None:
        decision = RuleBasedIntentClassifier().classify(
            "什么是供应商名称，它的业务口径是什么？",
            build_context(),
        )
        self.assertEqual(decision.route, RouteType.DATA_QA)
        self.assertFalse(decision.needs_database)

    def test_historical_result_follow_up_does_not_query_database(self) -> None:
        decision = RuleBasedIntentClassifier().classify(
            "帮我分析一下刚才的查询结果",
            build_context(sql_result_message()),
        )
        self.assertEqual(decision.route, RouteType.DATA_QA)
        self.assertIn("result_reference", decision.signals)

    def test_explicit_fresh_query_still_routes_to_database(self) -> None:
        decision = RuleBasedIntentClassifier().classify(
            "重新查询今天最新的采购金额",
            build_context(sql_result_message()),
        )
        self.assertEqual(decision.route, RouteType.DATABASE_QUERY)
        self.assertIn("fresh_data", decision.signals)

    def test_invalid_model_output_falls_back_to_rules(self) -> None:
        router = DynamicIntentRouter(
            model_classifier=ModelIntentClassifier(InvalidModelClient())
        )
        decision = router.route("统计今天的签单数量", build_context())
        self.assertEqual(decision.route, RouteType.DATABASE_QUERY)
        self.assertNotIn("model_classifier", decision.signals)

    def test_rule_based_data_qa_reuses_latest_sql_result(self) -> None:
        answer = RuleBasedDataQAClient().answer(
            "总结一下刚才的查询结果",
            build_context(sql_result_message()),
        )
        self.assertIn("4.58", answer)
        self.assertIn("4.95", answer)
        self.assertIn("平均值", answer)

    def test_model_data_qa_is_told_to_answer_in_the_questions_language(self) -> None:
        """对话问答是唯一直接给人读的模型原文：英文提问不该得到一段中文。"""
        from ai_sql_agent.pipeline.data_qa import ModelDataQAClient

        class RecordingClient:
            prompt = ""

            def generate(self, prompt: str) -> str:
                self.prompt = prompt
                return " ok "

        client = RecordingClient()
        answer = ModelDataQAClient(
            client, definitions={"安全库存": "按最新快照的可用量比较"}
        ).answer("What does safety stock mean here?", build_context())

        self.assertEqual(answer, "ok")
        self.assertIn("相同的语言", client.prompt)
        # 口径仍然照原文注入：换语言作答，不换定义
        self.assertIn("按最新快照的可用量比较", client.prompt)
        self.assertIn("What does safety stock mean here?", client.prompt)

    def test_english_data_requests_route_to_the_database_by_rules(self) -> None:
        """
        模型路由失灵时由规则兜底。规则里只有中文词表的话，英文问句一个词也命不中，
        全都落进「默认对话问答」——实测模型服务返回 530 的那一刻，用英文问 PO-24-0103
        的采购额得到的是一句「上下文里信息不够」，而不是一次查询。
        """
        classifier = RuleBasedIntentClassifier()
        for query in (
            "Which suppliers have an on-time delivery rate below 0.9?",
            "What are the purchase total, supplier payments and unpaid balance in USD for PO-24-0103?",
            "List the work orders for sales order SO-24-0003",
            "How many projects are over budget?",
            "Show me the top 5 suppliers by purchase amount",
            "For projects whose actual cost exceeds budget, summarize total purchase amounts by project code.",
        ):
            decision = classifier.classify(query, build_context())
            self.assertEqual(decision.route, RouteType.DATABASE_QUERY, query)

        fresh = classifier.classify(
            "Using the latest snapshot for each material, which materials are below safety stock?",
            build_context(sql_result_message()),
        )
        self.assertEqual(fresh.route, RouteType.DATABASE_QUERY)
        self.assertIn("fresh_data", fresh.signals)

        # 「再查一次」本身就是查询请求，手里有上一次的结果也一样
        for query in (
            "Re-run that with the latest data",
            "Please rerun the query",
            "Re-query the stock levels",
        ):
            again = classifier.classify(query, build_context(sql_result_message()))
            self.assertEqual(again.route, RouteType.DATABASE_QUERY, query)

    def test_english_explanations_and_follow_ups_stay_in_conversation(self) -> None:
        classifier = RuleBasedIntentClassifier()
        for query, signal in (
            ("What does safety stock mean here?", "knowledge_question"),
            ("How is the on-time rate calculated?", "knowledge_question"),
            ("Why would a purchase order be partly paid?", "explanation_request"),
            ("Thanks, that helps", "safe_default"),
        ):
            decision = classifier.classify(query, build_context())
            self.assertEqual(decision.route, RouteType.DATA_QA, query)
            self.assertIn(signal, decision.signals, query)

        follow_up = classifier.classify(
            "Can you explain the results above?", build_context(sql_result_message())
        )
        self.assertEqual(follow_up.route, RouteType.DATA_QA)
        self.assertIn("result_reference", follow_up.signals)

    def test_english_cues_match_whole_words(self) -> None:
        classifier = RuleBasedIntentClassifier()
        # price_list / person / stopwatch 里藏着 list / per / top
        for query in ("the price_list and each_person stopwatch", "personal toppings"):
            decision = classifier.classify(query, build_context())
            self.assertEqual(decision.route, RouteType.DATA_QA, query)
        # 中文问句里夹着英文词照常认
        self.assertEqual(
            classifier.classify("采购额 top 5 的供应商", build_context()).route,
            RouteType.DATABASE_QUERY,
        )

    def test_a_failing_routing_model_falls_back_to_english_rules_too(self) -> None:
        class DownClient:
            def generate(self, prompt: str) -> str:
                raise RuntimeError("530 from the relay")

        router = DynamicIntentRouter(model_classifier=ModelIntentClassifier(DownClient()))
        decision = router.route("Which suppliers are late most often?", build_context())
        self.assertEqual(decision.route, RouteType.DATABASE_QUERY)
        self.assertNotIn("model_classifier", decision.signals)

    def test_rule_based_reply_follows_the_questions_language(self) -> None:
        client = RuleBasedDataQAClient()
        english = client.answer("Thanks, that helps", build_context())
        self.assertIn("not enough in this conversation", english)
        self.assertFalse(any("一" <= char <= "鿿" for char in english))
        chinese = client.answer("谢谢", build_context())
        self.assertIn("没有足够的信息", chinese)

    def test_conversational_fallback_is_reported_as_degraded(self) -> None:
        """
        对话问答这条路上模型挂了，答复改由规则版给出。这必须让调用方知道：
        否则它拿到的是一句固定话术加 degraded=false，看起来就像模型认真答过。
        """
        from ai_sql_agent.api.result_summary import summarize_result
        from ai_sql_agent.core.errors import ModelCallError

        class DownQA:
            def answer(self, query, context):
                raise ModelCallError("模型服务错误", component="thinking", status_code=530)

        with tempfile.TemporaryDirectory() as temp_dir:
            memory = ConversationMemoryService(
                MemoryServiceConfig(
                    db_path=Path(temp_dir) / "memory.db",
                    embedding_dimensions=128,
                    use_model_summarizer_when_available=False,
                    use_reranker_when_available=False,
                )
            )
            try:
                def run(data_qa_client):
                    service = DynamicSqlAgentService(
                        pipeline=FakePipeline(),
                        memory=memory,
                        router=DynamicIntentRouter(),
                        data_qa_client=data_qa_client,
                        use_model_when_available=False,
                    )
                    return service.run(user_id="u1", session_id="s9", query="Thanks, that helps")

                down = run(DownQA())
                self.assertEqual(down.decision.route, RouteType.DATA_QA)
                self.assertTrue(down.degraded)
                summary = summarize_result(down)
                self.assertTrue(summary.degraded)
                self.assertEqual(summary.degrade_reason, "model_unavailable")
                self.assertEqual(summary.execution_status, "not_applicable")

                healthy = summarize_result(run(RuleBasedDataQAClient()))
                self.assertFalse(healthy.degraded)
                self.assertEqual(healthy.degrade_reason, "")
            finally:
                memory.close()

    def test_dynamic_service_only_calls_pipeline_for_database_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = ConversationMemoryService(
                MemoryServiceConfig(
                    db_path=Path(temp_dir) / "memory.db",
                    embedding_dimensions=128,
                    use_model_summarizer_when_available=False,
                    use_reranker_when_available=False,
                )
            )
            pipeline = FakePipeline()
            service = DynamicSqlAgentService(
                pipeline=pipeline,
                memory=memory,
                router=DynamicIntentRouter(),
                data_qa_client=RuleBasedDataQAClient(),
                use_model_when_available=False,
            )
            try:
                database_response = service.run(
                    user_id="u1",
                    session_id="s1",
                    query="查询供应商名称是多少",
                )
                self.assertEqual(database_response.decision.route, RouteType.DATABASE_QUERY)
                self.assertTrue(database_response.queried_database)
                self.assertEqual(pipeline.call_count, 1)

                qa_response = service.run(
                    user_id="u1",
                    session_id="s1",
                    query="分析一下刚才的查询结果",
                )
                self.assertEqual(qa_response.decision.route, RouteType.DATA_QA)
                self.assertFalse(qa_response.queried_database)
                self.assertEqual(pipeline.call_count, 1)
                self.assertIn("4.58", qa_response.answer)

                messages = memory.store.list_messages(user_id="u1", session_id="s1")
                self.assertEqual(messages[-1].message_type, "data_qa")
                self.assertEqual(
                    messages[-1].metadata["route_decision"]["route"],
                    RouteType.DATA_QA.value,
                )
            finally:
                memory.close()


if __name__ == "__main__":
    unittest.main()
