"""检索泛化：留出集（口语化问法，不参与任何调参）。

为什么需要这个测试：评测集上的召回率会被调参推高——短语表、别名、
枚举词表都是对着评测集补的。留出集用完全不同的口语说法，用来量化
「这个数字有多少是调出来的」。

**这个文件里的问法不允许被用来反向补短语表或别名**，否则它就失去了
留出集的意义，只是把过拟合搬了个地方。
"""

import json
import unittest
from pathlib import Path

from ai_sql_agent.pipeline import Text2SQLPipeline

HOLDOUT = Path(__file__).resolve().parents[1] / "benchmark" / "holdout.json"

# 离线（规则抽词）下的基线，40 条实测 57.5%（离线无随机性，可精确复现）。
# 设为下限而非精确值：允许改进，只拦住退化。
MIN_OFFLINE_RECALL = 0.50


class RecallGeneralizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = json.loads(HOLDOUT.read_text(encoding="utf-8"))
        cls.pipeline = Text2SQLPipeline()

    def test_holdout_recall_does_not_regress(self):
        hit = 0
        for item in self.items:
            result = self.pipeline.run(item["query"])
            retrieved = set(result.retrieved_columns)
            if all(c in retrieved for c in item["gold_columns"]):
                hit += 1

        recall = hit / len(self.items)
        self.assertGreaterEqual(
            recall,
            MIN_OFFLINE_RECALL,
            f"留出集召回率 {recall:.1%} 低于下限 {MIN_OFFLINE_RECALL:.0%}，检索出现退化",
        )

    def test_schema_graph_stays_focused_on_unseen_queries(self):
        """未见问法下 Schema 图也不能退化成全量 Schema。"""
        for item in self.items[:5]:
            result = self.pipeline.run(item["query"])
            self.assertLess(
                len(result.retrieved_columns), 45,
                f"「{item['query']}」进图字段过多，检索失去聚焦意义",
            )

    def test_holdout_is_large_enough_to_gate_on(self):
        """
        门禁线是 85%。15 条时一条题 = 6.7pp，一条题就能翻转门禁结论，
        这样的门禁既挡不住真实退化，也会被噪声误伤。40 条时一条题 = 2.5pp。
        """
        self.assertGreaterEqual(
            len(self.items), 40, "留出集少于 40 条，不足以支撑 85% 的门禁线"
        )

    def test_holdout_covers_every_table(self):
        """留出集要覆盖到每张表，否则「泛化」只是在少数几张表上泛化。"""
        tables = {".".join(column.split(".")[:2])
                  for item in self.items for column in item["gold_columns"]}
        expected = {
            f"{database.name}.{table}"
            for database in self.pipeline.databases
            for table in self.pipeline.business_metas.get(database.name, {})
        }
        self.assertEqual(expected - tables, set(), "有表没被留出集问到")

    def test_holdout_gold_columns_are_wellformed(self):
        """留出集的 gold 字段必须指向真实存在的库表字段。"""
        valid = set()
        for database in self.pipeline.databases:
            meta = self.pipeline.business_metas.get(database.name, {})
            for table, table_meta in meta.items():
                for column in table_meta.get("columns", {}):
                    valid.add(f"{database.name}.{table}.{column}")

        for item in self.items:
            for column in item["gold_columns"]:
                self.assertIn(column, valid, f"{item['id']} 的 gold 字段不存在：{column}")


if __name__ == "__main__":
    unittest.main()
