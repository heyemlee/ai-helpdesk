"""Anthropic Messages 协议接入验证。

主链路默认走 OpenAI 兼容协议，但 `AI_SQL_MODEL_PROVIDER=anthropic` 时
换成 Anthropic Messages 协议：POST /v1/messages、`x-api-key` +
`anthropic-version` 请求头、system 提到顶层、`max_tokens` 必填、
响应体是 content 块数组。

这里起一个最小的 Messages 服务作为被测端点，验证只改 .env 就能整体切换，
而不是仅在文档里声称支持。
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ai_sql_agent.core import build_chat_payload, extract_chat_content, reload_settings
from ai_sql_agent.core.transport import _iter_sse_content


RECEIVED = []


def _answer_for(prompt: str) -> str:
    """按 Prompt 特征返回该阶段应有的输出，模拟真实模型行为。"""
    if "字段解析助手" in prompt or "解析结果" in prompt:
        return "解析结果：准时率，供应商名称"
    if "route" in prompt or "意图" in prompt:
        return '{"route": "database_query", "confidence": 0.95, "reason": "查询意图"}'
    if "SQL生成助手" in prompt or ("输出目标" in prompt and "方言约束" in prompt):
        return "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9;"
    return """步骤1：
(
  数据库: erp_db,
  处理对象: supplier.on_time_rate，supplier.supplier_name,
  操作指令: 在supplier表中筛选on_time_rate低于0.9的记录，输出supplier_name,
  输出目标: supplier.supplier_name,
  预期输出量级: 数条
)"""


class _AnthropicMessagesHandler(BaseHTTPRequestHandler):
    """最小 Anthropic Messages 实现：POST /v1/messages。"""

    def log_message(self, *args):  # 静音测试输出
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        RECEIVED.append({
            "path": self.path,
            "api_key": self.headers.get("x-api-key", ""),
            "version": self.headers.get("anthropic-version", ""),
            "authorization": self.headers.get("Authorization", ""),
            "model": request.get("model"),
            "body": request,
        })

        if not self.path.endswith("/messages"):
            self._reply({"type": "error"}, status=404)
            return

        prompt = request["messages"][-1]["content"]
        self._reply({
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": request.get("model"),
            "content": [{"type": "text", "text": _answer_for(prompt)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AnthropicPayloadTest(unittest.TestCase):
    """请求体与响应体的协议差异都收在 transport 里，这里单测这层转换。"""

    def test_system_message_is_lifted_and_max_tokens_required(self):
        payload = build_chat_payload(
            [
                {"role": "system", "content": "你是字段解析助手"},
                {"role": "user", "content": "用户Query：准时率"},
            ],
            model="claude-sonnet-5",
            temperature=0.0,
            provider="anthropic",
            max_tokens=1024,
        )
        self.assertEqual(payload["system"], "你是字段解析助手")
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])

    def test_claude_5_payload_omits_deprecated_temperature(self):
        # Claude 5 起 temperature 被废弃，带上它整个请求会被 400 拒绝
        payload = build_chat_payload(
            [{"role": "user", "content": "x"}],
            model="claude-sonnet-5",
            temperature=0.0,
            provider="anthropic",
            max_tokens=64,
        )
        self.assertNotIn("temperature", payload)

    def test_older_claude_payload_still_carries_temperature(self):
        # 老模型仍然接受，全链路靠 temperature=0 拿确定性输出，不能一刀切删掉
        payload = build_chat_payload(
            [{"role": "user", "content": "x"}],
            model="claude-haiku-4-5",
            temperature=0.0,
            provider="anthropic",
            max_tokens=64,
        )
        self.assertEqual(payload["temperature"], 0.0)

    def test_openai_payload_keeps_system_in_messages(self):
        payload = build_chat_payload(
            [
                {"role": "system", "content": "你是字段解析助手"},
                {"role": "user", "content": "用户Query：准时率"},
            ],
            model="gpt-5.6-luna",
            provider="openai",
        )
        self.assertNotIn("system", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])

    def test_extract_content_from_message_blocks(self):
        result = {
            "content": [{"type": "text", "text": " SELECT 1; "}],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        self.assertEqual(
            extract_chat_content(result, component="coder", model="m"), "SELECT 1;"
        )

    def test_stream_parses_content_block_delta(self):
        lines = [
            'data: {"type":"message_start","message":{"id":"msg_1"}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"步骤"}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"1："}}',
            'data: {"type":"message_stop"}',
        ]
        self.assertEqual(list(_iter_sse_content(iter(lines))), ["步骤", "1："])


class AnthropicEndpointTest(unittest.TestCase):
    """把 provider 切成 anthropic，验证请求头、路径与全链路可用。"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _AnthropicMessagesHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        reload_settings()

    def setUp(self):
        RECEIVED.clear()

    def _point_settings_at_mock(self):
        """等价于生产里换厂商：只改 .env 里的 provider 与凭证。"""
        import os
        # 用 AI_SQL_ 前缀写：它优先级高于裸 ANTHROPIC_API_KEY，
        # 不会被 conftest 的「测试期强制离线」置空遮住
        os.environ["AI_SQL_MODEL_PROVIDER"] = "anthropic"
        os.environ["AI_SQL_ANTHROPIC_API_KEY"] = "sk-ant-local"
        os.environ["AI_SQL_ANTHROPIC_BASE_URL"] = self.base
        os.environ["AI_SQL_CHAT_MODEL"] = "claude-sonnet-5"
        os.environ["AI_SQL_CODER_MODEL"] = "claude-sonnet-5"
        os.environ.pop("AI_SQL_CHAT_URL", None)
        return reload_settings()

    def _restore(self):
        import os
        for key in ("AI_SQL_MODEL_PROVIDER", "AI_SQL_ANTHROPIC_API_KEY",
                    "AI_SQL_ANTHROPIC_BASE_URL", "AI_SQL_CHAT_MODEL",
                    "AI_SQL_CODER_MODEL"):
            os.environ.pop(key, None)
        os.environ["AI_SQL_ANTHROPIC_API_KEY"] = ""  # 交还给 conftest 的离线态
        reload_settings()

    def test_settings_switch_to_messages_endpoint(self):
        try:
            settings = self._point_settings_at_mock()
            self.assertEqual(settings.chat_url, f"{self.base}/v1/messages")
            self.assertEqual(settings.resolved_chat_api_key(), "sk-ant-local")
            self.assertTrue(settings.has_model_credentials)
        finally:
            self._restore()

    def test_chat_over_messages_protocol(self):
        """协议层：路径、鉴权头、system 位置、max_tokens 都符合 Messages 规范。"""
        try:
            self._point_settings_at_mock()
            from ai_sql_agent.planning.thinking_client import (
                ThinkingModelClient, ThinkingModelConfig,
            )

            client = ThinkingModelClient(ThinkingModelConfig())
            output = client.generate("处理对象 输出目标 supplier.on_time_rate")

            self.assertIn("步骤1", output)
            self.assertEqual(len(RECEIVED), 1)
            call = RECEIVED[0]
            self.assertTrue(call["path"].endswith("/v1/messages"))
            self.assertEqual(call["api_key"], "sk-ant-local")
            self.assertEqual(call["version"], "2023-06-01")
            self.assertEqual(call["authorization"], "")
            self.assertEqual(call["model"], "claude-sonnet-5")
            self.assertIn("max_tokens", call["body"])
        finally:
            self._restore()

    def test_full_pipeline_over_messages_protocol(self):
        """链路层：整条 Text2SQL 链路跑在 Anthropic 协议上。"""
        try:
            self._point_settings_at_mock()
            from ai_sql_agent.pipeline import Text2SQLPipeline

            pipeline = Text2SQLPipeline()
            result = pipeline.run("查询准时率低于0.9的供应商名称")

            self.assertTrue(result.step_logs, "应产生执行步骤")
            log = result.step_logs[-1]
            self.assertTrue(
                log.execution_result["success"], log.execution_result.get("error")
            )
            self.assertIn("supplier_name", (log.final_sql or log.sql))

            self.assertTrue(RECEIVED, "应发生真实 HTTP 调用")
            self.assertTrue(all(c["api_key"] == "sk-ant-local" for c in RECEIVED))
        finally:
            self._restore()


if __name__ == "__main__":
    unittest.main()
