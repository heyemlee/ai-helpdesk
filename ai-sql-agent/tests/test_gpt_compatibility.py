"""
换成 GPT 之后会碰到的那几处：请求参数、截断、结构化输出，以及带格式的输出怎么解析。

- OpenAI 的推理模型（GPT-5 起、o 系列）只认默认温度、输出上限叫 `max_completion_tokens`、
  可以调 `reasoning_effort`；中转站和开源推理框架不一定认这些参数——服务端点名拒绝哪个，
  就去掉 / 改名再发一次，并记住；
- 输出到了上限（`finish_reason=length`）不能把半截内容交出去；
- 路由判断走 JSON Schema（strict），不再从一段文字里找 JSON；
- `**数据库**：`、`- 结论：不一致`、`### 步骤2` 这类带格式的写法照样解析得出来，
  解析不出来时如实说没解析出来，而不是当成「通过」。

模型服务一律是 MockTransport，不出本机。
"""
import json
import unittest

import httpx

from ai_sql_agent.core import (
    ModelAccessRegistry,
    ModelBadRequestError,
    ModelResponseError,
    ModelTransport,
    RetryPolicy,
    build_chat_payload,
    extract_chat_content,
    get_settings,
    reload_settings,
)
from ai_sql_agent.core.model_text import plain_text
from ai_sql_agent.core.transport import forget_parameter_adaptations, openai_reasoning_model
from ai_sql_agent.generation.coder_client import CoderModelClient, CoderModelConfig
from ai_sql_agent.generation.cot_parser import CotStepParser
from ai_sql_agent.memory import ConversationMemoryContext
from ai_sql_agent.memory.objects import ShortTermContext
from ai_sql_agent.pipeline.routing import ROUTE_SCHEMA, ModelIntentClassifier, RouteType
from ai_sql_agent.retrieval.keyword_extractor_client import AliyunKeywordExtractor
from ai_sql_agent.validation.objects import (
    SELF_CHECK_MISMATCH,
    SELF_CHECK_NOT_APPLICABLE,
    SELF_CHECK_NOT_RUN,
    SELF_CHECK_PASSED,
    AnomalyType,
)
from ai_sql_agent.validation.self_check import SemanticSelfChecker
from ai_sql_agent.validation.validator import ResultValidator

URL = "https://model.invalid/v1/chat/completions"
CONTEXT = ConversationMemoryContext(short_term=ShortTermContext(user_id="u", session_id="s"))


def reply(content="ok", finish_reason="stop", **message):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content, **message}, "finish_reason": finish_reason}]},
    )


def rejection(text):
    return httpx.Response(400, json={"error": {"message": text, "type": "invalid_request_error"}})


class Recorder:
    """记下每个请求体，按顺序给出预先排好的应答。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0) if self.responses else reply()


def transport(handler, provider="openai"):
    return ModelTransport(
        "coder",
        api_key="test-key",
        provider=provider,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(max_retries=0),
        sleeper=lambda seconds: None,
        access_registry=ModelAccessRegistry(),
    )


class _Fresh(unittest.TestCase):
    def setUp(self):
        forget_parameter_adaptations()
        self.addCleanup(forget_parameter_adaptations)


class ReasoningModelNamesTest(unittest.TestCase):
    def test_which_names_count_as_openai_reasoning_models(self):
        for name in ("gpt-5", "gpt-5.6-luna", "gpt-6-sol", "gpt-6.1-sol", "o3", "o4-mini",
                     "openai/gpt-5.6-terra", "GPT-5.6-LUNA"):
            self.assertTrue(openai_reasoning_model(name), name)
        for name in ("gpt-4o", "gpt-4.1-mini", "qwen-plus", "deepseek-chat", "claude-sonnet-5",
                     "text-embedding-3-small", "", "gpt-4o-2024-08-06", "o"):
            self.assertFalse(openai_reasoning_model(name), name)


class PayloadTest(unittest.TestCase):
    MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    def test_reasoning_model_gets_effort_and_completion_budget_but_no_temperature(self):
        payload = build_chat_payload(
            self.MESSAGES, model="gpt-5.6-luna", max_tokens=8192, reasoning_effort="low"
        )
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["messages"], self.MESSAGES)

    def test_other_openai_compatible_models_are_sent_as_before(self):
        payload = build_chat_payload(
            self.MESSAGES, model="qwen-plus", max_tokens=8192, reasoning_effort="low"
        )
        self.assertEqual(payload, {"model": "qwen-plus", "messages": self.MESSAGES,
                                   "stream": False, "temperature": 0.0})

    def test_schema_becomes_a_strict_response_format(self):
        payload = build_chat_payload(
            self.MESSAGES, model="gpt-5.6-luna", response_schema=ROUTE_SCHEMA, schema_name="route"
        )
        self.assertEqual(payload["response_format"], {
            "type": "json_schema",
            "json_schema": {"name": "route", "strict": True, "schema": ROUTE_SCHEMA},
        })

    def test_anthropic_ignores_effort_and_schema(self):
        payload = build_chat_payload(
            self.MESSAGES, model="claude-sonnet-5", provider="anthropic", max_tokens=100,
            reasoning_effort="low", response_schema=ROUTE_SCHEMA,
        )
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("response_format", payload)
        self.assertEqual(payload["system"], "s")
        self.assertEqual(payload["max_tokens"], 100)

    def test_chat_sends_the_configured_effort_and_the_larger_budget(self):
        recorder = Recorder(reply("SELECT 1;"))
        transport(recorder).chat(URL, self.MESSAGES, model="gpt-5.6-luna")
        settings = get_settings()
        self.assertEqual(recorder.bodies[0]["reasoning_effort"], settings.reasoning_effort)
        self.assertEqual(recorder.bodies[0]["max_completion_tokens"], settings.max_completion_tokens)


class EffortSettingTest(unittest.TestCase):
    def test_empty_effort_sends_nothing(self):
        import os

        saved = os.environ.get("AI_SQL_REASONING_EFFORT")
        os.environ["AI_SQL_REASONING_EFFORT"] = ""
        self.addCleanup(lambda: (os.environ.pop("AI_SQL_REASONING_EFFORT", None)
                                 if saved is None else os.environ.__setitem__("AI_SQL_REASONING_EFFORT", saved),
                                 reload_settings()))
        reload_settings()
        recorder = Recorder(reply())
        transport(recorder).chat(URL, [{"role": "user", "content": "u"}], model="gpt-5.6-luna")
        self.assertNotIn("reasoning_effort", recorder.bodies[0])


class RejectedParameterTest(_Fresh):
    def test_rejected_effort_is_dropped_retried_and_remembered(self):
        recorder = Recorder(
            rejection("Unsupported parameter: 'reasoning_effort' is not supported with this model."),
            reply("first"),
            reply("second"),
        )
        client = transport(recorder)
        messages = [{"role": "user", "content": "u"}]
        self.assertEqual(client.chat(URL, messages, model="gpt-5.6-luna"), "first")
        self.assertEqual(client.chat(URL, messages, model="gpt-5.6-luna"), "second")
        self.assertIn("reasoning_effort", recorder.bodies[0])
        self.assertNotIn("reasoning_effort", recorder.bodies[1])
        self.assertNotIn("reasoning_effort", recorder.bodies[2])   # 记住了，不再先撞一次
        self.assertEqual(len(recorder.bodies), 3)

    def test_temperature_and_token_field_are_adapted_one_after_another(self):
        recorder = Recorder(
            rejection("Unsupported value: 'temperature' does not support 0.0 with this model."),
            rejection("Unsupported parameter: 'max_tokens' is not supported with this model. "
                      "Use 'max_completion_tokens' instead."),
            reply("done"),
        )
        payload = build_chat_payload([{"role": "user", "content": "u"}], model="relay-model")
        payload["max_tokens"] = 512
        result = transport(recorder).post_chat(URL, payload, model="relay-model")
        self.assertEqual(extract_chat_content(result, component="c", model="m"), "done")
        self.assertNotIn("temperature", recorder.bodies[2])
        self.assertEqual(recorder.bodies[2]["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", recorder.bodies[2])

    def test_unsupported_response_format_falls_back_to_plain_json_text(self):
        recorder = Recorder(
            rejection("Invalid parameter: 'response_format' of type 'json_schema' is not supported."),
            reply('{"route":"data_qa","confidence":0.9,"reason":"r"}'),
        )
        text = transport(recorder).chat(
            URL, [{"role": "user", "content": "u"}], model="gpt-5.6-luna",
            response_schema=ROUTE_SCHEMA, schema_name="route",
        )
        self.assertEqual(json.loads(text)["route"], "data_qa")
        self.assertNotIn("response_format", recorder.bodies[1])

    def test_a_400_about_something_else_is_not_retried(self):
        recorder = Recorder(rejection("This model's maximum context length is 128000 tokens."))
        with self.assertRaises(ModelBadRequestError):
            transport(recorder).chat(URL, [{"role": "user", "content": "u"}], model="gpt-5.6-luna")
        self.assertEqual(len(recorder.bodies), 1)

    def test_adaptations_are_per_endpoint_and_model(self):
        recorder = Recorder(
            rejection("Unrecognized request argument supplied: reasoning_effort"),
            reply(), reply(), reply(),
        )
        client = transport(recorder)
        messages = [{"role": "user", "content": "u"}]
        client.chat(URL, messages, model="gpt-5.6-luna")
        client.chat(URL, messages, model="gpt-6-sol")
        client.chat("https://other.invalid/v1/chat/completions", messages, model="gpt-5.6-luna")
        self.assertIn("reasoning_effort", recorder.bodies[2])     # 别的模型照常带
        self.assertIn("reasoning_effort", recorder.bodies[3])     # 别的地址照常带

    def test_streaming_adapts_before_the_first_chunk(self):
        stream_body = (
            'data: {"choices":[{"delta":{"content":"步骤1"}}]}\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
            "data: [DONE]\n"
        )
        recorder = Recorder(
            rejection("Unsupported parameter: 'reasoning_effort'"),
            httpx.Response(200, text=stream_body),
        )
        chunks = list(transport(recorder).chat_stream(URL, [{"role": "user", "content": "u"}],
                                                      model="gpt-5.6-luna"))
        self.assertEqual(chunks, ["步骤1"])
        self.assertTrue(recorder.bodies[1]["stream"])
        self.assertNotIn("reasoning_effort", recorder.bodies[1])


class TruncationTest(unittest.TestCase):
    def test_length_finish_is_an_error_not_half_a_sql(self):
        with self.assertRaises(ModelResponseError) as caught:
            extract_chat_content(
                {"choices": [{"message": {"content": "SELECT a, b FROM"}, "finish_reason": "length"}]},
                component="coder", model="gpt-5.6-luna",
            )
        self.assertIn("截断", str(caught.exception))

    def test_reasoning_that_used_up_the_budget_is_an_error(self):
        with self.assertRaises(ModelResponseError):
            extract_chat_content(
                {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
                component="coder", model="gpt-5.6-luna",
            )

    def test_refusal_is_reported(self):
        with self.assertRaises(ModelResponseError) as caught:
            extract_chat_content(
                {"choices": [{"message": {"content": None, "refusal": "I can't help with that."},
                              "finish_reason": "stop"}]},
                component="coder", model="gpt-5.6-luna",
            )
        self.assertIn("拒绝", str(caught.exception))

    def test_anthropic_max_tokens_stop_is_an_error(self):
        with self.assertRaises(ModelResponseError):
            extract_chat_content(
                {"content": [{"type": "text", "text": "步骤1"}], "stop_reason": "max_tokens"},
                component="thinking", model="claude-sonnet-5",
            )

    def test_truncated_stream_raises_after_the_partial_text(self):
        body = (
            'data: {"choices":[{"delta":{"content":"步骤1"}}]}\n'
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n'
            "data: [DONE]\n"
        )
        received = []
        with self.assertRaises(ModelResponseError):
            for chunk in transport(lambda request: httpx.Response(200, text=body)).chat_stream(
                URL, [{"role": "user", "content": "u"}], model="qwen-plus"
            ):
                received.append(chunk)
        self.assertEqual(received, ["步骤1"])

    def test_anthropic_truncated_stream_raises(self):
        body = (
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"步骤"}}\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n'
            'data: {"type":"message_stop"}\n'
        )
        with self.assertRaises(ModelResponseError):
            list(transport(lambda request: httpx.Response(200, text=body), provider="anthropic")
                 .chat_stream(URL, [{"role": "user", "content": "u"}], model="claude-sonnet-5"))


class StructuredRoutingTest(_Fresh):
    def test_classifier_asks_for_the_route_schema(self):
        calls = []

        class Client:
            def generate_structured(self, prompt, schema, name):
                calls.append((schema, name))
                return '{"route":"database_query","confidence":0.93,"reason":"需要新数据"}'

            def generate(self, prompt):
                raise AssertionError("有结构化输出时不该走纯文本")

        decision = ModelIntentClassifier(Client()).classify("上个月各车间产量", CONTEXT)
        self.assertEqual(decision.route, RouteType.DATABASE_QUERY)
        self.assertEqual(calls, [(ROUTE_SCHEMA, "route_decision")])

    def test_plain_clients_still_work(self):
        class Client:
            def generate(self, prompt):
                return '```json\n{"route":"data_qa","confidence":0.8,"reason":"解释口径"}\n```'

        decision = ModelIntentClassifier(Client()).classify("毛利率怎么算", CONTEXT)
        self.assertEqual(decision.route, RouteType.DATA_QA)

    def test_route_schema_is_strict_compatible(self):
        self.assertFalse(ROUTE_SCHEMA["additionalProperties"])
        self.assertEqual(set(ROUTE_SCHEMA["required"]), set(ROUTE_SCHEMA["properties"]))


class PlainTextTest(unittest.TestCase):
    def test_markdown_markers_are_removed(self):
        text = "### 步骤1\n- **数据库**：`erp_db`\n1. 先筛选\n* __输出目标__: supplier_name"
        self.assertEqual(plain_text(text), "步骤1\n数据库：erp_db\n先筛选\n输出目标: supplier_name")

    def test_decimals_and_single_asterisks_stay(self):
        self.assertEqual(plain_text("0.9 以下\na*b"), "0.9 以下\na*b")


class FormattedPlanTest(unittest.TestCase):
    GPT_STYLE = """### 步骤1
- **数据库**：`pm_db`
- **处理对象**：project.project_code，project.budget_amt，project.actual_cost
- **操作指令**：先在 project 表中筛选 actual_cost 大于 budget_amt 的项目；输出 project_code
- **输出目标**：project.project_code
- **预期输出量级**：数条

### 步骤2
- **数据库**：`erp_db`
- **处理对象**：purchase_order.job_no，purchase_order.po_amt
- **操作指令**：读取上一步的 project_code；按 job_no 汇总 po_amt
- **输出目标**：purchase_order.po_amt
- **预期输出量级**：数条
"""

    def test_bold_labels_headings_and_bullets_parse_into_steps(self):
        steps = CotStepParser().parse_many(self.GPT_STYLE)
        self.assertEqual([step.database for step in steps], ["pm_db", "erp_db"])
        self.assertEqual(steps[0].output_target, "project.project_code")
        self.assertEqual(steps[0].expected_cardinality, "数条")
        self.assertEqual(steps[1].expected_cardinality, "数条")


class FormattedKeywordsTest(unittest.TestCase):
    def test_bold_labels_do_not_become_keywords(self):
        extractor = AliyunKeywordExtractor.__new__(AliyunKeywordExtractor)
        keywords = extractor._parse_keywords("**解析结果**：准时率，供应商名称\n**映射术语**：供应商准时交付率")
        self.assertEqual(keywords, ["准时率", "供应商名称", "供应商准时交付率"])


class ScriptedChecker:
    def __init__(self, *answers):
        self.answers = list(answers)

    def generate(self, prompt):
        return self.answers.pop(0)


class MockScriptedChecker(ScriptedChecker):
    """像离线的 ThinkingModelClient 一样声明自己在用 Mock 回答。"""

    def __init__(self, *answers, from_mock=True):
        super().__init__(*answers)
        self.from_mock = from_mock

    def answers_from_mock(self):
        return self.from_mock


class FormattedSelfCheckTest(unittest.TestCase):
    def run_check(self, verdict):
        checker = SemanticSelfChecker(ScriptedChecker("**复述**：统计每个供应商的订单数量", verdict))
        return checker.run("每个供应商的采购金额", "SELECT supplier_id, COUNT(*) FROM po GROUP BY 1")

    def test_a_mock_client_is_not_applicable_and_is_not_asked(self):
        client = MockScriptedChecker("这是 Mock 的规划文本", "也没有结论")
        result = SemanticSelfChecker(client).run(
            "每个供应商的订单数量", "SELECT supplier_id, COUNT(*) FROM po GROUP BY 1"
        )
        self.assertEqual(result.status, SELF_CHECK_NOT_APPLICABLE)
        self.assertIsNone(result.finding)
        self.assertEqual(len(client.answers), 2, "Mock 不做语义判断，不该被问")

    def test_a_real_client_with_an_unreadable_verdict_is_still_not_run(self):
        client = MockScriptedChecker("复述：统计订单数量", "看起来没问题", from_mock=False)
        result = SemanticSelfChecker(client).run(
            "每个供应商的订单数量", "SELECT supplier_id, COUNT(*) FROM po GROUP BY 1"
        )
        self.assertEqual(result.status, SELF_CHECK_NOT_RUN)
        self.assertEqual(result.error, "UnreadableVerdict")

    def test_the_validator_leaves_no_finding_when_the_client_is_a_mock(self):
        validator = ResultValidator(
            self_checker=SemanticSelfChecker(MockScriptedChecker("Mock 文本", "Mock 文本")),
            enable_cardinality=False,
        )
        outcome = validator.validate(
            query="每个供应商的订单数量",
            sql="SELECT supplier_id, COUNT(*) FROM po GROUP BY 1",
            row_count=2,
        )
        self.assertEqual(outcome.findings, [])
        self.assertEqual(outcome.self_check, SELF_CHECK_NOT_APPLICABLE)
        self.assertTrue(outcome.passed)

    def test_bold_mismatch_is_a_mismatch(self):
        result = self.run_check("**结论**：不一致\n**理由**：问的是金额，统计的是数量")
        self.assertEqual(result.status, SELF_CHECK_MISMATCH)
        self.assertIn("问的是金额", result.finding.detail)
        self.assertIn("统计每个供应商的订单数量", result.finding.detail)

    def test_bold_match_passes(self):
        self.assertEqual(self.run_check("- **结论**：一致\n- **理由**：对得上").status, SELF_CHECK_PASSED)

    def test_unreadable_verdict_is_not_counted_as_passed(self):
        result = self.run_check("这条 SQL 基本上没问题。")
        self.assertEqual(result.status, SELF_CHECK_NOT_RUN)
        self.assertEqual(result.error, "UnreadableVerdict")

    def test_validator_says_why_the_check_did_not_happen(self):
        validator = ResultValidator(
            self_checker=SemanticSelfChecker(ScriptedChecker("复述：统计订单数量", "看起来没问题")),
            enable_cardinality=False,
        )
        outcome = validator.validate(
            query="每个供应商的订单数量",
            sql="SELECT supplier_id, COUNT(*) FROM po GROUP BY 1",
            row_count=2,
        )
        notes = [finding.detail for finding in outcome.findings
                 if finding.anomaly == AnomalyType.SELF_CHECK_NOT_RUN]
        self.assertEqual(len(notes), 1)
        self.assertIn("读不出结论", notes[0])
        self.assertFalse(outcome.passed)


class CleanSqlTest(unittest.TestCase):
    def setUp(self):
        self.client = CoderModelClient(CoderModelConfig(api_key="", use_mock_when_no_api_key=True))

    def test_fenced_block_wins_over_the_prose_around_it(self):
        text = "下面是查询：\n```sql\nSELECT name FROM supplier WHERE rate < 0.9;\n```\n这条 SQL 先筛选……"
        self.assertEqual(self.client.clean_sql(text), "SELECT name FROM supplier WHERE rate < 0.9;")

    def test_explanation_after_the_semicolon_is_dropped(self):
        text = "SELECT name FROM supplier;\nThis query lists every supplier."
        self.assertEqual(self.client.clean_sql(text), "SELECT name FROM supplier;")

    def test_semicolons_inside_literals_and_comments_do_not_cut(self):
        text = "SELECT 'a;b' AS x, 'it''s;' AS y -- note; here\nFROM t;"
        self.assertEqual(self.client.clean_sql(text), text)

    def test_chinese_paragraph_after_a_blank_line_is_not_sql(self):
        text = "SELECT name\nFROM supplier\n\n说明：按准时率筛选。"
        self.assertEqual(self.client.clean_sql(text), "SELECT name\nFROM supplier;")

    def test_plain_sql_is_unchanged(self):
        self.assertEqual(self.client.clean_sql("WITH a AS (SELECT 1) SELECT * FROM a"),
                         "WITH a AS (SELECT 1) SELECT * FROM a;")


if __name__ == "__main__":
    unittest.main()


class PromptRulesTest(unittest.TestCase):
    """
    换成 GPT 后评测里的两个固定毛病，各有规则对着：没要求汇总却求和（三轮 48 次错里占 16 次）、
    自己补题目没写的筛选条件。生成 SQL 那一步看不到原问题，所以规则主要放在规划里，
    生成提示词只做兜底。规则里的例子不能用评测集里的原题，否则测出来的提升不作数。
    """

    def test_planning_asks_for_detail_rows_unless_the_query_aggregates(self):
        from ai_sql_agent.planning.prompt_builder import CotPromptBuilder

        instruction = CotPromptBuilder()._build_task_instruction()
        self.assertIn("不要汇总成一个总数", instruction)
        self.assertIn("统计、合计、总额", instruction)
        self.assertNotIn("板材类供应商", instruction, "例子不能是评测集原题")
        self.assertIn("不要再加付款状态", instruction)
        # 2026-10-14 门禁：「逐条列出」被理解成不去重，三类题里 6 次答错只差一个 DISTINCT
        self.assertIn("不等于不去重", instruction)
        # 重复来自取值本身（同一个工人有很多条工时记录），不只是一对多关联的扇出
        self.assertIn("名称、编号这类取值", instruction)

    def test_generation_keeps_rows_and_filters_as_asked(self):
        from types import SimpleNamespace

        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        request = SimpleNamespace(
            cot_step=SimpleNamespace(processing_objects="", operation_instruction="", output_target=""),
            local_schema=SimpleNamespace(to_prompt_context=lambda: ""),
            sql_dialect="sqlite",
            certified_examples="",
            previous_results="",
        )
        prompt = SqlPromptBuilder().build(request)
        self.assertIn("操作指令没要求汇总，就逐条输出，不要聚合", prompt)
        self.assertIn("不要自己补充操作指令没写的条件", prompt)
        self.assertIn("「逐条」不等于不去重", prompt)
        self.assertIn("只输出「输出目标」里列出的字段", prompt)
        self.assertIn("不要用 LEFT JOIN 和 COALESCE 补行", prompt)

    def test_planning_keeps_the_output_to_what_was_asked(self):
        """
        换到 gpt-6.1-sol 后剩下的静默错误几乎都是输出形状：规划把筛选那一侧的编号和名称、
        日期、单位也写进了输出目标；「每个…的合计」以主表为准左关联，把没有记录的补成 0。
        """
        from ai_sql_agent.planning.prompt_builder import CotPromptBuilder

        instruction = CotPromptBuilder()._build_task_instruction()
        self.assertIn("输出目标只放Query问到的字段或计算结果", instruction)
        self.assertIn("它的编号、名称等字段不要输出", instruction)
        self.assertIn("跨库拆步时，前一步为下一步", instruction, "跨库拆步的关联键照样输出")
        # 只说「用主键标识每一行」时，模型拿快照编号、工单内部编号当标识，用户认不出来
        self.assertIn("快照编号、流水号这类只在系统内部用的记录编号不要拿来标识", instruction)
        self.assertIn("不要以X的主表为准", instruction)
        for leaked in ("板材", "涂料", "台面", "门板", "变更总金额", "工时合计"):
            self.assertNotIn(leaked, instruction, "例子不能是评测集原题")
