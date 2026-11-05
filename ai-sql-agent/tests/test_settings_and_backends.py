import os
import unittest
from contextlib import contextmanager

from ai_sql_agent.core import get_settings, reload_settings


@contextmanager
def env(**values):
    """临时设置环境变量并重新加载 Settings。"""
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield reload_settings()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()


class SettingsTest(unittest.TestCase):
    def test_openai_provider_derives_endpoint_from_base_url(self):
        with env(
            AI_SQL_MODEL_PROVIDER="openai",
            AI_SQL_OPENAI_API_KEY="sk-openai",
            OPENAI_BASE_URL="https://relay.example.com/v1",
            AI_SQL_CHAT_URL=None,
            AI_SQL_EMBEDDING_URL=None,
            AI_SQL_CHAT_MODEL=None,
        ) as settings:
            self.assertEqual(
                settings.chat_url, "https://relay.example.com/v1/chat/completions"
            )
            self.assertEqual(
                settings.embedding_url, "https://relay.example.com/v1/embeddings"
            )
            self.assertEqual(settings.resolved_chat_api_key(), "sk-openai")
            self.assertTrue(settings.has_model_credentials)

    def test_anthropic_provider_switches_endpoint_and_key(self):
        with env(
            AI_SQL_MODEL_PROVIDER="anthropic",
            AI_SQL_OPENAI_API_KEY="sk-openai",
            AI_SQL_ANTHROPIC_API_KEY="sk-ant",
            ANTHROPIC_BASE_URL="https://relay.example.com",
            AI_SQL_CHAT_URL=None,
            # 用空串而不是 None：None 只删环境变量，Settings 仍会读到项目根目录
            # .env 里钉死的型号，这条断言就变成了「你的 .env 配了什么」。
            AI_SQL_CHAT_MODEL="",
        ) as settings:
            self.assertEqual(settings.chat_url, "https://relay.example.com/v1/messages")
            self.assertEqual(settings.resolved_chat_api_key(), "sk-ant")
            self.assertTrue(settings.chat_model.startswith("claude"))
            # Embedding 没有 Anthropic 实现，始终留在 OpenAI 兼容通道
            self.assertTrue(settings.embedding_url.endswith("/embeddings"))
            self.assertEqual(settings.resolved_embedding_api_key(), "sk-openai")

    def test_explicit_chat_url_beats_provider_default(self):
        with env(
            AI_SQL_MODEL_PROVIDER="openai",
            OPENAI_BASE_URL="https://relay.example.com/v1",
            AI_SQL_CHAT_URL="http://vllm-chat:8000/v1/chat/completions",
        ) as settings:
            self.assertEqual(
                settings.chat_url, "http://vllm-chat:8000/v1/chat/completions"
            )

    def test_reads_plain_provider_variable_names(self):
        """裸 OPENAI_API_KEY / ANTHROPIC_API_KEY 也认，方便直接抄厂商文档。"""
        with env(
            AI_SQL_OPENAI_API_KEY=None,
            OPENAI_API_KEY="sk-plain",
            AI_SQL_MODEL_PROVIDER="openai",
        ) as settings:
            self.assertEqual(settings.resolved_chat_api_key(), "sk-plain")

    def test_ai_sql_prefix_takes_precedence(self):
        with env(
            OPENAI_API_KEY="sk-legacy", AI_SQL_OPENAI_API_KEY="sk-new"
        ) as settings:
            self.assertEqual(settings.openai_api_key, "sk-new")

    def test_execution_governance_defaults(self):
        # 1000 行 / 10 秒是一直实际生效的上限。这里原来钉的是 10000 / 30——那组数只进过
        # 启动日志，从没到过执行器（见 tests/test_execution_limits_setting.py）。
        with env(AI_SQL_MAX_ROWS=None, AI_SQL_MAX_EXECUTION_SECONDS=None) as settings:
            self.assertEqual(settings.max_rows, 1000)
            self.assertEqual(settings.max_execution_seconds, 10.0)

    def test_execution_governance_is_configurable(self):
        with env(AI_SQL_MAX_ROWS="50", AI_SQL_MAX_EXECUTION_SECONDS="2.5") as settings:
            self.assertEqual(settings.max_rows, 50)
            self.assertEqual(settings.max_execution_seconds, 2.5)

    def test_rerank_url_empty_without_workspace(self):
        with env(DASHSCOPE_WORKSPACE_ID=None, AI_SQL_RERANK_URL=None) as settings:
            self.assertEqual(settings.resolved_rerank_url(), "")


class VectorBackendFallbackTest(unittest.TestCase):
    def test_falls_back_to_memory_index_without_dsn(self):
        from ai_sql_agent.pipeline.local_clients import LocalHashEmbeddingClient
        from ai_sql_agent.retrieval.vector_backend import create_vector_index
        from ai_sql_agent.retrieval.vector_index import VectorIndex

        with env(AI_SQL_PGVECTOR_DSN=None):
            index = create_vector_index(LocalHashEmbeddingClient(), namespace="unit_test")

        self.assertIsInstance(index, VectorIndex)

    def test_falls_back_to_memory_index_when_pgvector_unreachable(self):
        from ai_sql_agent.pipeline.local_clients import LocalHashEmbeddingClient
        from ai_sql_agent.retrieval.vector_backend import create_vector_index
        from ai_sql_agent.retrieval.vector_index import VectorIndex

        # 指向一个不存在的实例：不应该抛异常，而是退回内存索引
        with env(
            AI_SQL_PGVECTOR_DSN="postgresql://nobody@127.0.0.1:1/none",
        ):
            index = create_vector_index(LocalHashEmbeddingClient(), namespace="unit_test")

        self.assertIsInstance(index, VectorIndex)


class MemoryVectorIndexFallbackTest(unittest.TestCase):
    DB_PATH = "runtime_data/test_memory_fallback.db"

    def _service(self, **config_kwargs):
        from ai_sql_agent.memory import ConversationMemoryService
        from ai_sql_agent.memory.conversation import MemoryServiceConfig

        return ConversationMemoryService(
            MemoryServiceConfig(db_path=self.DB_PATH, **config_kwargs)
        )

    def test_memory_service_runs_without_pgvector(self):
        with env(AI_SQL_PGVECTOR_DSN=None):
            service = self._service()
            try:
                self.assertIsNone(service.long_term.vector_index)
            finally:
                service.close()

    def test_backend_none_ignores_configured_dsn(self):
        with env(AI_SQL_PGVECTOR_DSN="postgresql://nobody@127.0.0.1:1/none"):
            service = self._service(vector_backend="none")
            try:
                self.assertIsNone(service.long_term.vector_index)
            finally:
                service.close()

    def test_backend_pgvector_without_dsn_fails_loudly(self):
        # 生产配置：显式要求 pgvector 时不允许静默降级
        with env(AI_SQL_PGVECTOR_DSN=None):
            with self.assertRaises(ValueError):
                self._service(vector_backend="pgvector")

    def test_backend_pgvector_unreachable_fails_loudly(self):
        with env(AI_SQL_PGVECTOR_DSN="postgresql://nobody@127.0.0.1:1/none"):
            with self.assertRaises(Exception):
                self._service(vector_backend="pgvector")

    def test_backend_auto_degrades_with_warning(self):
        with env(AI_SQL_PGVECTOR_DSN="postgresql://nobody@127.0.0.1:1/none"):
            service = self._service(vector_backend="auto")
            try:
                self.assertIsNone(service.long_term.vector_index)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()


class BenchmarkCredentialDetectionTest(unittest.TestCase):
    """评测 CLI 与发布门禁必须用 Settings 判断凭证，不能用 os.getenv。

    凭证常配在 .env 里，os.getenv 读不到 → 门禁会在「其实有 key」时
    悄悄降级成 4 条冒烟就放行，给出虚假信心。这是回归防护。
    """

    def test_cli_and_gate_do_not_use_os_getenv_for_credentials(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for name in ("run_benchmark.py", "gate.py"):
            source = (root / "benchmark" / name).read_text(encoding="utf-8")
            self.assertNotIn(
                'os.getenv("DASHSCOPE_API_KEY")', source,
                f"{name} 仍在用 os.getenv 判断凭证，会漏掉 .env 中的配置",
            )
            self.assertIn(
                "has_model_credentials", source,
                f"{name} 应通过 Settings.has_model_credentials 判断凭证",
            )


class BenchmarkRepeatModeTest(unittest.TestCase):
    """评测支持重复跑并报告波动区间。

    模型输出不完全确定：实测同一份代码、同一批 25 条跨库题，
    准确率在 16%~36% 之间波动。单次跑分不能用来判断改动是否有效，
    因此 CLI 必须提供 --repeat 并显式报告区间。
    """

    def test_cli_exposes_repeat_flag(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "benchmark" / "run_benchmark.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"--repeat"', source)
        self.assertIn("_run_repeated", source)

    def test_repeated_summary_reports_range(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "benchmark" / "run_benchmark.py"
        ).read_text(encoding="utf-8")
        # 必须同时给出均值与区间，只报均值会掩盖波动
        self.assertIn("均值", source)
        self.assertIn("区间", source)
        self.assertIn("跨度", source)
