"""检索质量回归：索引文本合并与每表保底配额。

这两条都是实测中抓到的真实缺陷，加测试防止回退。
"""

import unittest

from ai_sql_agent.pipeline import Text2SQLPipeline


class IndexTextMergeTest(unittest.TestCase):
    """自定义索引文本必须与自动构建文本合并，不能覆盖。

    覆盖会让手写过 keyword_text 的字段反而更难被召回——自动文本里的
    库名、表名、字段名、类型、样例值全丢了。实测表现为 po_amt（手写）
    召不回、同表 po_date（自动）却能召回。
    """

    @classmethod
    def setUpClass(cls):
        cls.pipeline = Text2SQLPipeline()

    def test_hand_written_column_is_still_retrievable(self):
        result = self.pipeline.run("查询板材类供应商的采购金额")
        self.assertIn(
            "erp_db.purchase_order.po_amt",
            result.retrieved_columns,
            "手写过 keyword_text 的字段必须能被召回",
        )

    def test_index_text_contains_both_custom_and_auto_parts(self):
        service = self.pipeline.schema_retrieval_service.column_services["erp_db"]
        doc = next(d for d in service.documents if d.doc_id.endswith("purchase_order.po_amt"))
        self.assertIn("采购金额", doc.keyword_text)      # 自定义部分
        self.assertIn("purchase_order", doc.keyword_text)  # 自动部分


class PerTableFloorTest(unittest.TestCase):
    """多表关联时每张选中表都要有字段进图，不能只剩 join key。"""

    @classmethod
    def setUpClass(cls):
        cls.pipeline = Text2SQLPipeline()

    def test_join_query_retrieves_columns_from_both_tables(self):
        result = self.pipeline.run("查询库存低于安全库存的物料名称")
        columns = result.retrieved_columns
        self.assertTrue(
            any("wms_db.material." in c for c in columns), "material 表应有字段进图"
        )
        self.assertTrue(
            any("wms_db.stock_snapshot." in c for c in columns),
            "stock_snapshot 表应有字段进图",
        )

    def test_schema_graph_stays_focused(self):
        """保底配额不能让 Schema 图退化成全量 Schema。"""
        result = self.pipeline.run("查询准时率低于0.9的供应商名称")
        self.assertLess(
            len(result.retrieved_columns), 45,
            "进图字段数应远小于全库字段总数（86），否则三级检索失去意义",
        )


if __name__ == "__main__":
    unittest.main()


class CompactSchemaRenderingTest(unittest.TestCase):
    """
    Schema 上下文装不下时，先降描述精度，最后才动字段本身。

    「字段进不进得来」比「字段描述得多详细」重要得多：实测 5 条召回缺失里
    4 条是 Token 预算切掉的、不是检索没找到（retrieval_recall_hit=True 而
    recall_hit=False），后果是规划模型写出「字段信息未提供」，
    再生成 `SELECT project_code FROM project WHERE 0`。
    """

    def _graph(self):
        from ai_sql_agent.retrieval.objects import ColumnSchema, SchemaGraph, TableSchema

        return SchemaGraph(
            database="pm_db",
            tables={"milestone": TableSchema(
                database="pm_db", table_name="milestone", description="里程碑表",
                aliases=["节点表"], primary_keys=["milestone_id"],
            )},
            columns={"milestone": [ColumnSchema(
                database="pm_db", table_name="milestone",
                column_name="ms_status", data_type="TEXT", description="里程碑状态。",
                aliases=["节点状态", "状态"], semantic_role="dimension",
                samples=["delayed", "on_time", "pending", "a", "b"],
                value_range="delayed / on_time / pending",
                business_usage="判断节点是否延期，用于超期分析与预警。",
            )]},
            relations=[],
        )

    def test_compact_keeps_the_parts_that_decide_correctness(self):
        text = self._graph().to_prompt_context(compact=True)
        self.assertIn("ms_status", text)
        self.assertIn("里程碑状态", text)
        # 取值域最关键：用户按取值提问（「delayed」），模型得知道库里真实存的是什么
        self.assertIn("delayed / on_time / pending", text)

    def test_compact_drops_the_decorative_parts(self):
        text = self._graph().to_prompt_context(compact=True)
        self.assertNotIn("字段别名", text)
        self.assertNotIn("字段角色", text)
        self.assertNotIn("业务用途", text)

    def test_compact_is_substantially_cheaper(self):
        from ai_sql_agent.pipeline.token_budget import estimate_tokens

        graph = self._graph()
        full = estimate_tokens(graph.to_prompt_context())
        compact = estimate_tokens(graph.to_prompt_context(compact=True))
        self.assertLess(compact, full * 0.7, "精简档没省下足够预算就没有意义")

    def test_full_rendering_is_unchanged_by_default(self):
        text = self._graph().to_prompt_context()
        for marker in ("字段别名", "字段角色", "业务用途"):
            self.assertIn(marker, text)
