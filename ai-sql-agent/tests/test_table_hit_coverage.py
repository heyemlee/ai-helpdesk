"""Tier table hits receive the existing floor after authorization, without new calls."""
import copy
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ai_sql_agent.auth.objects import DataPolicy
from ai_sql_agent.generation.objects import CotStep
from ai_sql_agent.generation.schema_store import LocalSchemaStore
from ai_sql_agent.generation.sql_generator import SqlGenerator
from ai_sql_agent.retrieval.access import ALLOW_ALL, filter_schema_graph
from ai_sql_agent.retrieval.graph_builder import build_schema_graph
from ai_sql_agent.retrieval.hierarchical_service import (
    DatabaseSource, ThreeTierRetrievalConfig, ThreeTierSchemaRetrievalService, TierHit,
)
from ai_sql_agent.retrieval.objects import ColumnSchema, SchemaHit, TableRelation, TableSchema


class TableHitCoverageTest(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "documents": ["document_ref", "scheduled_at", "stage", "note"],
            "document_lines": ["document_ref", "variant_code", "units", "private_margin"],
            "unselected": ["document_ref", "private_payload"],
        }
        self.tables = {t: TableSchema("sample_db", t) for t in self.spec}
        self.columns = [ColumnSchema("sample_db", t, c, "TEXT", description=c)
                        for t, names in self.spec.items() for c in names]
        self.relations = [TableRelation("sample_db", "documents", "document_ref", "document_lines", "document_ref")]
        self.service = SimpleNamespace(tables=self.tables, columns=self.columns, relations=self.relations,
            config=SimpleNamespace(min_columns_per_table=3, include_join_columns=True))
        self.candidates = ["document_lines", "documents"]
        self.head = build_schema_graph(
            [SchemaHit(c.full_name, 1, c) for c in self.columns if c.table_name == "documents"],
            self.tables, self.columns, self.relations, min_columns_per_table=3,
            table_order=self.candidates)

    def cover(self, access=ALLOW_ALL, graph=None, candidates=None):
        graph = copy.deepcopy(graph or self.head)
        filter_schema_graph(graph, access)
        return ThreeTierSchemaRetrievalService._cover_table_hits(
            graph, self.service, self.candidates if candidates is None else candidates, access, "sample_db")

    def test_sort_only_table_order_keeps_its_original_selection_semantics(self):
        self.assertEqual(list(self.head.tables), ["documents"])
        self.assertNotIn("document_lines", self.head.columns)
        self.assertFalse(self.head.relations)

    def test_table_hit_without_any_ranked_field_gets_only_the_existing_floor_and_real_relation(self):
        graph = self.cover()
        self.assertEqual(list(graph.tables), self.candidates)
        self.assertEqual([c.column_name for c in graph.columns["document_lines"]],
                         ["document_ref", "variant_code", "units"])
        self.assertEqual(graph.relations, self.relations)
        self.assertNotIn("unselected", graph.tables)
        self.assertNotIn("private_margin", graph.to_prompt_context())
        self.assertEqual(len(graph.columns["documents"]), 4)  # preserve original ranked columns

    def test_unknown_or_unselected_tables_are_not_created(self):
        graph = self.cover(candidates=["not_in_schema", "documents"])
        self.assertEqual(list(graph.tables), ["documents"])
        self.assertNotIn("not_in_schema", graph.to_prompt_context())
        self.assertNotIn("private_payload", graph.to_prompt_context())

    def test_disabled_floor_does_not_add_tables(self):
        self.service.config.min_columns_per_table = 0
        graph = self.cover()
        self.assertEqual(list(graph.tables), ["documents"])

    def test_small_real_table_is_not_padded_beyond_its_existing_columns(self):
        self.service.config.min_columns_per_table = 10
        graph = self.cover()
        self.assertEqual(len(graph.columns["document_lines"]), 4)
        self.assertEqual(len(graph.columns["documents"]), 4)

    def test_join_column_option_is_preserved(self):
        self.service.config.min_columns_per_table = 1
        self.service.config.include_join_columns = False
        self.service.columns = sorted(self.columns, key=lambda c: c.column_name != "variant_code")
        graph = self.cover()
        self.assertEqual([c.column_name for c in graph.columns["document_lines"]], ["variant_code"])
        self.service.config.include_join_columns = True
        graph = self.cover()
        self.assertEqual([c.column_name for c in graph.columns["document_lines"]], ["variant_code", "document_ref"])

    def test_denied_table_stays_out_even_if_it_is_a_required_table_hit(self):
        graph = self.cover(DataPolicy(allow_tables=("*",), deny_tables=("sample_db.document_lines",)))
        self.assertEqual(list(graph.tables), ["documents"])
        self.assertFalse(graph.relations)

    def test_column_permissions_apply_before_filling_the_floor(self):
        policy = DataPolicy(allow_tables=("*",), deny_columns=("sample_db.document_lines.document_ref",))
        graph = self.cover(policy)
        self.assertEqual([c.column_name for c in graph.columns["document_lines"]],
                         ["variant_code", "units", "private_margin"])
        self.assertFalse(graph.relations)  # forbidden join key cannot be recovered via a relation
        self.assertNotIn("document_lines.document_ref", graph.to_prompt_context())

    def test_zero_readable_columns_cannot_gain_a_table_or_a_relation(self):
        graph = self.cover(DataPolicy(allow_tables=("*",), deny_columns=("sample_db.document_lines.*",)))
        self.assertNotIn("document_lines", graph.tables)
        self.assertFalse(graph.relations)

    def test_floor_after_column_filtering_can_fill_visible_existing_table_columns(self):
        graph = self.cover(DataPolicy(allow_tables=("*",), deny_columns=("sample_db.documents.note",)))
        self.assertEqual({c.column_name for c in graph.columns["documents"]},
                         {"document_ref", "scheduled_at", "stage"})
        self.assertNotIn("note", [c.column_name for c in graph.columns["documents"]])

    def test_coverage_is_idempotent_and_unknown_tables_do_not_change_order(self):
        graph = self.cover(candidates=["not_in_schema", *self.candidates, "document_lines"])
        again = self.cover(graph=graph)
        self.assertEqual(graph.to_prompt_context(), again.to_prompt_context())
        for cols in again.columns.values():
            self.assertEqual(len(cols), len({c.column_name for c in cols}))

    def test_real_three_tier_boundary_preserves_original_query_and_does_not_retrieve_again(self):
        query = "Return document_ref, variant_code, units, scheduled_at and stage for DOC-470."
        column = self.service
        column.retrieve = Mock(return_value=SimpleNamespace(schema_graph=copy.deepcopy(self.head)))
        tier = ThreeTierSchemaRetrievalService.__new__(ThreeTierSchemaRetrievalService)
        tier.config = ThreeTierRetrievalConfig(database_keyword_guard=False)
        tier.sources = {"sample_db": DatabaseSource("sample_db", dialect="sqlite")}
        tier.column_services = {"sample_db": column}
        tier._prefetch_vectors = Mock()
        tier._select_databases = Mock(return_value=[TierHit("sample_db", 1)])
        tier._select_tables = Mock(return_value=[TierHit("sample_db." + t, 1) for t in self.candidates])
        result = tier.retrieve(query, keywords=["document", "packing"], access=ALLOW_ALL)
        graph = result.schema_graphs["sample_db"]
        self.assertEqual(list(graph.tables), self.candidates)
        self.assertIn("variant_code", graph.to_prompt_context())
        self.assertIn("units", graph.to_prompt_context())
        self.assertEqual(column.retrieve.call_count, 1)
        self.assertEqual(column.retrieve.call_args.kwargs["query"], query)

    def test_covered_schema_supports_one_generation_and_actual_detail_rows(self):
        graph = self.cover()
        store = LocalSchemaStore.from_schema_graph(graph)
        plan = CotStep("sample_db", "documents;document_lines",
                       "按 document_ref='DOC-470' 真实关联并逐行返回原始列",
                       "document_ref,variant_code,units,scheduled_at,stage")
        sql = ("SELECT d.document_ref,l.variant_code,l.units,d.scheduled_at,d.stage "
               "FROM documents d JOIN document_lines l ON l.document_ref=d.document_ref "
               "WHERE d.document_ref='DOC-470'")
        coder = Mock()
        coder.generate_sql.return_value = sql
        coder.clean_sql.side_effect = lambda value: value
        query = "逐行返回 DOC-470 的 document_ref、variant_code、units、scheduled_at、stage。"
        result = SqlGenerator(store, coder_client=coder).generate(plan, user_query=query)
        self.assertEqual(coder.generate_sql.call_count, 1)
        prompt = coder.generate_sql.call_args.args[0]
        self.assertIn(query, prompt)
        self.assertIn("document_lines.document_ref", prompt)
        self.assertIn("variant_code", prompt)
        with sqlite3.connect(":memory:") as db:
            db.executescript("CREATE TABLE documents(document_ref TEXT,scheduled_at TEXT,stage TEXT);"
                             "CREATE TABLE document_lines(document_ref TEXT,variant_code TEXT,units INT);"
                             "INSERT INTO documents VALUES('DOC-470','2028-03-14','staged');"
                             "INSERT INTO document_lines VALUES('DOC-470','Type-K',6),('DOC-470','Type-M',2);")
            cursor = db.execute(result.sql)
            self.assertEqual([c[0] for c in cursor.description],
                             ["document_ref", "variant_code", "units", "scheduled_at", "stage"])
            self.assertEqual(cursor.fetchall(), [("DOC-470","Type-K",6,"2028-03-14","staged"),
                                                ("DOC-470","Type-M",2,"2028-03-14","staged")])
