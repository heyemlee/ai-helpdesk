"""字段层重排的模型名要跟着 Settings 走，不能在流水线里写死。

流水线原来写死 model="qwen-rerank"，而 Settings 默认是 qwen3-rerank，
设 AI_SQL_RERANK_MODEL 也不生效。
"""
import unittest

from ai_sql_agent.core import get_settings
from ai_sql_agent.pipeline.text2sql_pipeline import build_rerank_client


class RerankModelComesFromSettingsTest(unittest.TestCase):
    def test_default_model_is_the_settings_value(self):
        self.assertEqual(build_rerank_client().config.model, get_settings().rerank_model)

    def test_changing_the_setting_changes_the_model(self):
        settings = get_settings()
        original = settings.rerank_model
        settings.rerank_model = "rerank-model-under-test"
        try:
            self.assertEqual(build_rerank_client().config.model, "rerank-model-under-test")
        finally:
            settings.rerank_model = original


if __name__ == "__main__":
    unittest.main()
