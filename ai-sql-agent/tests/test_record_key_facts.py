"""Execution-bound key facts explain provenance without changing result grain."""
import hashlib
import sqlite3
import unittest

from ai_sql_agent.pipeline.provenance import record_key_facts
from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer
from ai_sql_agent.validation import AnomalyType, ResultValidator, SemanticSelfChecker
from test_result_validation import _StubClient


class RecordKeyFactsTest(unittest.TestCase):
    def setUp(self):
        self.layer = SemanticLayer(databases={"erp_db": DatabaseSemantics(name="erp_db", tables={
            "orders": {"primary_key": ["order_no"], "columns": {"order_no": {}, "status": {}}},
            "lines": {"columns": {"order_no": {}, "model": {}, "quantity": {}}},
            "material": {"primary_key": ["material_name"], "columns": {"material_name": {}, "qty": {}}},
        })})

    def facts(self, sql, columns):
        return record_key_facts(self.layer, database="erp_db", sql=sql, dialect="sqlite", columns=columns)

    def test_real_entity_alias_and_undeclared_detail_key_are_distinct(self):
        sql = "SELECT o.order_no AS order_ref,l.model,l.quantity FROM orders o JOIN lines l ON l.order_no=o.order_no"
        facts = self.facts(sql, ["order_ref", "model", "quantity"])
        self.assertEqual(facts["query_hash"], hashlib.sha256(sql.encode()).hexdigest())
        tables = {t["table"]: t for t in facts["tables"]}
        self.assertEqual(tables["orders"]["projected_keys"], {"order_no": "order_ref"})
        self.assertEqual(tables["lines"]["key_status"], "not_declared")
        self.assertEqual(facts["result_grain"], "rows")

    def test_existing_material_key_is_not_mistaken_for_a_missing_id(self):
        facts = self.facts("SELECT material_name,qty FROM material", ["material_name", "qty"])
        self.assertEqual(facts["tables"][0]["projected_keys"], {"material_name": "material_name"})
        empty = self.facts("SELECT material_name,qty FROM material WHERE material_name='absent'", [])
        self.assertEqual(empty["tables"][0]["projected_keys"], {"material_name": "material_name"})

    def test_missing_declared_key_unknown_schema_and_invalid_metadata_stay_distinct(self):
        missing = self.facts("SELECT status FROM orders", ["status"])["tables"][0]
        self.assertEqual((missing["key_status"], missing["projected_keys"]), ("declared", {}))
        unknown = self.facts("SELECT id FROM unknown_table", ["id"])["tables"][0]
        self.assertEqual(unknown["key_status"], "unknown")
        self.layer.databases["erp_db"].tables["orders"]["primary_key"] = ["nonexistent"]
        self.assertEqual(self.facts("SELECT status FROM orders", ["status"])["tables"][0]["key_status"], "unknown")

    def test_derived_qualified_ambiguous_and_unsafe_sources_gain_no_key_proof(self):
        for sql in ["WITH x AS (SELECT order_no FROM orders) SELECT order_no FROM x",
                    "SELECT order_no FROM another.orders", "DELETE FROM orders",
                    "SELECT order_no FROM orders UNION SELECT order_no FROM lines"]:
            with self.subTest(sql=sql):
                self.assertEqual(self.facts(sql, ["order_no"]), {})
        duplicate = self.facts("SELECT order_no AS k, status AS k FROM orders", ["k"])
        self.assertEqual(duplicate["tables"][0]["projected_keys"], {})

    def test_actual_aggregate_distinct_and_group_results_do_not_gain_detail_keys(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("CREATE TABLE lines(order_no TEXT,model TEXT,quantity INT);"
                             "INSERT INTO lines VALUES('C-1','A',2),('C-1','A',3),('C-2','B',4);")
            for sql, grain, expected in [
                ("SELECT SUM(quantity) AS total FROM lines", "aggregate", [(9,)]),
                ("SELECT DISTINCT model FROM lines ORDER BY model", "distinct", [("A",), ("B",)]),
                ("SELECT model,SUM(quantity) AS total FROM lines GROUP BY model ORDER BY model", "grouped", [("A", 5), ("B", 4)]),
            ]:
                result = db.execute(sql)
                facts = self.facts(sql, [c[0] for c in result.description])
                self.assertEqual(facts["result_grain"], grain)
                self.assertEqual(result.fetchall(), expected)
                self.assertTrue(all(not t["projected_keys"] for t in facts["tables"]))

    def test_key_context_reaches_single_and_multistep_checker_without_overriding_mismatch(self):
        for preceding in ["", "前一步已取得所问订单"]:
            client = _StubClient("返回库存数量", "结论：不一致\n理由：缺少所问业务实体且来源错误")
            result = ResultValidator(SemanticSelfChecker(client)).validate(
                query="返回所问订单标识和状态", sql="SELECT status FROM orders", row_count=1,
                key_facts=self.facts("SELECT status FROM orders", ["status"]), preceding_summary=preceding)
            self.assertIn('"key_status": "declared"', client.prompts[1])
            self.assertIn('"projected_keys": {}', client.prompts[1])
            self.assertIn("来源错误", client.prompts[1])
            self.assertFalse(result.passed)
            self.assertIn(AnomalyType.SEMANTIC_MISMATCH, [f.anomaly for f in result.findings])

    def test_valid_detail_without_declared_internal_key_still_undergoes_semantic_review(self):
        sql = "SELECT order_no,model,quantity FROM lines"
        client = _StubClient("逐行返回订单号型号数量", "结论：一致\n理由：所问实体和业务字段齐全")
        result = ResultValidator(SemanticSelfChecker(client)).validate(query="列出订单明细及数量",
            sql=sql, row_count=3, key_facts=self.facts(sql, ["order_no", "model", "quantity"]))
        self.assertTrue(result.passed)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn('"key_status": "not_declared"', client.prompts[1])

    def test_unrequested_surrogate_key_does_not_replace_the_business_entity_requirement(self):
        self.layer.databases["erp_db"].tables["orders"] = {"primary_key": ["id"],
            "columns": {"id": {}, "order_no": {}, "status": {}}}
        sql = "SELECT order_no,status FROM orders"
        facts = self.facts(sql, ["order_no", "status"])
        self.assertEqual(facts["tables"][0]["key_columns"], ["id"])
        self.assertEqual(facts["tables"][0]["projected_keys"], {})
        client = _StubClient("返回订单号和状态", "结论：一致\n理由：所问业务字段完整，内部id未被要求")
        result = ResultValidator(SemanticSelfChecker(client)).validate(query="返回订单号和状态",
            sql=sql, row_count=1, key_facts=facts)
        self.assertTrue(result.passed)
        self.assertIn("未明确要求的内部记录键不是业务答案的附加条件", client.prompts[1])

    def test_passing_semantic_key_context_cannot_clear_unrequested_aggregation(self):
        sql = "SELECT SUM(quantity) AS total FROM lines"
        client = _StubClient("汇总数量", "结论：一致\n理由：同一个表")
        result = ResultValidator(SemanticSelfChecker(client)).validate(query="逐项列出订单明细及数量",
            sql=sql, row_count=1, expected_cardinality="数条", key_facts=self.facts(sql, ["total"]))
        # Existing shape policy keeps these as warnings. They still make the
        # wire result risky and ineligible for business-value evidence.
        self.assertEqual({f.anomaly for f in result.findings},
                         {AnomalyType.AGGREGATION_UNREQUESTED, AnomalyType.SHAPE_CONTRADICTS_PLAN})
        self.assertTrue(all(f.severity == "warn" for f in result.findings))
