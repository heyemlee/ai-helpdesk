"""Schema 预算感知：按相关性丢字段，而不是按字符位置切文本。

原先的预算裁剪是对渲染好的整段文本做字符级截断的——超预算时它砍掉的是
「排在后面的字符」，不是「最不相关的字段」，一张表可能被从中间切断。
实测后果：规划模型写「Schema 未提供该字段及完整表结构」，
然后生成 `SELECT project_code FROM project WHERE 0`。
5 条召回缺失里 4 条是这么丢的（retrieval_recall_hit=True 而 recall_hit=False）。
"""

import unittest

from ai_sql_agent.pipeline.schema_budget import MIN_COLUMNS_PER_TABLE, fit_graphs_to_budget
from ai_sql_agent.pipeline.token_budget import estimate_tokens
from ai_sql_agent.retrieval.objects import (
    ColumnSchema, SchemaGraph, TableRelation, TableSchema,
)


def _column(name, database="pm_db", table="project"):
    return ColumnSchema(
        database=database, table_name=table, column_name=name, data_type="TEXT",
        description="字段含义说明" * 6, aliases=["别名一", "别名二"],
        semantic_role="dimension", business_usage="业务用途说明" * 6,
    )


def _graph(database="pm_db", table="project", n=12, relations=None):
    return SchemaGraph(
        database=database,
        tables={table: TableSchema(database=database, table_name=table)},
        columns={table: [_column(f"c{i}", database, table) for i in range(n)]},
        relations=relations or [],
    )


def _kept(sections):
    return [line.split("：", 1)[1] for line in "\n".join(sections).split("\n")
            if line.startswith("- 字段名：")]


class DegradationLadderTest(unittest.TestCase):
    def test_within_budget_keeps_full_detail(self):
        sections, info = fit_graphs_to_budget([_graph()], 100_000)
        self.assertFalse(info["compact"])
        self.assertEqual(info["dropped"], 0)
        self.assertIn("业务用途", "\n".join(sections))

    def test_first_degrades_precision_not_columns(self):
        """先降描述精度：字段进不进得来，比描述得多详细重要。"""
        graph = _graph(n=12)
        full = estimate_tokens("\n".join(g for g in [graph.to_prompt_context()]))
        sections, info = fit_graphs_to_budget([graph], int(full * 0.7))
        self.assertTrue(info["compact"])
        self.assertEqual(info["dropped"], 0, "还没到该丢字段的时候")
        self.assertEqual(len(_kept(sections)), 12)

    def test_then_drops_least_relevant_columns(self):
        sections, info = fit_graphs_to_budget([_graph(n=12)], 250)
        self.assertTrue(info["compact"])
        self.assertGreater(info["dropped"], 0)
        kept = _kept(sections)
        self.assertIn("c0", kept, "最相关的字段不能被丢")
        self.assertNotIn("c11", kept, "最不相关的字段应当先丢")

    def test_result_actually_fits_the_budget(self):
        sections, _ = fit_graphs_to_budget([_graph(n=12)], 250)
        self.assertLessEqual(sum(estimate_tokens(s) for s in sections), 250)


class ProtectionTest(unittest.TestCase):
    def test_each_table_keeps_a_floor(self):
        """一张表被丢到只剩表名，它在 Prompt 里就没有可用信息了。"""
        sections, _ = fit_graphs_to_budget([_graph(n=12)], 40)
        self.assertGreaterEqual(len(_kept(sections)), MIN_COLUMNS_PER_TABLE)

    def test_join_keys_are_never_dropped(self):
        """join key 丢了就没法关联，任何预算下都要保留。"""
        graph = _graph(n=12)
        graph.relations = [TableRelation(
            database="pm_db",
            source_table="project", source_column="c9",
            target_table="project", target_column="c10",
        )]
        sections, info = fit_graphs_to_budget([graph], 250)
        kept = _kept(sections)
        self.assertIn("c9", kept)
        self.assertIn("c10", kept)

    def test_dropped_columns_are_reported_not_silent(self):
        """丢了什么必须留痕，否则只会看到一句莫名其妙的「Schema 未提供该字段」。"""
        _, info = fit_graphs_to_budget([_graph(n=12)], 250)
        self.assertEqual(len(info["dropped_columns"]), info["dropped"])
        self.assertTrue(all("." in name for name in info["dropped_columns"]))

    def test_retrieval_result_is_not_mutated(self):
        """裁剪只能作用于副本，不能把检索结果改掉——召回率还要用它。"""
        graph = _graph(n=12)
        fit_graphs_to_budget([graph], 250)
        self.assertEqual(len(graph.columns["project"]), 12)

    def test_empty_input_is_safe(self):
        self.assertEqual(fit_graphs_to_budget([], 1000)[0], [])
