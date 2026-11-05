"""
链路追踪（ai_sql_agent.core.telemetry）和查询历史里的 run_id。

起整个 app（演示库 + Mock 模型），和 test_api_observability 同一套搭法。span 收进内存里的
导出器，不发任何网络请求。

要钉住的是：调用方的 traceparent 真的成了父节点（Tempo 里客服和 SQL Agent 是同一条链路）；
阶段、SQL 执行、模型调用各有 span；span 里没有 SQL 和问题原文；流式响应的 span 等流发完才结束；
JSON 日志带着 OTel 的 trace id；/history 能拿到 run_id。
"""
import io
import json
import logging
import unittest
import uuid

from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_sql_agent.api.app import TRACE_HEADER, app
from ai_sql_agent.core import get_settings, run_context, telemetry
from ai_sql_agent.core.logging_setup import JsonFormatter, TraceIdFilter

EXPORTER = InMemorySpanExporter()

# 调用方那一跳：员工实例的 httpx 发请求时带的 traceparent
UPSTREAM_TRACE = uuid.uuid4().hex
UPSTREAM_SPAN = uuid.uuid4().hex[:16]
TRACEPARENT = f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01"

DEMO_QUESTION = "查询准时率低于0.9的供应商名称"


def setUpModule():
    # 全局的 TracerProvider 一个进程只能设一次。别的用例先设过了就往它上面挂导出器
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))


def _spans_of(trace_id_hex: str):
    return [s for s in EXPORTER.get_finished_spans()
            if format(s.context.trace_id, "032x") == trace_id_hex]


class TracingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        EXPORTER.clear()
        self.session = f"s_trace_{uuid.uuid4().hex[:8]}"

    def _query(self, headers=None, user_id="u_trace"):
        response = self.client.post(
            "/query",
            headers=headers or {},
            json={"user_id": user_id, "session_id": self.session, "query": DEMO_QUESTION},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_the_callers_traceparent_becomes_the_parent(self):
        response = self._query({"traceparent": TRACEPARENT})
        spans = _spans_of(UPSTREAM_TRACE)
        servers = [s for s in spans if s.kind == trace.SpanKind.SERVER]
        self.assertEqual(len(servers), 1, [s.name for s in spans])
        server = servers[0]

        self.assertEqual(server.name, "POST /query")
        self.assertEqual(format(server.parent.span_id, "016x"), UPSTREAM_SPAN)
        self.assertTrue(server.parent.is_remote)
        # 自己的 16 位 trace_id 和 run_id 记在 span 上：在 Tempo 里看到这一跳，就能去查审计和 run
        self.assertEqual(server.attributes["ai_sql.trace_id"], response.json()["trace_id"])
        self.assertEqual(server.attributes["ai_sql.run_id"], response.headers["X-Run-Id"])
        self.assertEqual(server.attributes["http.response.status_code"], 200)

    def test_stages_and_sql_executions_hang_under_the_request(self):
        self._query({"traceparent": TRACEPARENT})
        spans = _spans_of(UPSTREAM_TRACE)
        by_id = {s.context.span_id: s for s in spans}
        server = next(s for s in spans if s.kind == trace.SpanKind.SERVER)

        stages = [s for s in spans if s.name.startswith("stage.")]
        self.assertTrue(stages, [s.name for s in spans])
        self.assertTrue({"stage.routing", "stage.sql_generation", "stage.execution"}
                        <= {s.name for s in stages}, sorted({s.name for s in stages}))
        for current in stages:
            # 每个阶段一路往上都能找到这个请求
            node = current
            while node.parent is not None and node.parent.span_id in by_id:
                node = by_id[node.parent.span_id]
            self.assertIs(node, server, current.name)

        executions = [s for s in spans if s.name == "sql.execute"]
        self.assertTrue(executions)
        for execution in executions:
            self.assertIn(by_id[execution.parent.span_id].name, {s.name for s in stages})
            self.assertEqual(execution.attributes["ai_sql.status"], "ok")
            self.assertIn("ai_sql.database", execution.attributes)

    def test_spans_carry_no_sql_or_question_text(self):
        """SQL、问题原文和错误原文在审计和运行事件里，权限和保留期都在那边管；span 里不放。"""
        self._query({"traceparent": TRACEPARENT})
        for current in _spans_of(UPSTREAM_TRACE):
            for key, value in current.attributes.items():
                text = str(value)
                self.assertNotIn("SELECT", text.upper(), f"{current.name}.{key}")
                self.assertNotIn(DEMO_QUESTION, text, f"{current.name}.{key}")

    def test_without_traceparent_the_request_starts_its_own_trace(self):
        response = self._query()
        servers = [s for s in EXPORTER.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
        self.assertEqual(len(servers), 1)
        self.assertIsNone(servers[0].parent)
        self.assertEqual(servers[0].attributes["ai_sql.trace_id"], response.headers[TRACE_HEADER])

    def test_a_stream_span_ends_after_the_stream_does(self):
        """A2A 的 message/stream 在中间件返回之后才干活：请求的 span 要盖住底下所有阶段。"""
        get_settings().a2a_streaming = True
        try:
            body = {"jsonrpc": "2.0", "id": "req-1", "method": "message/stream",
                    "params": {"message": {"kind": "message", "role": "user", "messageId": "m1",
                                           "contextId": self.session,
                                           "parts": [{"kind": "text", "text": DEMO_QUESTION}],
                                           "metadata": {"user_id": "u_trace"}}}}
            with self.client.stream("POST", "/a2a", json=body,
                                    headers={"traceparent": TRACEPARENT}) as response:
                frames = [json.loads(line[5:]) for line in response.iter_lines()
                          if line.startswith("data:")]
        finally:
            get_settings().a2a_streaming = False

        task_id = frames[0]["result"]["id"]
        spans = _spans_of(UPSTREAM_TRACE)
        server = next(s for s in spans if s.kind == trace.SpanKind.SERVER)
        stages = [s for s in spans if s.name.startswith("stage.")]
        self.assertTrue(stages)
        self.assertEqual(server.name, "POST /a2a")
        self.assertEqual(server.attributes["ai_sql.run_id"], task_id)
        self.assertGreaterEqual(server.end_time, max(s.end_time for s in stages))

    def test_a_model_call_becomes_a_span_of_its_own_duration(self):
        """模型调用在结束时才发运行事件、带着耗时：span 的开始时间往回推这么多。"""
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("stage.sql_generation") as parent:
            run_context.emit("model.call", component="coder", model="m-1", status="error",
                             attempt=0, duration_ms=250.0, error_category="timeout",
                             error="模型返回了一段不该进 span 的原文")
        span = next(s for s in EXPORTER.get_finished_spans() if s.name == "model.call")
        self.assertEqual(span.parent.span_id, parent.get_span_context().span_id)
        self.assertAlmostEqual((span.end_time - span.start_time) / 1e6, 250.0, delta=1.0)
        self.assertEqual(span.attributes["ai_sql.component"], "coder")
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertNotIn("ai_sql.error", span.attributes)

    def test_history_returns_the_run_id(self):
        user = f"u_hist_{uuid.uuid4().hex[:8]}"
        response = self._query(user_id=user)
        items = self.client.get("/history", params={"user_id": user}).json()["items"]
        self.assertEqual(items[0]["trace_id"], response.json()["trace_id"])
        self.assertEqual(items[0]["run_id"], response.headers["X-Run-Id"])


class JsonLogTest(unittest.TestCase):
    def test_json_logs_carry_the_otel_trace_id(self):
        """Grafana 的 Loki 数据源按 otel_trace_id 从日志跳到 Tempo。"""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(TraceIdFilter())
        # 不挂在 ai_sql 底下：全量跑时 AI_SQL_LOG_LEVEL=ERROR，会把这里的 warning 吞掉
        logger = logging.getLogger("test_tracing.json_log")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.warning("outside")
            with trace.get_tracer("test").start_as_current_span("x") as current:
                logger.warning("inside")
            expected = format(current.get_span_context().trace_id, "032x")
        finally:
            logger.removeHandler(handler)

        outside, inside = (json.loads(line) for line in stream.getvalue().splitlines())
        self.assertNotIn("otel_trace_id", outside)
        self.assertEqual(inside["otel_trace_id"], expected)
        self.assertEqual(telemetry.current_otel_trace_id(), "")


if __name__ == "__main__":
    unittest.main()
