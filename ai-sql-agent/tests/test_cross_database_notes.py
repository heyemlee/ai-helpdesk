"""跨库约束注入。

跨库准确率长期只有 ~27%，诊断出两个失败模式：
1. 模型把两个库压进一条 SQL（执行报 no such table）；
2. 模型留在单库里用同名字段硬凑——问「采购总金额」却用了 contract_amt
   （合同额），SQL 跑通、结果是错的，更危险。

把「库物理隔离 / 已知关联键 / 指标口径」显式写进 Schema 上下文后，
实测跨库类目从 ~27% 提到 ~54%。这里锁住这段内容不被删改。
"""

import unittest

from ai_sql_agent.pipeline import Text2SQLPipeline
from ai_sql_agent.pipeline.demo_data import (
    get_cross_database_links,
    get_metric_disambiguation,
)


class CrossDatabaseDeclarationTest(unittest.TestCase):
    def test_links_reference_existing_columns(self):
        """声明的关联键必须指向真实存在的库表字段。"""
        pipeline = Text2SQLPipeline()
        valid = set()
        for database in pipeline.databases:
            meta = pipeline.business_metas.get(database.name, {})
            for table, table_meta in meta.items():
                for column in table_meta.get("columns", {}):
                    valid.add(f"{database.name}.{table}.{column}")

        for link in get_cross_database_links():
            self.assertIn(link["left"], valid, f"关联键左侧不存在：{link['left']}")
            self.assertIn(link["right"], valid, f"关联键右侧不存在：{link['right']}")

    def test_links_are_marked_as_inferred_not_foreign_keys(self):
        """必须标明是命名约定推断，不能让人误以为是数据库外键。"""
        for link in get_cross_database_links():
            self.assertEqual(link["confidence"], "naming_convention")

    def test_metric_disambiguation_covers_the_known_confusion(self):
        """采购金额 vs 合同额是实测中最高频的选错，必须覆盖。"""
        terms = " ".join(item["term"] for item in get_metric_disambiguation())
        corrects = " ".join(item["correct"] for item in get_metric_disambiguation())
        self.assertIn("采购", terms)
        self.assertIn("po_amt", corrects)


class CrossDatabaseNotesInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Text2SQLPipeline()

    def test_notes_are_injected_for_multi_database_retrieval(self):
        result = self.pipeline.run("查询实际成本超过预算的项目的采购总金额")
        context = result.schema_context
        self.assertIn("跨库约束", context)
        self.assertIn("禁止在一条 SQL 里引用多个库的表", context)
        self.assertIn("project_code", context)
        self.assertIn("job_no", context)

    def test_notes_survive_token_budget_trimming(self):
        """跨库约束必须在预算裁剪之后追加，否则 Schema 一长就先被裁掉。"""
        result = self.pipeline.run("查询实际成本超过预算的项目的采购总金额")
        # 约束段落应出现在上下文尾部
        index = result.schema_context.index("跨库约束")
        self.assertGreater(index, len(result.schema_context) * 0.5)

    def test_single_database_graph_emits_no_notes(self):
        """只有一个库贡献字段时不应注入跨库约束（纯噪声）。"""
        service = self.pipeline.schema_retrieval_service
        self.assertEqual(service._build_cross_database_notes(["erp_db"]), "")
        self.assertEqual(service._build_cross_database_notes([]), "")

    def test_notes_only_list_links_between_selected_databases(self):
        service = self.pipeline.schema_retrieval_service
        notes = service._build_cross_database_notes(["pm_db", "mes_db"])
        self.assertIn("mes_db.work_order.job_no", notes)
        # erp 未参与本次检索，它的关联键不该出现
        self.assertNotIn("erp_db.purchase_order.job_no", notes)


if __name__ == "__main__":
    unittest.main()
