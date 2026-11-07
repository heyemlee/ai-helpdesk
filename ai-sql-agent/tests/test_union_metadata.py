"""Compound metadata follows every physical leaf, without record/scope claims."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ai_sql_agent.api.result_summary import summarize_result
from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.execution.objects import MCPExecutionRequest
from ai_sql_agent.execution.sqlite_executor import SQLiteMCPExecutor
from ai_sql_agent.pipeline.provenance import column_metadata, column_units, record_key_facts, source_records
from ai_sql_agent.pipeline.text2sql_pipeline import Text2SQLPipeline
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer
from tests.test_contract_fixtures import _is_valid
from tests.test_structured_evidence import result


SQL = """SELECT o.order_no AS order_ref, i.model, i.quantity AS ordered_qty,
o.delivery_date, o.order_status FROM orders o LEFT JOIN order_item i ON o.order_no=i.order_no
WHERE o.order_no='META-482'
UNION ALL
SELECT i.order_no AS other_ref, i.model AS other_model, i.quantity AS other_qty,
o.delivery_date AS other_date, o.order_status AS other_status
FROM order_item i LEFT JOIN orders o ON i.order_no=o.order_no
WHERE i.order_no='META-482' AND o.order_no IS NULL"""
COLUMNS = ["order_ref", "model", "ordered_qty", "delivery_date", "order_status"]


class UnionMetadataTest(unittest.TestCase):
    def setUp(self):
        self.layer = SemanticLayer(databases={"erp_db": DatabaseSemantics(name="erp_db", tables={
            "orders": {"primary_key": ["order_no"], "columns": {
                "order_no": {"description": "Order identifier."},
                "delivery_date": {"description": "Planned delivery date, not actual arrival."},
                "order_status": {"description": "Current recorded order status."}}},
            "order_item": {"primary_key": ["order_no"], "columns": {
                "order_no": {"description": "Order identifier."},
                "model": {"description": "Ordered product model."},
                "quantity": {"description": "Ordered quantity.", "unit": "pcs"}}},
            "other_item": {"columns": {
                "quantity": {"description": "Ordered quantity.", "unit": "pcs"},
                "weight": {"description": "Recorded mass.", "unit": "kg"}}},
        })})

    def units(self, sql, columns=("q",), dialect="sqlite"):
        return column_units(self.layer, database="erp_db", sql=sql, dialect=dialect, columns=list(columns))

    def meta(self, sql, columns=("q",), dialect="sqlite"):
        return column_metadata(self.layer, database="erp_db", sql=sql, dialect=dialect, columns=list(columns))

    def test_executed_outer_join_union_reads_config_for_four_common_positions(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.executescript("""CREATE TABLE orders(order_no TEXT, delivery_date TEXT, order_status TEXT);
                CREATE TABLE order_item(order_no TEXT,model TEXT,quantity INT);
                INSERT INTO orders VALUES('META-482','2028-04-09','packing');
                INSERT INTO order_item VALUES('META-482','Panel-Z',7);""")
            cursor = db.execute(SQL)
            self.assertEqual([c[0] for c in cursor.description], COLUMNS)
            rows = [dict(r) for r in cursor]
        self.assertEqual(rows[0]["ordered_qty"], 7)
        for dialect in ("sqlite", "postgres"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.units(SQL, COLUMNS, dialect), {"ordered_qty": "pcs"})
                metadata = self.meta(SQL, COLUMNS, dialect)
                self.assertEqual(set(metadata), set(COLUMNS) - {"order_ref"})
                self.assertEqual(metadata["ordered_qty"]["column"], "quantity")
                self.assertEqual(metadata["delivery_date"]["description"], "Planned delivery date, not actual arrival.")
                self.assertTrue(all(m["query_hash"] == hashlib.sha256(SQL.encode()).hexdigest() for m in metadata.values()))
                self.assertEqual(source_records(self.layer, database="erp_db", sql=SQL, dialect=dialect,
                                                columns=COLUMNS, rows=rows), [])
                self.assertEqual(record_key_facts(self.layer, database="erp_db", sql=SQL, dialect=dialect,
                                                 columns=COLUMNS), {})

    def test_all_levels_and_positions_must_agree_despite_branch_aliases(self):
        left = "SELECT quantity AS q, model AS m FROM order_item"
        right = "SELECT quantity AS renamed, model AS another FROM order_item"
        for sql in (f"{left} UNION ALL {right} UNION ALL {right}",
                    f"{left} UNION ALL ({right} UNION ALL {right})"):
            with self.subTest(sql=sql):
                self.assertEqual(self.units(sql, ["q", "m"]), {"q": "pcs"})
                self.assertEqual(set(self.meta(sql, ["q", "m"])), {"q", "m"})
        for last in ("SELECT quantity,model FROM other_item", "SELECT model,quantity FROM order_item"):
            self.assertEqual(self.meta(f"{left} UNION ALL {right} UNION ALL {last}", ["q", "m"]), {})

    def test_equal_names_units_and_meanings_do_not_merge_different_physical_columns(self):
        for unit in ("pcs", "kg"):
            self.layer.databases["erp_db"].tables["other_item"]["columns"]["quantity"]["unit"] = unit
            sql = "SELECT quantity AS q FROM order_item UNION ALL SELECT quantity AS q FROM other_item"
            self.assertEqual(self.units(sql), {})
            self.assertEqual(self.meta(sql), {})

    def test_any_unsupported_expression_or_empty_branch_keeps_its_position_unknown(self):
        for expression in ("NULL", "0", "'7'", "quantity+0", "CAST(quantity AS INT)",
                           "COALESCE(quantity,0)", "SUM(quantity)", "COUNT(*)", "ABS(quantity)"):
            sql = ("SELECT quantity AS q, model AS m FROM order_item UNION ALL "
                   f"SELECT {expression} AS renamed, model AS x FROM order_item WHERE 1=0")
            with self.subTest(expression=expression):
                self.assertEqual(self.units(sql, ["q", "m"]), {})
                self.assertEqual(set(self.meta(sql, ["q", "m"])), {"m"})

    def test_unknown_arity_sources_ambiguity_and_set_kinds_stay_unknown(self):
        left = "SELECT quantity AS q FROM order_item"
        branches = [
            "SELECT * FROM order_item", "SELECT quantity,model FROM order_item",
            "SELECT quantity AS q FROM other.order_item",
            "SELECT quantity AS q FROM (SELECT quantity FROM order_item) x",
            "SELECT quantity AS q FROM order_item i JOIN order_item j ON 1=1",
            "SELECT i.quantity AS q FROM order_item i(quantity)",
            "SELECT i.quantity AS q FROM order_item i JOIN other_item i ON 1=1",
        ]
        queries = [f"{left} UNION ALL {b}" for b in branches]
        queries += [f"{left} {op} {left}" for op in ("UNION", "INTERSECT", "EXCEPT")]
        queries += [
            f"{left} UNION ALL BY NAME {left}",
            f"({left} UNION {left}) UNION ALL {left}",
            f"WITH x AS ({left}) {left} UNION ALL SELECT q FROM x",
            f"{left} UNION ALL {left}; SELECT quantity AS q FROM order_item",
            "SELECT quantity AS q,model AS Q FROM order_item UNION ALL SELECT quantity,model FROM order_item",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertEqual(self.units(sql), {})
                self.assertEqual(self.meta(sql), {})

    def test_missing_configuration_and_semantic_changes_do_not_borrow_defaults(self):
        sql = "SELECT quantity AS q FROM order_item UNION ALL SELECT quantity FROM order_item"
        original = self.meta(sql)["q"]
        column = self.layer.databases["erp_db"].tables["order_item"]["columns"]["quantity"]
        column.pop("unit")
        self.assertEqual(self.units(sql), {})
        column["description"] = "A revised business definition."
        self.assertNotEqual(self.meta(sql)["q"]["schema_revision"], original["schema_revision"])
        for bad in ("", " ", None, {"wrong": "type"}, "x" * 2001):
            column["description"] = bad
            self.assertEqual(self.meta(sql), {})
        self.assertEqual(self.units(sql, dialect="mysql"), {})

    def test_existing_single_select_unit_wrappers_are_unchanged(self):
        self.assertEqual(self.units("SELECT SUM(quantity) AS q FROM order_item"), {"q": "pcs"})
        self.assertEqual(self.meta("SELECT SUM(quantity) AS q FROM order_item"), {})

    def test_actual_pipeline_wire_binds_units_and_meaning_to_the_full_union(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.db"
            with sqlite3.connect(path) as db:
                db.executescript("""CREATE TABLE orders(order_no TEXT,delivery_date TEXT,order_status TEXT);
                    CREATE TABLE order_item(order_no TEXT,model TEXT,quantity INT);
                    INSERT INTO orders VALUES('META-482','2028-04-09','packing');
                    INSERT INTO order_item VALUES('META-482','Panel-Z',7);""")
            with authorization_scope(ALLOW_ALL):
                execution = SQLiteMCPExecutor("erp_db", path).execute(MCPExecutionRequest("erp_db", SQL))
            self.assertTrue(execution.success)
            pipeline = object.__new__(Text2SQLPipeline)
            pipeline.semantic = self.layer
            pipeline._dialect_name = Mock(return_value="sqlite")
            units, records, metadata = pipeline._provenance(database="erp_db", sql=SQL, execution_result=execution)
            saved = result(execution)
            step = saved.pipeline_result.step_logs[0]
            step.sql = SQL
            step.column_units, step.source_records, step.column_metadata = units, records, metadata
            wire = summarize_result(saved).data_part()
            wire["trace_id"] = "new-union-query"
            self.assertTrue(_is_valid("schema/artifact-data-part.schema.json", wire))
            self.assertEqual(wire["steps"][0]["column_units"], {"ordered_qty": "pcs"})
            self.assertEqual(set(wire["steps"][0]["column_metadata"]), set(COLUMNS) - {"order_ref"})
            self.assertEqual(wire["steps"][0]["source_records"], [])
            quantity = next(c for c in wire["evidence"] if c["field"] == "ordered_qty")
            self.assertEqual((quantity["value"], quantity["unit"]), (7, "pcs"))
            self.assertEqual(quantity["query_hash"], hashlib.sha256(SQL.encode()).hexdigest())
