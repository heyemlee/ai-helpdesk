import os
import unittest

from ai_sql_agent.ops import ReadonlyAudit, ReadonlyRoleSpec

DSN = os.getenv("AI_SQL_PGVECTOR_DSN", "")


class ReadonlyRoleSpecTest(unittest.TestCase):
    def test_generates_password_when_absent(self):
        spec = ReadonlyRoleSpec()
        self.assertGreaterEqual(len(spec.password), 20)

    def test_rejects_injectable_identifiers(self):
        for field in ("role", "database", "schema"):
            with self.assertRaises(ValueError, msg=field):
                ReadonlyRoleSpec(**{field: "x; DROP TABLE t"})

    def test_sql_covers_the_easy_to_forget_parts(self):
        sql = ReadonlyRoleSpec(role="ro", database="db", password="p").to_sql()
        # 新建的表也要自动只给读，否则业务每加一张表都要找 DBA
        self.assertIn("ALTER DEFAULT PRIVILEGES", sql)
        # 收回 PUBLIC 上的建表权限
        self.assertIn("REVOKE CREATE ON SCHEMA public FROM PUBLIC", sql)
        # 数据库层兜底的超时与只读事务
        self.assertIn("statement_timeout", sql)
        self.assertIn("default_transaction_read_only = on", sql)

    def test_password_quote_is_escaped(self):
        sql = ReadonlyRoleSpec(role="ro", database="db", password="a'b").to_sql()
        self.assertIn("'a''b'", sql)

    def test_dsn_uses_the_generated_password(self):
        spec = ReadonlyRoleSpec(role="ro", database="db", password="pw")
        self.assertEqual(spec.dsn("h", 6543), "postgresql://ro:pw@h:6543/db")


class ReadonlyAuditVerdictTest(unittest.TestCase):
    def test_clean_audit_is_readonly(self):
        self.assertTrue(ReadonlyAudit(user="ro").is_readonly)

    def test_superuser_is_flagged(self):
        audit = ReadonlyAudit(user="postgres", is_superuser=True)
        self.assertFalse(audit.is_readonly)
        self.assertIn("超级用户", audit.problems()[0])

    def test_writable_tables_are_flagged(self):
        audit = ReadonlyAudit(user="app", writable_tables=["orders", "users"])
        self.assertFalse(audit.is_readonly)
        self.assertIn("2 张表", audit.problems()[0])

    def test_create_privilege_is_flagged(self):
        self.assertFalse(
            ReadonlyAudit(user="app", can_create_in_schema=True).is_readonly
        )

    def test_connection_error_is_not_reported_as_readonly(self):
        self.assertFalse(ReadonlyAudit(user="unknown", errors=["连不上"]).is_readonly)


@unittest.skipUnless(DSN, "未配置 AI_SQL_PGVECTOR_DSN，跳过只读账号集成测试")
class ReadonlyProvisionIntegrationTest(unittest.TestCase):
    ROLE = "ai_sql_ro_unittest"

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.ops import provision_readonly_role

        import psycopg

        with psycopg.connect(DSN, autocommit=True) as conn:
            cls.database = conn.execute("SELECT current_database()").fetchone()[0]

        cls.spec = provision_readonly_role(
            DSN,
            ReadonlyRoleSpec(
                role=cls.ROLE, database=cls.database, password="UnitTestPw123"
            ),
        )
        cls.readonly_dsn = cls.spec.dsn(
            host="localhost", port=int(os.getenv("AI_SQL_PG_PORT", "5432"))
        )

    @classmethod
    def tearDownClass(cls):
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {cls.ROLE}"
            )
            conn.execute(
                f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {cls.ROLE}"
            )
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"REVOKE SELECT ON TABLES FROM {cls.ROLE}"
            )
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"REVOKE SELECT ON SEQUENCES FROM {cls.ROLE}"
            )
            conn.execute(f"REVOKE ALL ON SCHEMA public FROM {cls.ROLE}")
            conn.execute(
                f"REVOKE ALL ON DATABASE {cls.database} FROM {cls.ROLE}"
            )
            conn.execute(f"DROP ROLE IF EXISTS {cls.ROLE}")

    def test_provisioned_role_passes_the_audit(self):
        from ai_sql_agent.ops import audit_readonly

        audit = audit_readonly(self.readonly_dsn)
        self.assertTrue(audit.is_readonly, audit.problems())
        self.assertTrue(audit.default_transaction_read_only)

    def test_admin_account_fails_the_audit(self):
        from ai_sql_agent.ops import audit_readonly

        audit = audit_readonly(DSN)
        self.assertFalse(audit.is_readonly)

    def test_writes_are_rejected_at_the_database_layer(self):
        """第三层防线：连应用层和连接层都绕过了，账号本身也写不动。"""
        import psycopg

        with psycopg.connect(self.readonly_dsn, autocommit=True) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1").fetchone())

            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TABLE ai_sql_ro_probe (id int)")

        with psycopg.connect(self.readonly_dsn, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("DELETE FROM ai_sql_audit_log")


if __name__ == "__main__":
    unittest.main()
