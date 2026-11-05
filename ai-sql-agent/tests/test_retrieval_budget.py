"""
进图字段占比（门禁线 40%）的两个旋钮：每库保留的字段数、每表保底字段数。

它们跟着 Schema 检索实际用的向量走：远端 Embedding 收紧，本地哈希向量（CI、远端调不通时的兜底）
保持原来的宽预算——收紧会让本地的留出集召回从 55% 掉到 47.5%。
"""
import unittest


class RetrievalBudgetTest(unittest.TestCase):
    """预算按实际在用的向量客户端选，不按配置里写的后端。"""

    def test_the_budget_follows_the_embedding_actually_in_use(self):
        from types import SimpleNamespace

        from ai_sql_agent.pipeline.text2sql_pipeline import column_budget_for

        self.assertEqual(column_budget_for(SimpleNamespace(backend="remote")), (10, 3))
        self.assertEqual(column_budget_for(SimpleNamespace(backend="local")), (20, 4))
        self.assertEqual(column_budget_for(object()), (20, 4), "说不清是哪种向量时按本地的宽预算")

    def test_the_offline_pipeline_keeps_the_wide_budget(self):
        """没配远端（CI、远端调不通退回本地）时，保底字段不能跟着收紧——留出集召回会掉。"""
        from ai_sql_agent.pipeline import Text2SQLPipeline

        config = Text2SQLPipeline().schema_retrieval_service.config.column_config
        self.assertEqual((config.rrf_config.final_top_k, config.min_columns_per_table), (20, 4))


if __name__ == "__main__":
    unittest.main()
