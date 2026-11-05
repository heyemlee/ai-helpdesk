"""OpenAI 兼容端点接入验证。

本项目的模型调用走 OpenAI 兼容协议（POST JSON + Authorization: Bearer），
地址由 AI_SQL_CHAT_URL / AI_SQL_EMBEDDING_URL 配置。这意味着模型服务可以是：

- 公网 API（阿里云百炼 compatible-mode 等）；
- 内网自建推理服务（vLLM 的 OpenAI server、SGLang、Ollama 等）。

这里起一个最小的 OpenAI 兼容 HTTP 服务作为被测端点，验证换成自建地址后
全链路真的能跑通，而不是仅在文档里声称兼容。
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ai_sql_agent.core import reload_settings


RECEIVED = []


class _OpenAICompatibleHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容实现：/v1/chat/completions 与 /v1/embeddings。"""

    def log_message(self, *args):  # 静音测试输出
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        RECEIVED.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "model": request.get("model"),
            "body": request,
        })

        if self.path.endswith("/chat/completions"):
            prompt = request["messages"][-1]["content"]
            self._reply({
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": request.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": _answer_for(prompt)},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            })
            return

        if self.path.endswith("/embeddings"):
            texts = request.get("input") or []
            self._reply({
                "object": "list",
                "model": request.get("model"),
                "data": [
                    {"object": "embedding", "index": i,
                     "embedding": _fake_vector(text, request.get("dimensions", 1024))}
                    for i, text in enumerate(texts)
                ],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })
            return

        self._reply({"error": "not found"}, status=404)


def _fake_vector(text: str, dim: int):
    """确定性伪向量，保证同文本同向量。"""
    vec = [0.0] * dim
    for token in str(text).split():
        vec[hash(token) % dim] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


def _answer_for(prompt: str) -> str:
    """按 Prompt 特征返回该阶段应有的输出，模拟真实模型行为。"""
    # 关键词抽取
    if "字段解析助手" in prompt or "解析结果" in prompt:
        return "解析结果：准时率，供应商名称"
    # 意图路由（要求返回 JSON）
    if "route" in prompt or "意图" in prompt:
        return '{"route": "database_query", "confidence": 0.95, "reason": "查询意图"}'
    # SQL 生成
    if "SQL生成助手" in prompt or ("输出目标" in prompt and "方言约束" in prompt):
        return "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9;"
    # CoT 规划
    return """步骤1：
(
  数据库: erp_db,
  处理对象: supplier.on_time_rate，supplier.supplier_name,
  操作指令: 在supplier表中筛选on_time_rate低于0.9的记录，输出supplier_name,
  输出目标: supplier.supplier_name,
  预期输出量级: 数条
)"""


class OpenAICompatibleEndpointTest(unittest.TestCase):
    """把模型服务地址指向自建 OpenAI 兼容服务，验证全链路可用。"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _OpenAICompatibleHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        reload_settings()

    def setUp(self):
        RECEIVED.clear()

    def _point_settings_at_mock(self):
        """等价于生产里指向 vLLM：只改地址与凭证，不改任何代码。"""
        import os
        os.environ["AI_SQL_CHAT_URL"] = f"{self.base}/chat/completions"
        os.environ["AI_SQL_EMBEDDING_URL"] = f"{self.base}/embeddings"
        os.environ["AI_SQL_OPENAI_API_KEY"] = "sk-local-vllm"
        os.environ["AI_SQL_CHAT_MODEL"] = "Qwen2.5-14B-Instruct"
        os.environ["AI_SQL_CODER_MODEL"] = "Qwen2.5-Coder-14B-Instruct"
        return reload_settings()

    def _restore(self):
        import os
        for key in ("AI_SQL_CHAT_URL", "AI_SQL_EMBEDDING_URL",
                    "AI_SQL_OPENAI_API_KEY", "AI_SQL_CHAT_MODEL",
                    "AI_SQL_CODER_MODEL"):
            os.environ.pop(key, None)
        reload_settings()

    def test_settings_accept_self_hosted_endpoint(self):
        """配置层：地址与模型名可整体切到自建服务。"""
        try:
            settings = self._point_settings_at_mock()
            self.assertTrue(settings.chat_url.startswith("http://127.0.0.1"))
            self.assertTrue(settings.embedding_url.startswith("http://127.0.0.1"))
            self.assertEqual(settings.chat_model, "Qwen2.5-14B-Instruct")
            self.assertEqual(settings.coder_model, "Qwen2.5-Coder-14B-Instruct")
            self.assertTrue(settings.has_model_credentials)
        finally:
            self._restore()

    def test_chat_completion_over_openai_protocol(self):
        """协议层：按 OpenAI 格式发请求、解析响应，鉴权头正确。"""
        try:
            self._point_settings_at_mock()
            from ai_sql_agent.planning.thinking_client import ThinkingModelClient, ThinkingModelConfig

            client = ThinkingModelClient(ThinkingModelConfig())
            output = client.generate("处理对象 输出目标 supplier.on_time_rate")

            self.assertIn("步骤1", output)
            self.assertIn("erp_db", output)

            self.assertEqual(len(RECEIVED), 1)
            call = RECEIVED[0]
            self.assertTrue(call["path"].endswith("/v1/chat/completions"))
            self.assertEqual(call["auth"], "Bearer sk-local-vllm")
            self.assertEqual(call["model"], "Qwen2.5-14B-Instruct")
            self.assertIn("messages", call["body"])
        finally:
            self._restore()

    def test_embedding_over_openai_protocol(self):
        """协议层：Embedding 走同一套 OpenAI 兼容格式。"""
        try:
            self._point_settings_at_mock()
            from ai_sql_agent.indexing.embedding_client import (
                AliyunEmbeddingClient, AliyunEmbeddingConfig,
            )

            client = AliyunEmbeddingClient(AliyunEmbeddingConfig(dimensions=1024))
            vectors = client.embed_texts(["采购金额", "供应商名称"])

            self.assertEqual(vectors.shape, (2, 1024))
            embed_calls = [c for c in RECEIVED if c["path"].endswith("/embeddings")]
            self.assertEqual(len(embed_calls), 1)
            self.assertEqual(embed_calls[0]["auth"], "Bearer sk-local-vllm")
            self.assertEqual(embed_calls[0]["body"]["input"], ["采购金额", "供应商名称"])
        finally:
            self._restore()

    def test_full_pipeline_against_self_hosted_endpoint(self):
        """链路层：整条 Text2SQL 链路跑在自建端点上，产出可执行 SQL 与真实结果。"""
        try:
            self._point_settings_at_mock()
            from ai_sql_agent.pipeline import Text2SQLPipeline

            pipeline = Text2SQLPipeline()
            result = pipeline.run("查询准时率低于0.9的供应商名称")

            self.assertTrue(result.step_logs, "应产生执行步骤")
            log = result.step_logs[-1]
            self.assertTrue(log.execution_result["success"], log.execution_result.get("error"))
            self.assertGreater(log.execution_result["row_count"], 0)
            self.assertIn("supplier_name", (log.final_sql or log.sql))

            # 确认确实打到了自建端点，而不是走了离线 mock 分支
            self.assertTrue(RECEIVED, "应发生真实 HTTP 调用")
            self.assertTrue(
                all(c["auth"] == "Bearer sk-local-vllm" for c in RECEIVED)
            )
        finally:
            self._restore()


if __name__ == "__main__":
    unittest.main()
