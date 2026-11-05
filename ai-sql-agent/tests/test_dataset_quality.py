"""评测集自身的质量守卫。

评测集是发布门禁的地基。地基有缺陷时，指标测出来的是「模型有没有猜中出题人的
习惯」，不是「答得对不对」——而且这种缺陷会给准确率设一个再优化也过不去的天花板。

真实踩到的一例：q101「查询实际成本超过预算的项目的采购总金额」gold 是
`GROUP BY job_no`（逐项目），q115「查询准时率低于0.8的供应商的入库总量」gold 是
单个 `SUM`（不分组）。**同一种「<筛选>的<实体>的<聚合量>」句式，两条 gold 方向相反。**
模型两次都判错了形状，但题面本身没有可判的依据——两种读法都成立。

修法是把问句改到只有一种读法，**以 gold 为准**；反过来把 gold 改成模型的输出就是刷分。
下面的测试守住这条规则，防止以后新增题目时再引入同样的歧义。
"""

import json
import re
import unittest
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "benchmark" / "dataset.json"

AGGREGATE = re.compile(r"\b(SUM|COUNT|AVG)\s*\(", re.I)
GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.I)

# 「<筛选>的<实体>的<聚合量>」——两个「的」夹一个聚合词，形状全靠读者猜
AMBIGUOUS_SHAPE = re.compile(r"的.+的.*(总金额|总量|总数|数量|平均)")

# 明确要求逐实体拆开 / 明确要求汇总成一个数
PER_ENTITY_MARKERS = ("各", "每个", "每家", "每张", "分别", "按")
TOTAL_MARKERS = ("合计", "总共", "一共")


class DatasetShapeAmbiguityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = json.loads(DATASET.read_text(encoding="utf-8"))

    def test_aggregate_questions_state_their_output_shape(self):
        offenders = []
        for item in self.items:
            gold, query = item["gold_sql"], item["query"]
            if not AGGREGATE.search(gold) or not AMBIGUOUS_SHAPE.search(query):
                continue
            grouped = bool(GROUP_BY.search(gold))
            markers = PER_ENTITY_MARKERS if grouped else TOTAL_MARKERS
            if not any(marker in query for marker in markers):
                offenders.append(
                    f"{item['id']}「{query}」gold 是"
                    f"{'分组聚合' if grouped else '单值汇总'}，"
                    f"但问句没有说明输出形状"
                )
        self.assertEqual(
            offenders, [],
            "问句形状有歧义，模型只能靠猜——这类题测不出能力，只测运气：\n  "
            + "\n  ".join(offenders),
        )

    def test_non_aggregate_gold_returns_no_duplicate_rows(self):
        """
        约定：无聚合的 gold 不得返回重复行 —— 该去重就写 DISTINCT。

        实测踩到过：q033「查询付款状态为paid的采购单的工程号」gold 返回 8 行、
        只有 6 个不同工程号（J24001 出现两次）；q045「工单所属车间」返回 3 行
        全是「一车间」。问「哪些 X」拿到重复行，那是 join 扇出的副产品，
        没有信息量——而模型加了 DISTINCT 反而被判错，还被计进静默错误率。
        """
        import re
        import sqlite3

        aggregate = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.I)
        conn = sqlite3.connect(":memory:")
        root = DATASET.parent.parent / "runtime_data"
        for db in ("erp", "pm", "mes", "wms"):
            conn.execute(f"ATTACH DATABASE '{root / (db + '_demo.db')}' AS {db}")

        offenders = []
        for item in self.items:
            if aggregate.search(item["gold_sql"]):
                continue
            rows = [tuple(r) for r in conn.execute(item["gold_sql"])]
            if len(rows) != len(set(rows)):
                offenders.append(f"{item['id']}（{len(rows)} 行 / {len(set(rows))} 唯一）")
        conn.close()
        self.assertEqual(offenders, [], "gold 返回重复行，该加 DISTINCT：\n  " + "\n  ".join(offenders))

    def test_per_entity_questions_group_by_a_key_column(self):
        """
        约定：「每个/各 X」类问题，gold 一律按 X 的**主键**分组，不按名称。

        按名称分组有重名风险，主键没有。这条约定 gold 本身是一致的
        （job_no / supplier_id / project_code / work_order_id / material_code），
        所以模型改按 material_name 分组是真错，不是歧义——
        **不能为了让它通过而把问句改简单**。
        """
        import re

        pattern = re.compile(r"(每个|各)(物料|供应商|项目|工单|工程)")
        offenders = []
        for item in self.items:
            if not pattern.search(item["query"]):
                continue
            key = re.search(r"GROUP\s+BY\s+([A-Za-z_.]+)", item["gold_sql"], re.I)
            column = (key.group(1).split(".")[-1] if key else "")
            if not column.endswith(("_code", "_id", "_no")):
                offenders.append(f"{item['id']} 按 {column or '(无 GROUP BY)'} 分组")
        self.assertEqual(offenders, [], "「每个 X」应按主键分组：" + "; ".join(offenders))

    def test_ids_are_unique(self):
        ids = [item["id"] for item in self.items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_item_has_gold_columns(self):
        """没有 gold_columns 的条目不参与召回率统计，会悄悄缩小分母。"""
        missing = [i["id"] for i in self.items if not i.get("gold_columns")]
        self.assertEqual(missing, [], f"缺 gold_columns: {missing}")


if __name__ == "__main__":
    unittest.main()
