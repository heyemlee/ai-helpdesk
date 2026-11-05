"""
授权边界在**真实 PostgreSQL** 上的验收。

SQLite 上验不到的三件事：
- **不同 schema 的同名表**（SQLite 没有 schema）；
- **列级 GRANT**：数据库自己拒绝读禁列，应用层漏判也越不了权；
- **行级安全（RLS）**：行范围只能落在数据库侧。

没有 `AI_SQL_PGVECTOR_DSN` 时整组跳过；CI 里有 pgvector 服务，这组必跑。
"""
from __future__ import annotations

import os
import unittest
import uuid

from ai_sql_agent.auth import DataPolicy, authorization_scope, check_sql_access
from ai_sql_agent.execution import MCPExecutionRequest, PostgresMCPExecutor

DSN = os.getenv("AI_SQL_PGVECTOR_DSN", "")

requires_postgres = unittest.skipUnless(
    DSN, "未配置 AI_SQL_PGVECTOR_DSN，跳过 PostgreSQL 授权集成测试"
)

SUFFIX = uuid.uuid4().hex[:8]
PUBLIC_SCHEMA = f"authpub_{SUFFIX}"
SECRET_SCHEMA = f"authsec_{SUFFIX}"
RO_ROLE = f"auth_ro_{SUFFIX}"
RO_PASSWORD = f"pw-{uuid.uuid4().hex}"


def _admin():
    import psycopg

    return psycopg.connect(DSN, autocommit=True)


def _ro_dsn() -> str:
    """把 DSN 的用户名密码换成受限角色，其余（主机/库）照抄。"""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(DSN)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((
        parts.scheme, f"{RO_ROLE}:{RO_PASSWORD}@{host}{port}", parts.path, "", ""
    ))


@requires_postgres
class PostgresAuthorizationTest(unittest.TestCase):
    """
    两个 schema、同名表 `orders`：一个公开、一个机密。
    再加一张有禁列的 `batch`。
    """

    @classmethod
    def setUpClass(cls):
        conn = _admin()
        try:
            conn.execute(f"CREATE SCHEMA {PUBLIC_SCHEMA}")
            conn.execute(f"CREATE SCHEMA {SECRET_SCHEMA}")
            conn.execute(
                f"CREATE TABLE {PUBLIC_SCHEMA}.orders "
                "(order_no TEXT PRIMARY KEY, amount NUMERIC)"
            )
            conn.execute(
                f"INSERT INTO {PUBLIC_SCHEMA}.orders VALUES ('PUB-1', 10)"
            )
            # 同名表，另一个 schema——旧实现把两者都砍成 `orders`，一条规则同时放行
            conn.execute(
                f"CREATE TABLE {SECRET_SCHEMA}.orders "
                "(order_no TEXT PRIMARY KEY, margin NUMERIC)"
            )
            conn.execute(
                f"INSERT INTO {SECRET_SCHEMA}.orders VALUES ('SEC-1', 999)"
            )
            conn.execute(
                f"CREATE TABLE {PUBLIC_SCHEMA}.batch "
                "(sku TEXT PRIMARY KEY, unit_cost NUMERIC, owner_id TEXT)"
            )
            conn.execute(
                f"INSERT INTO {PUBLIC_SCHEMA}.batch VALUES "
                "('SKU-1', 42, 'alice'), ('SKU-2', 84, 'bob')"
            )
            conn.execute(
                f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{RO_PASSWORD}'"
            )
            conn.execute(f"GRANT USAGE ON SCHEMA {PUBLIC_SCHEMA} TO {RO_ROLE}")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = _admin()
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {PUBLIC_SCHEMA} CASCADE")
            conn.execute(f"DROP SCHEMA IF EXISTS {SECRET_SCHEMA} CASCADE")
            conn.execute(f"DROP OWNED BY {RO_ROLE} CASCADE")
            conn.execute(f"DROP ROLE IF EXISTS {RO_ROLE}")
        except Exception:
            pass
        finally:
            conn.close()

    # ── 应用层：同名不同 schema 必须能区分 ──

    def test_same_table_name_in_two_schemas_is_distinguishable(self):
        policy = DataPolicy(allow_tables=(f"pg_db.{PUBLIC_SCHEMA}.orders",))
        allowed = check_sql_access(
            policy, database="pg_db",
            sql=f"SELECT order_no FROM {PUBLIC_SCHEMA}.orders", dialect="postgres",
        )
        self.assertTrue(allowed.allowed, allowed.reason)

        denied = check_sql_access(
            policy, database="pg_db",
            sql=f"SELECT margin FROM {SECRET_SCHEMA}.orders", dialect="postgres",
        )
        self.assertFalse(denied.allowed)
        self.assertIn(f"{SECRET_SCHEMA}.orders", denied.denied_tables)

    def test_executor_blocks_the_secret_schema(self):
        executor = PostgresMCPExecutor(database="pg_db", dsn=DSN)
        policy = DataPolicy(allow_tables=(f"pg_db.{PUBLIC_SCHEMA}.orders",))
        with authorization_scope(policy):
            result = executor.execute(MCPExecutionRequest(
                database="pg_db",
                sql=f"SELECT margin FROM {SECRET_SCHEMA}.orders",
            ))
        executor.close()
        self.assertFalse(result.success)
        self.assertEqual(result.rows, [])
        self.assertIn("没有访问权限", result.error)

    def test_permitted_schema_still_works(self):
        """反向断言：一个「永远拒绝」的实现也能让上面两条变绿。"""
        executor = PostgresMCPExecutor(database="pg_db", dsn=DSN)
        policy = DataPolicy(allow_tables=(f"pg_db.{PUBLIC_SCHEMA}.orders",))
        with authorization_scope(policy):
            result = executor.execute(MCPExecutionRequest(
                database="pg_db",
                sql=f"SELECT order_no FROM {PUBLIC_SCHEMA}.orders",
            ))
        executor.close()
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [{"order_no": "PUB-1"}])

    # ── 数据库侧：应用层漏判也越不了权 ──

    def test_column_grant_blocks_a_denied_column_at_the_database(self):
        """
        这一条是数据库侧最小授权的核心：**绕过应用层**直接用受限角色连库，
        禁列照样查不出来。只读账号只挡写，挡不住读密列。
        """
        from ai_sql_agent.ops.least_privilege import (
            TableInventory, build_plan, verify_least_privilege,
        )

        policy = DataPolicy(
            allow_tables=(f"pg_db.{PUBLIC_SCHEMA}.orders", f"pg_db.{PUBLIC_SCHEMA}.batch"),
            deny_columns=(f"pg_db.{PUBLIC_SCHEMA}.batch.unit_cost",),
        )
        inventory = TableInventory.from_database(DSN, schema=PUBLIC_SCHEMA)
        plan = build_plan(
            policy, inventory, database="pg_db",
            role=RO_ROLE, schema=PUBLIC_SCHEMA,
        )
        # 策略里的表名带 schema 前缀，inventory 给的是裸名——build_plan 用裸名判，
        # 所以这里用一个只按裸名写的等价策略，避免用例验的是名字拼法而不是授权
        bare = DataPolicy(
            allow_tables=("pg_db.orders", "pg_db.batch"),
            deny_columns=("pg_db.batch.unit_cost",),
        )
        plan = build_plan(
            bare, inventory, database="pg_db", role=RO_ROLE, schema=PUBLIC_SCHEMA
        )
        self.assertIn("batch", plan.column_grants)
        self.assertNotIn("unit_cost", plan.column_grants["batch"])

        conn = _admin()
        try:
            conn.execute(plan.to_sql())
        finally:
            conn.close()

        report = verify_least_privilege(_ro_dsn(), plan)
        self.assertTrue(
            report.enforced,
            f"数据库侧没拦住：{report.problems()}",
        )

        # 直接查一次，证明 verify 不是在自证
        import psycopg

        ro = psycopg.connect(_ro_dsn(), autocommit=True)
        try:
            self.assertEqual(
                ro.execute(f"SELECT sku FROM {PUBLIC_SCHEMA}.batch ORDER BY sku")
                  .fetchall(),
                [("SKU-1",), ("SKU-2",)],
                "允许的列必须还能查——只验拒绝的话，全拒也能过",
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                ro.execute(f"SELECT unit_cost FROM {PUBLIC_SCHEMA}.batch").fetchall()
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                ro.execute(f"SELECT * FROM {PUBLIC_SCHEMA}.batch").fetchall()
        finally:
            ro.close()

    def test_row_level_security_scopes_rows(self):
        """
        行隔离只能落在数据库侧：`DataPolicy` 的行范围
        最终也落到 RLS 上。运维给条件，RLS 执行它。
        """
        import psycopg

        from ai_sql_agent.ops.least_privilege import row_policy_sql

        conn = _admin()
        try:
            conn.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.batch TO {RO_ROLE}")
            conn.execute(row_policy_sql(
                role=RO_ROLE,
                table="batch",
                schema=PUBLIC_SCHEMA,
                using_expression="owner_id = current_setting('app.user_id', true)",
                policy_name=f"batch_owner_{SUFFIX}",
            ))
        finally:
            conn.close()

        ro = psycopg.connect(_ro_dsn(), autocommit=True)
        try:
            ro.execute("SET app.user_id = 'alice'")
            self.assertEqual(
                ro.execute(f"SELECT sku FROM {PUBLIC_SCHEMA}.batch").fetchall(),
                [("SKU-1",)],
                "alice 只应看到自己的行",
            )
            ro.execute("SET app.user_id = 'bob'")
            self.assertEqual(
                ro.execute(f"SELECT sku FROM {PUBLIC_SCHEMA}.batch").fetchall(),
                [("SKU-2",)],
            )
            ro.execute("RESET app.user_id")
            self.assertEqual(
                ro.execute(f"SELECT sku FROM {PUBLIC_SCHEMA}.batch").fetchall(),
                [],
                "没声明身份时一行都不该看到（fail closed）",
            )
        finally:
            ro.close()


@requires_postgres
class RowLevelSecurityThroughTheAgentTest(unittest.TestCase):
    """
    行隔离在**经由 agent 发起的查询**上生效。

    上一轮只证明了「有人手工连库时 RLS 管用」——那不够：真实流量都从执行器走，
    而执行器以前不告诉数据库「这次是谁在查」。现在它在事务里
    `set_config('app.user_id', …, true)`，RLS 才真的能按人过滤。

    `SET LOCAL` 是事务级的，随 rollback 还原，所以连接池复用不会串身份——
    这条也一起验（同一个执行器连着换两个身份查）。
    """

    SCHEMA = f"authrls_{SUFFIX}"
    ROLE = f"rls_ro_{SUFFIX}"
    PASSWORD = f"pw-{uuid.uuid4().hex}"

    @classmethod
    def setUpClass(cls):
        conn = _admin()
        try:
            conn.execute(f"CREATE SCHEMA {cls.SCHEMA}")
            conn.execute(
                f"CREATE TABLE {cls.SCHEMA}.job "
                "(job_no TEXT PRIMARY KEY, owner_id TEXT, amount NUMERIC)"
            )
            conn.execute(
                f"INSERT INTO {cls.SCHEMA}.job VALUES "
                "('J-A', 'alice', 10), ('J-B', 'bob', 20), ('J-C', 'alice', 30)"
            )
            conn.execute(f"CREATE ROLE {cls.ROLE} LOGIN PASSWORD '{cls.PASSWORD}'")
            conn.execute(f"GRANT USAGE ON SCHEMA {cls.SCHEMA} TO {cls.ROLE}")
            conn.execute(f"GRANT SELECT ON {cls.SCHEMA}.job TO {cls.ROLE}")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = _admin()
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {cls.SCHEMA} CASCADE")
            conn.execute(f"DROP OWNED BY {cls.ROLE} CASCADE")
            conn.execute(f"DROP ROLE IF EXISTS {cls.ROLE}")
        except Exception:
            pass
        finally:
            conn.close()

    def _ro_dsn(self) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(DSN)
        host = parts.hostname or "localhost"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((
            parts.scheme, f"{self.ROLE}:{self.PASSWORD}@{host}{port}", parts.path, "", ""
        ))

    def _install_rls(self):
        """RLS 由策略生成，不是手写——这正是行范围进 DataPolicy 之后多出来的能力。"""
        from ai_sql_agent.ops.least_privilege import TableInventory, row_policies_from_policy

        policy = DataPolicy.from_dict({
            "allow_tables": ["pg_db.*"],
            "row_filters": [
                {"table": "pg_db.job",
                 "predicate": "owner_id = current_setting('app.user_id', true)"},
            ],
        })
        inventory = TableInventory.from_database(DSN, schema=self.SCHEMA)
        statements = row_policies_from_policy(
            policy, inventory, database="pg_db", role=self.ROLE, schema=self.SCHEMA
        )
        self.assertEqual(len(statements), 1, "只有 job 声明了非空谓词")
        conn = _admin()
        try:
            for statement in statements:
                conn.execute(statement)
        finally:
            conn.close()

    def test_each_user_only_sees_their_own_rows_through_the_executor(self):
        from ai_sql_agent.auth import authorization_scope

        self._install_rls()
        executor = PostgresMCPExecutor(database="pg_db", dsn=self._ro_dsn())
        sql = f"SELECT job_no FROM {self.SCHEMA}.job ORDER BY job_no"
        policy = DataPolicy(allow_tables=("pg_db.*",))
        try:
            with authorization_scope(policy, user_id="alice"):
                alice = executor.execute(MCPExecutionRequest(database="pg_db", sql=sql))
            # 同一个执行器、同一个连接池，紧接着换一个身份——不能串
            with authorization_scope(policy, user_id="bob"):
                bob = executor.execute(MCPExecutionRequest(database="pg_db", sql=sql))
        finally:
            executor.close()

        self.assertTrue(alice.success, alice.error)
        self.assertEqual([r["job_no"] for r in alice.rows], ["J-A", "J-C"])
        self.assertTrue(bob.success, bob.error)
        self.assertEqual([r["job_no"] for r in bob.rows], ["J-B"])

    def test_without_an_authorization_scope_no_rows_come_back(self):
        """
        没有绑定作用域时 app.user_id 是空串，RLS 谓词不匹配任何行——fail closed，
        与执行器那道关卡方向一致。
        """
        from ai_sql_agent.auth import authorization_scope
        from ai_sql_agent.retrieval.access import ALLOW_ALL

        self._install_rls()
        executor = PostgresMCPExecutor(database="pg_db", dsn=self._ro_dsn())
        try:
            # 用 ALLOW_ALL 过掉应用层那关，单独验数据库这一层
            with authorization_scope(ALLOW_ALL):
                result = executor.execute(MCPExecutionRequest(
                    database="pg_db",
                    sql=f"SELECT job_no FROM {self.SCHEMA}.job",
                ))
        finally:
            executor.close()
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [], "没有身份就一行都不该看到")


if __name__ == "__main__":
    unittest.main()
