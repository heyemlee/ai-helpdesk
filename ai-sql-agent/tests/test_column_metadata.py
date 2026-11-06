"""Current configured meaning follows direct executed columns, never expressions."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ai_sql_agent.api.result_summary import summarize_result
from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.execution.objects import MCPExecutionRequest
from ai_sql_agent.execution.sqlite_executor import SQLiteMCPExecutor
from ai_sql_agent.pipeline import provenance
from ai_sql_agent.pipeline.text2sql_pipeline import Text2SQLPipeline
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer
from tests.test_contract_fixtures import _is_valid, _validator
from tests.test_structured_evidence import result


class ColumnMetadataTest(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((Path(__file__).parent / "fixtures/stage6_field_description.json").read_text())
        self.layer = SemanticLayer(databases={"erp_db": DatabaseSemantics(name="erp_db", tables={
            "orders": {"columns": {"delivery_date": copy.deepcopy(self.record["metadata"])}}})})

    def describe(self, sql, columns=("d",)):
        return provenance.column_metadata(self.layer, database="erp_db", sql=sql, dialect="sqlite", columns=columns)

    def test_direct_alias_is_bound_to_executed_sql_and_config_revision(self):
        sql = "SELECT o.delivery_date AS d FROM orders o"
        meta = self.describe(sql)["d"]
        self.assertEqual(meta["description"], self.record["metadata"]["description"])
        self.assertEqual(meta["column"], "delivery_date")
        self.assertEqual(meta["table"], "orders")
        self.assertEqual(meta["query_hash"], hashlib.sha256(sql.encode()).hexdigest())
        original = meta["schema_revision"]
        self.layer.databases["erp_db"].tables["orders"]["columns"]["delivery_date"]["description"] = "Actual delivery date."
        changed = self.describe(sql)["d"]
        self.assertNotEqual(changed["schema_revision"], original)
        self.assertEqual(changed["description"], "Actual delivery date.")
        self.assertEqual(changed["query_hash"], meta["query_hash"])

    def test_expression_missing_and_ambiguous_columns_have_no_inferred_meaning(self):
        for sql in (
            "SELECT MAX(delivery_date) AS d FROM orders",
            "SELECT date(delivery_date) AS d FROM orders",
            "SELECT COALESCE(delivery_date, '2026-11-22') AS d FROM orders",
            "SELECT '2026-11-22' AS d FROM orders",
            "SELECT * FROM orders",
            "SELECT absent AS d FROM orders",
            "SELECT delivery_date AS d FROM orders o JOIN delivery x ON o.id=x.id",
            "SELECT delivery_date AS d FROM orders o JOIN orders x ON o.id=x.id",
            "SELECT o.delivery_date AS d, x.delivery_date AS d FROM orders o JOIN orders x ON o.id=x.id",
            "WITH x AS (SELECT delivery_date FROM orders) SELECT delivery_date AS d FROM x",
            "SELECT delivery_date AS d FROM other.orders",
            "SELECT orders.delivery_date AS d FROM orders original JOIN (SELECT 'ACTUAL' AS delivery_date) orders ON 1=1",
            "SELECT d.*, o.delivery_date AS d FROM delivery d JOIN orders o ON 1=1",
            "SELECT o.delivery_date AS d FROM orders o(delivery_date)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.describe(sql), {})

    def test_qualified_join_column_remains_attributable(self):
        meta = self.describe("SELECT o.delivery_date AS d FROM orders o JOIN delivery x ON o.id=x.id")
        self.assertEqual(meta["d"]["column"], "delivery_date")

    def test_duplicate_execution_columns_and_postgres_folded_aliases_are_unknown(self):
        self.assertEqual(self.describe("SELECT delivery_date AS d FROM orders", columns=["d", "d"]), {})
        sql = "SELECT delivery_date AS day, delivery_date AS DAY FROM orders"
        self.assertEqual(provenance.column_metadata(self.layer, database="erp_db", sql=sql,
                                                    dialect="postgres", columns=["day"]), {})

    def test_real_derived_alias_and_star_collisions_never_receive_physical_meaning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collisions.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE orders(delivery_date TEXT)")
                db.execute("CREATE TABLE delivery(delivery_date TEXT)")
                db.execute("INSERT INTO orders VALUES ('PLANNED')")
                db.execute("INSERT INTO delivery VALUES ('ACTUAL')")
            queries = [
                ("SELECT orders.delivery_date AS shown FROM orders original "
                 "JOIN (SELECT 'ACTUAL' AS delivery_date) orders ON 1=1", "shown"),
                ("SELECT d.*, o.delivery_date AS delivery_date FROM delivery d JOIN orders o ON 1=1", "delivery_date"),
            ]
            for sql, field in queries:
                with self.subTest(sql=sql), authorization_scope(ALLOW_ALL):
                    execution = SQLiteMCPExecutor("erp_db", path).execute(MCPExecutionRequest("erp_db", sql))
                    self.assertTrue(execution.success)
                    self.assertEqual(execution.rows[0][field], "ACTUAL")
                    self.assertEqual(self.describe(sql, columns=execution.columns), {})

    def test_postgres_quoted_tables_and_aliases_preserve_distinct_sources(self):
        self.layer.databases["erp_db"].tables["Orders"] = {"columns": {
            "delivery_date": {"description": "Actual delivery date."}}}
        def query(sql, columns):
            return provenance.column_metadata(self.layer, database="erp_db", sql=sql, dialect="postgres", columns=columns)
        meta = query('SELECT delivery_date AS shown FROM "Orders"', ["shown"])
        self.assertEqual(meta["shown"]["table"], "Orders")
        self.assertEqual(meta["shown"]["description"], "Actual delivery date.")
        meta = query('SELECT "A".delivery_date AS planned, "a".delivery_date AS actual '
                     'FROM orders "A" JOIN "Orders" "a" ON 1=1', ["planned", "actual"])
        self.assertEqual(meta["planned"]["table"], "orders")
        self.assertEqual(meta["actual"]["table"], "Orders")
        self.assertEqual(meta["planned"]["output_field"], "planned")
        self.assertEqual(meta["actual"]["output_field"], "actual")
        meta = query('SELECT delivery_date AS shown FROM ORDERS', ["shown"])
        self.assertEqual(meta["shown"]["table"], "orders")

    def test_invalid_or_oversized_configuration_is_not_clipped_or_guessed(self):
        column = self.layer.databases["erp_db"].tables["orders"]["columns"]["delivery_date"]
        for description in (None, "", " ", "x" * 2001, {"kind": "planned"}):
            with self.subTest(description=str(description)[:30]):
                column["description"] = description
                self.assertEqual(self.describe("SELECT delivery_date AS d FROM orders"), {})

    def test_actual_execution_pipeline_summary_and_wire_schema_preserve_meaning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE orders (order_ref TEXT, delivery_date TEXT)")
                db.execute("INSERT INTO orders VALUES ('SO-recorded', '2026-11-22')")
            sql = "SELECT order_ref, delivery_date AS d FROM orders"
            with authorization_scope(ALLOW_ALL):
                execution = SQLiteMCPExecutor("erp_db", path).execute(MCPExecutionRequest("erp_db", sql))
            pipeline = object.__new__(Text2SQLPipeline)
            pipeline.semantic = self.layer
            pipeline._dialect_name = Mock(return_value="sqlite")
            units, records, metadata = pipeline._provenance(database="erp_db", sql=sql, execution_result=execution)
            saved = result(execution)
            step = saved.pipeline_result.step_logs[0]
            step.column_units, step.source_records, step.column_metadata = units, records, metadata
            summary = summarize_result(saved)
            wire = summary.data_part()
            wire["trace_id"] = "field-description-query"  # added by each carrier around the summary
            self.assertTrue(_is_valid("schema/artifact-data-part.schema.json", wire),
                            [e.message for e in _validator("schema/artifact-data-part.schema.json").iter_errors(wire)])
            self.assertEqual(wire["steps"][0]["column_metadata"], metadata)
            self.assertEqual(summary.step_logs[0].model_dump()["column_metadata"], metadata)
            self.assertEqual(wire["evidence"][1]["field"], "d")
            self.assertNotIn("column_metadata", wire["evidence"][1])  # old evidence identity is unchanged
            missing = copy.deepcopy(wire)
            del missing["steps"][0]["column_metadata"]
            self.assertTrue(_is_valid("schema/artifact-data-part.schema.json", missing))
            bad = copy.deepcopy(wire)
            bad["steps"][0]["column_metadata"]["d"]["description"] = {"guess": "planned"}
            self.assertFalse(_is_valid("schema/artifact-data-part.schema.json", bad))
            bad = copy.deepcopy(wire)
            bad["steps"][0]["column_metadata"]["d"]["query_hash"] = "unbound"
            self.assertFalse(_is_valid("schema/artifact-data-part.schema.json", bad))
            execution.success = False
            self.assertEqual(pipeline._provenance(database="erp_db", sql=sql, execution_result=execution), ({}, [], {}))
