"""Replay the recorded image-query projection loss without a model connection."""
import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sql_agent.generation.objects import CotStep, TableColumn, TableRelation, TableSchema
from ai_sql_agent.generation.cot_parser import CotStepParser
from ai_sql_agent.generation.projection import RawProjectionError, projection_issues
from ai_sql_agent.generation.schema_store import LocalSchemaStore
from ai_sql_agent.generation.sql_generator import SqlGenerator


class RecordedProjectionTest(unittest.TestCase):
    def setUp(self):
        self.schema = LocalSchemaStore(tables={
            "shipment": TableSchema("shipment", [TableColumn(c) for c in ("reference", "sku", "amount")]),
        }, relations=[])
        self.step = CotStep("sample", "shipment(reference,sku,amount)", "逐行返回原始列，同一行保留明细", "reference、sku、amount（别名quantity）")

    def issues(self, sql, step=None):
        step = step or self.step
        return projection_issues(sql, step, self.schema.extract_local_schema(step), "sqlite")

    def test_direct_alias_and_cte_preserve_real_columns(self):
        for sql in (
            "SELECT reference, sku, amount AS quantity FROM shipment",
            "WITH s AS (SELECT reference, sku, amount AS quantity FROM shipment) SELECT reference, sku, quantity FROM s",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.issues(sql), [])

    def test_constants_json_and_renamed_inner_constants_do_not_supply_raw_columns(self):
        for sql in (
            "SELECT reference, NULL AS sku, amount AS quantity FROM shipment",
            "SELECT json_object('reference',reference,'sku',sku,'quantity',amount) AS record FROM shipment",
            "SELECT reference, sku, 5 AS quantity FROM shipment WHERE amount=5",
            "WITH s AS (SELECT reference, 'fake' AS sku, amount FROM shipment) SELECT reference, sku, amount AS quantity FROM s",
            "SELECT reference, sku, amount AS different_alias FROM shipment",
        ):
            with self.subTest(sql=sql):
                self.assertTrue(self.issues(sql))

    def test_same_row_plan_rejects_split_union_even_when_columns_appear(self):
        self.assertTrue(self.issues("SELECT reference, sku, NULL AS quantity FROM shipment UNION ALL SELECT reference, NULL, amount FROM shipment"))

    def test_complete_union_branches_and_unused_union_cte_are_allowed(self):
        complete = "SELECT reference, sku, amount AS quantity FROM shipment"
        self.assertEqual(self.issues(complete + " UNION ALL " + complete), [])
        self.assertEqual(self.issues("WITH unused AS (SELECT 1 UNION ALL SELECT 2) " + complete), [])

    def test_postgresql_dialect_alias_is_supported(self):
        sql = "SELECT reference, sku, amount AS quantity FROM shipment"
        self.assertEqual(projection_issues(sql, self.step, self.schema.extract_local_schema(self.step), "postgresql"), [])

    def test_unqualified_filter_column_survives_join_key_cropping(self):
        store = LocalSchemaStore(tables={
            "orders": TableSchema("orders", [TableColumn(c) for c in ("order_no", "order_status", "unrequested")]),
            "order_item": TableSchema("order_item", [TableColumn(c) for c in ("order_no", "model", "quantity")]),
        }, relations=[TableRelation("orders", "order_no", "order_item", "order_no")])
        step = CotStep("sample", "orders, order_item",
                       "按 orders.order_no=order_item.order_no 关联，只保留 order_status=已交付", "model,quantity")
        local = store.extract_local_schema(step)
        self.assertEqual({c.column_name for c in local.tables["orders"].columns}, {"order_no", "order_status"})
        self.assertEqual({c.column_name for c in local.tables["order_item"].columns}, {"order_no", "model", "quantity"})

    def test_rowwise_computation_is_not_a_raw_column_contract(self):
        schema = LocalSchemaStore(tables={"orders": TableSchema("orders", [TableColumn(c) for c in ("po_no", "po_amt", "pay_amt")])}, relations=[])
        step = CotStep("sample", "orders(po_no,po_amt,pay_amt)", "逐条计算未付余额", "po_no、po_amt-pay_amt AS unpaid_balance")
        coder = Mock()
        coder.generate_sql.return_value = "SELECT po_no,po_amt-pay_amt AS unpaid_balance FROM orders"
        coder.clean_sql.side_effect = lambda sql: sql
        result = SqlGenerator(schema, coder_client=coder).generate(step)
        self.assertEqual(result.sql, coder.generate_sql.return_value)
        self.assertEqual(coder.generate_sql.call_count, 1)

    def test_ordinary_aggregate_does_not_trigger_raw_projection_repair(self):
        step = replace(self.step, operation_instruction="汇总数量", output_target="SUM(amount) AS total")
        coder = Mock()
        coder.generate_sql.return_value = "SELECT SUM(amount) AS total FROM shipment"
        coder.clean_sql.side_effect = lambda sql: sql
        result = SqlGenerator(self.schema, coder_client=coder).generate(step)
        self.assertEqual(result.sql, coder.generate_sql.return_value)
        self.assertEqual(coder.generate_sql.call_count, 1)

    def test_persistent_omission_fails_closed_after_one_correction(self):
        coder = Mock()
        coder.generate_sql.return_value = "SELECT reference FROM shipment"
        coder.clean_sql.side_effect = lambda sql: sql
        with self.assertRaisesRegex(RawProjectionError, "raw_projection_incomplete"):
            SqlGenerator(self.schema, coder_client=coder).generate(self.step)
        self.assertEqual(coder.generate_sql.call_count, 2)

    def test_pipeline_returns_structured_failure_without_executing_incomplete_sql(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline
        from ai_sql_agent.retrieval.access import ALLOW_ALL
        pipeline = Text2SQLPipeline.__new__(Text2SQLPipeline)
        pipeline._resolve_schema_store = Mock(return_value=self.schema)
        pipeline._certified_examples = Mock(return_value="")
        pipeline._execute_unless_self_comparison = Mock()
        pipeline.dialects = {"sample": "sqlite"}
        pipeline.config = SimpleNamespace(enable_cross_db_guard=False)
        with patch("ai_sql_agent.pipeline.text2sql_pipeline.CoderModelClient") as coder_type:
            coder = coder_type.return_value
            coder.generate_sql.return_value = "SELECT reference FROM shipment"
            coder.clean_sql.side_effect = lambda sql: sql
            log, executed = pipeline._run_step("query", "query", self.step, None,
                step_index=1, schema_stores={}, previous_results="", access=ALLOW_ALL, user_id="test")
        self.assertIsNone(executed)
        self.assertFalse(log.execution_result["success"])
        self.assertEqual(log.execution_result["error_category"], "raw_projection_incomplete")
        self.assertEqual(coder.generate_sql.call_count, 2)
        pipeline._execute_unless_self_comparison.assert_not_called()

    def test_projection_failure_during_cross_database_correction_is_structured(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline
        from ai_sql_agent.retrieval.access import ALLOW_ALL
        pipeline = Text2SQLPipeline.__new__(Text2SQLPipeline)
        pipeline._resolve_schema_store = Mock(return_value=self.schema)
        pipeline._certified_examples = Mock(return_value="")
        pipeline._execute_unless_self_comparison = Mock()
        pipeline._tables_of = Mock(return_value={"shipment"})
        pipeline.dialects = {"sample": "sqlite"}
        pipeline.config = SimpleNamespace(enable_cross_db_guard=True)
        with patch("ai_sql_agent.pipeline.text2sql_pipeline.CoderModelClient") as coder_type:
            coder = coder_type.return_value
            coder.generate_sql.side_effect = [
                "SELECT reference, sku, amount AS quantity FROM shipment WHERE EXISTS(SELECT 1 FROM foreign_table)",
                "SELECT reference FROM shipment", "SELECT reference FROM shipment",
            ]
            coder.clean_sql.side_effect = lambda sql: sql
            log, executed = pipeline._run_step("query", "query", self.step, None,
                step_index=1, schema_stores={}, previous_results="", access=ALLOW_ALL, user_id="test")
        self.assertIsNone(executed)
        self.assertEqual(log.execution_result["error_category"], "raw_projection_incomplete")
        self.assertEqual(coder.generate_sql.call_count, 3)
        pipeline._execute_unless_self_comparison.assert_not_called()

    def test_recorded_missing_projection_is_corrected_before_execution(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/stage6_image_projection.json").read_text())
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE orders(order_no TEXT PRIMARY KEY, order_status TEXT, delivery_date TEXT);
            CREATE TABLE order_item(order_no TEXT, model TEXT, quantity INTEGER);
            INSERT INTO orders VALUES('SO-2026-11039', '已交付', '2026-11-22');
            INSERT INTO order_item VALUES('SO-2026-11039', 'BX-11039-24', 5);
            INSERT INTO order_item VALUES('SO-2026-11039', 'another-model', 9);
        """)
        self.assertEqual([dict(r) for r in db.execute(fixture["recorded_sql"])], fixture["recorded_rows"][:1] + [
            fixture["recorded_rows"][1], {"order_ref": "SO-2026-11039", "ordered_qty": 9, "query_source": "order_item"}])
        correct = """SELECT o.order_no AS order_ref, i.model, i.quantity AS ordered_qty,
                            o.order_status, o.delivery_date
                     FROM orders o JOIN order_item i ON i.order_no=o.order_no
                     WHERE o.order_no='SO-2026-11039'"""

        class RecordedCoder:
            def __init__(self): self.prompts = []
            def generate_sql(self, prompt):
                self.prompts.append(prompt)
                return fixture["recorded_sql"] if len(self.prompts) == 1 else correct
            @staticmethod
            def clean_sql(sql): return sql

        schema = LocalSchemaStore(
            tables={name: TableSchema(name, [TableColumn(r[1], data_type=r[2]) for r in db.execute(f"PRAGMA table_info({name})")])
                    for name in ("orders", "order_item")},
            relations=[TableRelation("orders", "order_no", "order_item", "order_no")],
        )
        recorded_step = CotStepParser().parse_many(fixture["planning_output"])[0]
        local = schema.extract_local_schema(recorded_step)
        self.assertEqual({c.column_name for c in local.tables["orders"].columns}, {"order_no", "order_status", "delivery_date"})
        self.assertEqual({c.column_name for c in local.tables["order_item"].columns}, {"order_no", "model", "quantity"})
        issues = projection_issues(fixture["recorded_sql"], recorded_step, local, "sqlite")
        self.assertTrue(any("model" in issue and "order_status" in issue and "delivery_date" in issue for issue in issues))
        coder = RecordedCoder()
        result = SqlGenerator(schema_store=schema, coder_client=coder).generate(CotStep(
            database="erp_db", processing_objects="orders(order_no,order_status,delivery_date); order_item(order_no,model,quantity)",
            operation_instruction="逐行返回原始列，同一行保留订单与其型号明细，不能合并不同型号。",
            output_target="order_no（别名order_ref）、model、quantity（别名ordered_qty）、order_status、delivery_date",
        ))
        rows = [dict(r) for r in db.execute(result.sql)]
        self.assertEqual(rows, [
            {"order_ref": "SO-2026-11039", "model": "BX-11039-24", "ordered_qty": 5, "order_status": "已交付", "delivery_date": "2026-11-22"},
            {"order_ref": "SO-2026-11039", "model": "another-model", "ordered_qty": 9, "order_status": "已交付", "delivery_date": "2026-11-22"},
        ])
        self.assertEqual(len(coder.prompts), 2)
        for field in ("model", "order_status", "delivery_date"):
            self.assertIn(field, coder.prompts[-1])
