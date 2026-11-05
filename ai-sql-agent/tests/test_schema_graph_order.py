"""Schema 图里表的先后。

三层检索里，库内的表按表级检索的排名排，也就是 stage.retrieval 日志里 tables 字段的顺序；
单独调用 build_schema_graph、不给排名时，按字段命中的先后排。两种都与字符串哈希种子无关。
模型面整体跨进程一致由 `test_prompt_snapshot.PromptsIgnoreHashSeedTest` 守着；这里守的是
排出来的是哪一种顺序。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.core import get_settings
from ai_sql_agent.retrieval.graph_builder import build_schema_graph
from ai_sql_agent.retrieval.objects import ColumnSchema, SchemaHit, TableSchema

_TABLES = ("alpha", "mid", "zeta")
# 检索排序：zeta 的字段最相关，其次 alpha，再次 mid。
_RANKED = [("zeta", "name"), ("alpha", "id"), ("mid", "name"), ("alpha", "name"), ("zeta", "id")]


def _column(table, name):
    return ColumnSchema(database="erp_db", table_name=table, column_name=name, data_type="TEXT")


def _graph(table_order=None):
    hits = [
        SchemaHit(doc_id=f"erp_db.{table}.{name}", score=1.0 - index / 10,
                  column=_column(table, name))
        for index, (table, name) in enumerate(_RANKED)
    ]
    return build_schema_graph(
        hits,
        tables={name: TableSchema(database="erp_db", table_name=name) for name in _TABLES},
        all_columns=[_column(table, name) for table in _TABLES for name in ("id", "name")],
        relations=[],
        table_order=table_order,
    )


def _rendered_tables(text):
    """{库: [表, ...]}，按 Schema 文本里出现的先后。"""
    order, database = {}, ""
    for line in text.splitlines():
        if line.startswith("数据库："):
            database = line.split("：", 1)[1]
        elif line.startswith("表名："):
            order.setdefault(database, []).append(line.split("：", 1)[1])
    return order


class TableOrderTest(unittest.TestCase):
    def test_tables_follow_the_table_ranking(self):
        graph = _graph(table_order=["mid", "alpha", "zeta"])

        self.assertEqual(list(graph.tables), ["mid", "alpha", "zeta"])
        self.assertEqual(_rendered_tables(graph.to_prompt_context()),
                         {"erp_db": ["mid", "alpha", "zeta"]})
        # 只动表的先后：表内字段仍按命中先后
        self.assertEqual([column.column_name for column in graph.columns["alpha"]], ["id", "name"])

    def test_tables_missing_from_the_ranking_go_last_in_hit_order(self):
        self.assertEqual(list(_graph(table_order=["mid"]).tables), ["mid", "zeta", "alpha"])

    def test_without_a_ranking_tables_follow_their_best_ranked_hit(self):
        # alpha 的第二个字段排在 mid 之后，不影响 alpha 排在 mid 前面。
        self.assertEqual(list(_graph().tables), ["zeta", "alpha", "mid"])


# 2026-10-16 门禁里这题抽出的关键词。本地精排下 stock_snapshot 的字段全排在 material 前面，
# 表级检索却把 material 排第一：按字段命中排和按表级排名排，在这题上结果不同。
_QUESTION = "查询板材类物料的在库数量"
_KEYWORDS = ["板材类", "物料", "在库数量", "物料品类", "物料名称", "板材"]

_CHILD = """
import json, sys
from ai_sql_agent.core import reload_settings
from ai_sql_agent.core.settings import SqlAgentSettings
SqlAgentSettings.model_config["env_file"] = None
reload_settings()
from ai_sql_agent.pipeline import Text2SQLPipeline
result = Text2SQLPipeline().run(sys.argv[2], keywords=json.loads(sys.argv[3]))
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"schema_context": result.schema_context, "ranking": result.selected_tables},
              handle, ensure_ascii=False)
"""


def _render_in_subprocess(hash_seed):
    """在 PYTHONHASHSEED=hash_seed 的子进程里用 Mock 模型跑一遍，返回 Schema 上下文和表级排名。"""
    with tempfile.TemporaryDirectory() as work_dir:
        out_path = Path(work_dir) / "result.json"
        env = dict(os.environ)
        env.update({
            "PYTHONHASHSEED": hash_seed,
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "AI_SQL_LOG_LEVEL": "ERROR",
            "AI_SQL_MODEL_SETTINGS_ENABLED": "false",
        })
        for key in ("AI_SQL_OPENAI_API_KEY", "OPENAI_API_KEY",
                    "AI_SQL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
            env[key] = ""
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, str(out_path), _QUESTION,
             json.dumps(_KEYWORDS, ensure_ascii=False)],
            cwd=work_dir, env=env, capture_output=True, text=True, timeout=300,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"子进程失败（PYTHONHASHSEED={hash_seed}）：\n{completed.stderr[-4000:]}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


class TableTierOrderTest(unittest.TestCase):
    """三层检索：库内表的先后就是表级检索的排名，换哈希种子也不变。"""

    def setUp(self):
        if get_settings().has_model_credentials:
            self.skipTest("只在离线 Mock 模型下有意义；当前环境配置了模型凭证")

    def test_tables_follow_the_table_tier_ranking_under_two_seeds(self):
        first, second = _render_in_subprocess("1"), _render_in_subprocess("2")
        self.assertEqual(first, second, "换了哈希种子，Schema 上下文或表级排名变了")

        rendered = _rendered_tables(first["schema_context"])
        for database, tables in rendered.items():
            ranked = [name for name in first["ranking"].get(database, []) if name in tables]
            self.assertEqual(tables[:len(ranked)], ranked, database)
        self.assertEqual(rendered["wms_db"], ["material", "stock_snapshot", "batch"])


if __name__ == "__main__":
    unittest.main()
