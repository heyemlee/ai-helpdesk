"""
pgvector / PostgreSQL 集成测试。

未配置 AI_SQL_PGVECTOR_DSN 时整体跳过，本地与 CI 都能无依赖跑通：

    docker compose --project-directory . -f deploy/docker-compose.pgvector.yml up -d
    export AI_SQL_PGVECTOR_DSN="postgresql://ai_sql:ai_sql@localhost:5432/ai_sql"
    python -m unittest tests.test_pgvector_integration
"""

import os
import unittest

import numpy as np

DSN = os.getenv("AI_SQL_PGVECTOR_DSN", "")

requires_pgvector = unittest.skipUnless(
    DSN, "未配置 AI_SQL_PGVECTOR_DSN，跳过 pgvector 集成测试"
)


@requires_pgvector
class PgVectorSchemaIndexTest(unittest.TestCase):
    def setUp(self):
        from ai_sql_agent.core import reload_settings
        from ai_sql_agent.pipeline.local_clients import LocalHashEmbeddingClient
        from ai_sql_agent.retrieval.pgvector_index import PgVectorIndex

        reload_settings()
        self.index = PgVectorIndex(
            embedding_client=LocalHashEmbeddingClient(),
            dsn=DSN,
            namespace="unittest_schema",
        )

    def tearDown(self):
        self.index.conn.execute(f"DROP TABLE IF EXISTS {self.index.table}")
        self.index.conn.close()

    def test_build_and_search(self):
        self.index.build(
            [
                "sales_order.order_amt 订单采购金额",
                "supplier.supplier_name 订单供应商名称",
            ]
        )
        hits = self.index.search("采购金额", top_k=2)

        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0][0], 0)
        self.assertGreater(hits[0][1], hits[1][1])

    def test_unchanged_documents_reuse_the_existing_index(self):
        """Schema 没变就不该重跑 Embedding：启动慢且按量计费。"""
        from ai_sql_agent.pipeline.local_clients import LocalHashEmbeddingClient

        class CountingEmbedding(LocalHashEmbeddingClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def embed_texts(self, texts):
                self.calls += 1
                return super().embed_texts(texts)

        from ai_sql_agent.retrieval.pgvector_index import PgVectorIndex

        client = CountingEmbedding()
        documents = ["字段 a 的说明", "字段 b 的说明"]

        index = PgVectorIndex(client, dsn=DSN, namespace="unittest_fingerprint")
        try:
            index.build(documents)
            self.assertEqual(client.calls, 1)

            # 模拟重启：同样的文档不应再调 Embedding
            reopened = PgVectorIndex(
                client, dsn=DSN, namespace="unittest_fingerprint"
            )
            reopened.build(documents)
            self.assertEqual(client.calls, 1, "文档未变却重跑了 Embedding")

            # 复用的索引照样能检索（检索本身要给 query 算一次向量）
            self.assertEqual(len(reopened.search("字段 a", top_k=2)), 2)
            self.assertEqual(client.calls, 2)

            # Schema 变了就必须重建
            reopened.build(documents + ["字段 c 的说明"])
            self.assertEqual(client.calls, 3)
        finally:
            index.conn.execute("DROP TABLE IF EXISTS ai_sql_vectors_unittest_fingerprint")
            index.conn.execute(
                "DELETE FROM ai_sql_vector_meta WHERE namespace = 'unittest_fingerprint'"
            )
            index.conn.close()

    def test_index_rebuilds_when_vectors_were_wiped(self):
        """指纹在但数据被清空（比如手工 TRUNCATE）时要能自愈。"""
        from ai_sql_agent.pipeline.local_clients import LocalHashEmbeddingClient
        from ai_sql_agent.retrieval.pgvector_index import PgVectorIndex

        index = PgVectorIndex(
            LocalHashEmbeddingClient(), dsn=DSN, namespace="unittest_wiped"
        )
        try:
            documents = ["字段 a 的说明", "字段 b 的说明"]
            index.build(documents)
            index.conn.execute(f"TRUNCATE {index.table}")

            index.build(documents)
            self.assertEqual(len(index.search("字段 a", top_k=2)), 2)
        finally:
            index.conn.execute("DROP TABLE IF EXISTS ai_sql_vectors_unittest_wiped")
            index.conn.execute(
                "DELETE FROM ai_sql_vector_meta WHERE namespace = 'unittest_wiped'"
            )
            index.conn.close()

    def test_vector_backend_factory_selects_pgvector(self):
        from ai_sql_agent.pipeline.local_clients import LocalHashEmbeddingClient
        from ai_sql_agent.retrieval.pgvector_index import PgVectorIndex
        from ai_sql_agent.retrieval.vector_backend import create_vector_index

        index = create_vector_index(
            LocalHashEmbeddingClient(), namespace="unittest_factory"
        )
        try:
            self.assertIsInstance(index, PgVectorIndex)
        finally:
            index.conn.execute(f"DROP TABLE IF EXISTS {index.table}")
            index.conn.close()


@requires_pgvector
class PgVectorLongTermMemoryTest(unittest.TestCase):
    def setUp(self):
        from ai_sql_agent.memory.vector_store import (
            PgVectorLongTermMemoryConfig,
            PgVectorLongTermVectorIndex,
        )

        self.index = PgVectorLongTermVectorIndex(
            PgVectorLongTermMemoryConfig(
                dsn=DSN,
                table_name="unittest_long_term_memory",
                embedding_dimensions=4,
            )
        )

    def tearDown(self):
        self.index.conn.execute(f"DROP TABLE IF EXISTS {self.index.table}")
        self.index.close()

    def _memory(self, memory_id, user_id, kind, vector):
        from ai_sql_agent.memory.objects import LongTermMemory

        return LongTermMemory(
            id=memory_id,
            user_id=user_id,
            kind=kind,
            summary=f"记忆 {memory_id}",
            content={},
            embedding=np.array(vector, dtype=np.float32),
        )

    def test_upsert_search_delete_and_user_isolation(self):
        from ai_sql_agent.memory.objects import MemoryKind

        self.index.upsert(
            self._memory("m1", "u1", MemoryKind.STRUCTURED, [1, 0, 0, 0])
        )
        self.index.upsert(
            self._memory("m2", "u1", MemoryKind.UNSTRUCTURED, [0, 1, 0, 0])
        )
        self.index.upsert(
            self._memory("m3", "u2", MemoryKind.STRUCTURED, [1, 0, 0, 0])
        )

        query = np.array([1, 0, 0, 0], dtype=np.float32)

        hits = self.index.search(user_id="u1", query_embedding=query, top_k=5)
        self.assertEqual([hit[0] for hit in hits], ["m1", "m2"])
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)

        filtered = self.index.search(
            user_id="u1",
            query_embedding=query,
            top_k=5,
            kinds=[MemoryKind.UNSTRUCTURED],
        )
        self.assertEqual([hit[0] for hit in filtered], ["m2"])

        self.index.delete(user_id="u1", memory_id="m1")
        remaining = self.index.search(user_id="u1", query_embedding=query, top_k=5)
        self.assertEqual([hit[0] for hit in remaining], ["m2"])

    def test_existing_table_with_other_dimensions_is_rejected(self):
        from ai_sql_agent.memory.vector_store import (
            PgVectorLongTermMemoryConfig,
            PgVectorLongTermVectorIndex,
        )

        # 换了 Embedding 模型（维度变化）却复用旧表：必须在建实例时就失败
        with self.assertRaises(ValueError) as ctx:
            PgVectorLongTermVectorIndex(
                PgVectorLongTermMemoryConfig(
                    dsn=DSN,
                    table_name=self.index.table,
                    embedding_dimensions=1024,
                )
            )
        self.assertIn("维度", str(ctx.exception))

    def test_dimension_mismatch_is_rejected(self):
        from ai_sql_agent.memory.objects import MemoryKind

        with self.assertRaises(ValueError):
            self.index.upsert(
                self._memory("bad", "u1", MemoryKind.STRUCTURED, [1, 0, 0])
            )


@requires_pgvector
class PostgresExecutorTest(unittest.TestCase):
    TABLE = "unittest_pg_exec"

    def setUp(self):
        import psycopg

        from ai_sql_agent.execution import PostgresMCPExecutor

        self.conn = psycopg.connect(DSN, autocommit=True)
        self.conn.execute(f"DROP TABLE IF EXISTS {self.TABLE}")
        self.conn.execute(
            f"CREATE TABLE {self.TABLE} (user_id INT PRIMARY KEY, supplier_name NUMERIC)"
        )
        self.conn.execute(
            f"INSERT INTO {self.TABLE} VALUES (1001, 4.2), (1002, 3.9), (1003, 5.1)"
        )
        self.executor = PostgresMCPExecutor(
            database="pg_db", dsn=DSN, max_rows=2, max_execution_seconds=1.0
        )

    def tearDown(self):
        self.conn.execute(f"DROP TABLE IF EXISTS {self.TABLE}")
        self.conn.close()

    def execute(self, sql):
        from ai_sql_agent.auth import authorization_scope
        from ai_sql_agent.execution import MCPExecutionRequest
        from ai_sql_agent.retrieval.access import ALLOW_ALL

        # 执行器入口有强制授权关卡，不绑作用域一律拒绝（见 ai_sql_agent.auth.enforcement）。
        # 这组用例验的是行数上限与语句超时，权限维度用全放开策略排除掉；
        # 真实 PostgreSQL 上的权限用例在 tests/test_authorization_postgres.py。
        with authorization_scope(ALLOW_ALL):
            return self.executor.execute(
                MCPExecutionRequest(database="pg_db", sql=sql)
            )

    def test_select_with_row_cap(self):
        result = self.execute(f"SELECT user_id FROM {self.TABLE} ORDER BY user_id")
        self.assertTrue(result.success)
        self.assertEqual(result.row_count, 2)
        self.assertTrue(result.truncated)
        self.assertEqual(result.columns, ["user_id"])

    def test_write_is_rejected(self):
        result = self.execute(f"UPDATE {self.TABLE} SET supplier_name = 0")
        self.assertFalse(result.success)

    def test_statement_timeout_aborts_long_query(self):
        # 不能再用 pg_sleep：只读校验把它列进了禁用函数（它会让一条连接白白挂到超时）。
        # 换成一条真跑得慢的查询，仍然由会话级 statement_timeout 中止。
        result = self.execute("SELECT count(*) FROM generate_series(1, 2000000000)")
        self.assertFalse(result.success)
        self.assertIn("超时", result.error)

    def test_pg_sleep_is_stopped_by_the_guard_before_it_runs(self):
        result = self.execute("SELECT pg_sleep(3)")
        self.assertFalse(result.success)
        self.assertIn("pg_sleep", result.error)

    def test_readonly_transaction_blocks_write_at_db_layer(self):
        import psycopg

        conn = psycopg.connect(DSN, autocommit=False)
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(f"UPDATE {self.TABLE} SET supplier_name = 0")
        finally:
            conn.rollback()
            conn.close()


@requires_pgvector
class PostgresSchemaLoaderTest(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.conn = psycopg.connect(DSN, autocommit=True)
        self.conn.execute("DROP TABLE IF EXISTS unittest_member")
        self.conn.execute("DROP TABLE IF EXISTS unittest_project")
        self.conn.execute(
            "CREATE TABLE unittest_project (project_id INT PRIMARY KEY, budget NUMERIC)"
        )
        self.conn.execute(
            """
            CREATE TABLE unittest_member (
                id INT PRIMARY KEY,
                project_id INT REFERENCES unittest_project(project_id),
                user_id INT
            )
            """
        )
        self.conn.execute("INSERT INTO unittest_project VALUES (1, 2000000)")
        self.conn.execute("INSERT INTO unittest_member VALUES (1, 1, 1003)")

    def tearDown(self):
        self.conn.execute("DROP TABLE IF EXISTS unittest_member")
        self.conn.execute("DROP TABLE IF EXISTS unittest_project")
        self.conn.close()

    def test_loads_tables_columns_and_relations(self):
        from ai_sql_agent.retrieval.postgres_loader import PostgresSchemaLoader

        tables, columns, relations = PostgresSchemaLoader(
            dsn=DSN, database_name="pg_db"
        ).load()

        self.assertIn("unittest_project", tables)
        self.assertEqual(tables["unittest_project"].primary_keys, ["project_id"])

        member_columns = {
            column.column_name
            for column in columns
            if column.table_name == "unittest_member"
        }
        self.assertEqual(member_columns, {"id", "project_id", "user_id"})

        fk = [
            relation
            for relation in relations
            if relation.source_table == "unittest_member"
        ]
        self.assertEqual(len(fk), 1)
        self.assertEqual(fk[0].target_table, "unittest_project")

        samples = [
            column.samples
            for column in columns
            if column.table_name == "unittest_project"
            and column.column_name == "budget"
        ]
        self.assertEqual(samples, [["2000000"]])

    def test_ai_sql_infra_tables_are_excluded(self):
        from ai_sql_agent.retrieval.postgres_loader import PostgresSchemaLoader

        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_sql_vectors_unittest (id INT PRIMARY KEY)"
        )
        try:
            tables, _, _ = PostgresSchemaLoader(dsn=DSN, database_name="pg_db").load()
            self.assertNotIn("ai_sql_vectors_unittest", tables)
        finally:
            self.conn.execute("DROP TABLE IF EXISTS ai_sql_vectors_unittest")


if __name__ == "__main__":
    unittest.main()
