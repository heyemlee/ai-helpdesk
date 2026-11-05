import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.execution import MCPExecutionRequest, SQLiteMCPExecutor, check_readonly_sql
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.execution.dialects import POSTGRES, SQLITE, get_dialect


class SqlGuardTest(unittest.TestCase):
    def test_allows_plain_select_and_cte(self):
        self.assertTrue(check_readonly_sql("SELECT 1").allowed)
        self.assertTrue(check_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x").allowed)
        self.assertTrue(check_readonly_sql("-- 注释\nSELECT 2").allowed)
        self.assertTrue(check_readonly_sql("SELECT * FROM t /* 注释 */ LIMIT 1").allowed)

    def test_rejects_write_statements(self):
        for sql in [
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "INSERT INTO t VALUES (1)",
            "DROP TABLE t",
        ]:
            self.assertFalse(check_readonly_sql(sql).allowed, sql)

    def test_rejects_multi_statement(self):
        result = check_readonly_sql("SELECT 1; DROP TABLE t")
        self.assertFalse(result.allowed)
        self.assertIn("一条", result.reason)

    def test_rejects_write_hidden_behind_cte(self):
        # 只看首关键字会放行，这里必须靠全语句关键字扫描拦住
        result = check_readonly_sql("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")
        self.assertFalse(result.allowed)

    def test_rejects_attach_pragma_and_file_functions(self):
        self.assertFalse(check_readonly_sql("ATTACH DATABASE '/tmp/e.db' AS e").allowed)
        self.assertFalse(check_readonly_sql("PRAGMA table_info(t)").allowed)
        self.assertFalse(check_readonly_sql("SELECT load_extension('x')").allowed)
        self.assertFalse(check_readonly_sql("SELECT writefile('/tmp/x', 'y')").allowed)

    def test_semicolon_inside_string_literal_is_fine(self):
        self.assertTrue(
            check_readonly_sql("SELECT * FROM t WHERE name = 'a; DROP TABLE t'").allowed
        )

    def test_empty_sql_rejected(self):
        self.assertFalse(check_readonly_sql("   ").allowed)
        self.assertFalse(check_readonly_sql(";").allowed)


class SqlGuardAstTest(unittest.TestCase):
    """
    只读校验按语法树判（2026-10 起）。每条用例都对两种方言各跑一遍：
    执行器传自己的方言，两种结果必须一致，除非用例本身就是方言差异。
    """

    DIALECTS = ("sqlite", "postgres")

    def assertAllowed(self, sql, dialects=DIALECTS):
        for dialect in dialects:
            result = check_readonly_sql(sql, dialect=dialect)
            self.assertTrue(result.allowed, f"[{dialect}] {sql!r}: {result.reason}")

    def assertRejected(self, sql, dialects=DIALECTS, reason=None):
        for dialect in dialects:
            result = check_readonly_sql(sql, dialect=dialect)
            self.assertFalse(result.allowed, f"[{dialect}] {sql!r} 不该放行")
            self.assertTrue(result.reason, f"[{dialect}] {sql!r} 拒绝时必须给原因")
            if reason:
                self.assertIn(reason, result.reason, f"[{dialect}] {sql!r}")

    # ---- 放行：只读查询的各种形状 ----

    def test_allows_read_only_query_shapes(self):
        for sql in [
            "SELECT 1",
            "SELECT 1;",
            "SELECT 1; -- 结尾注释",
            "select a, count(*) from t group by a having count(*) > 1 order by 2 desc limit 5",
            "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
            "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r WHERE x < 5) SELECT * FROM r",
            "SELECT 1 UNION SELECT 2",
            "SELECT 1 UNION ALL SELECT 2",
            "SELECT 1 INTERSECT SELECT 1",
            "SELECT 1 EXCEPT SELECT 2",
            "(SELECT 1) UNION (SELECT 2)",
            "(SELECT 1)",
            "VALUES (1, 'a'), (2, 'b')",
            "SELECT * FROM (SELECT 1 AS x) s WHERE x IN (SELECT 1)",
            "SELECT a, row_number() OVER (PARTITION BY b ORDER BY c) FROM t",
            "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END FROM t",
            "SELECT replace(name, 'a', 'b') FROM t",
            "SELECT * FROM t LIMIT ?",
        ]:
            self.assertAllowed(sql)

    def test_keywords_inside_strings_comments_and_quoted_identifiers_are_not_hits(self):
        for sql in [
            "SELECT * FROM t WHERE name = 'a; DROP TABLE t'",
            "SELECT * FROM t WHERE name = 'delete' OR note = 'insert into x'",
            "SELECT * FROM t WHERE name = 'it''s; update'",
            'SELECT "update", "Last Update" FROM "set"',
            "SELECT 1 AS \"drop\" FROM \"into\"",
            "SELECT a FROM t -- DROP TABLE t",
            "SELECT a /* ; DROP TABLE t */ FROM t",
            "SELECT a FROM t WHERE b = 'x' /* 'unbalanced */",
        ]:
            self.assertAllowed(sql)

    # ---- 拒绝：写操作、DDL、会话控制 ----

    def test_rejects_every_non_query_statement(self):
        for sql in [
            "INSERT INTO t VALUES (1)",
            "INSERT OR REPLACE INTO t VALUES (1)",
            "INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING",
            "REPLACE INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE",
            "CREATE TABLE a (x INT)",
            "CREATE TABLE a AS SELECT 1",
            "CREATE TEMP TABLE a AS SELECT 1",
            "CREATE VIEW v AS SELECT 1",
            "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN c INT",
            "TRUNCATE t",
            "GRANT SELECT ON t TO u",
            "REVOKE SELECT ON t FROM u",
            "PRAGMA table_info(t)",
            "PRAGMA writable_schema = 1",
            "VACUUM",
            "ANALYZE t",
            "REINDEX t",
            "BEGIN",
            "BEGIN IMMEDIATE",
            "COMMIT",
            "END",
            "ROLLBACK",
            "SAVEPOINT a",
            "RELEASE a",
            "SET search_path = x",
            "SET LOCAL statement_timeout = 0",
            "RESET all",
            "COPY t TO '/tmp/x'",
            "DO $$ BEGIN END $$",
            "CALL p()",
            "EXPLAIN SELECT 1",
            "EXPLAIN ANALYZE DELETE FROM t",
            "SHOW all",
            "LOCK TABLE t",
            "LISTEN c",
            "NOTIFY c",
            "TABLE t",
        ]:
            self.assertRejected(sql)

    def test_rejects_sqlite_only_statements(self):
        # ATTACH / DETACH 在 PostgreSQL 方言里解析不了，同样是拒绝
        self.assertRejected("ATTACH DATABASE '/tmp/e.db' AS e")
        self.assertRejected("DETACH DATABASE e")

    def test_rejects_writes_hidden_inside_a_query(self):
        for sql in [
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
            "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
            "WITH a AS (SELECT 1), b AS (DELETE FROM t RETURNING a) SELECT * FROM a",
            "SELECT * INTO new_t FROM t",
            "SELECT * INTO TEMP new_t FROM t",
            "SELECT * FROM t FOR UPDATE",
            "SELECT * FROM t FOR SHARE",
        ]:
            self.assertRejected(sql)

    def test_cte_write_reports_where_it_was_found(self):
        result = check_readonly_sql(
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", dialect="postgres"
        )
        self.assertFalse(result.allowed)
        self.assertIn("DELETE", result.reason)
        self.assertIn("WITH", result.reason)

    # ---- 拒绝：多语句与词法夹带 ----

    def test_rejects_multiple_statements_however_they_are_smuggled(self):
        for sql in [
            "SELECT 1; DROP TABLE t",
            "SELECT 1;DROP TABLE t",
            "SELECT 1 ; -- x\nDROP TABLE t",
            "SELECT 1 /* c */; DROP TABLE t",
            "SELECT 1; /* c */ DELETE FROM t",
            "SELECT 1;\n\n   UPDATE t SET a = 1",
            "SELECT 1; SELECT 2",
            # 旧实现先删 `--` 再认字符串：引号里的 `--` 把后半条语句当注释删掉了
            "SELECT '--' AS x; DROP TABLE t",
            "SELECT '/*' AS x; DROP TABLE t; SELECT '*/'",
            # 反斜杠不是单引号字符串的转义符：`'a\\'` 到此为止
            "SELECT 'a\\' ; DROP TABLE t; --'",
        ]:
            self.assertRejected(sql, reason="一条")

    def test_case_and_whitespace_variations_change_nothing(self):
        for sql in [
            "sElEcT 1; dRoP tAbLe t",
            "SELECT 1;\tDROP\nTABLE\r\nt",
            "dElEtE fRoM t",
            "wItH x As (sElEcT 1) iNsErT iNtO t sElEcT * fRoM x",
            "/**/DROP/**/TABLE/**/t",
            "   \n  UPDATE t SET a = 1",
        ]:
            self.assertRejected(sql)
        self.assertAllowed("   \n sElEcT   1 \n")

    def test_postgres_dollar_quoting_follows_the_dialect(self):
        # PostgreSQL 里 `$$…$$` 是字符串，整条就是一条 SELECT；SQLite 里不是，
        # 它会把分号后面的 DROP 当成第二条语句。两种方言都要先放行才算放行。
        sql = "SELECT $$; DROP TABLE t; $$"
        self.assertTrue(check_readonly_sql(sql, dialect="postgres").allowed)
        self.assertFalse(check_readonly_sql(sql, dialect="sqlite").allowed)
        self.assertFalse(check_readonly_sql(sql).allowed)

    def test_nested_comments_follow_the_dialect(self):
        # PostgreSQL 的块注释可嵌套，SQLite 的不行：同一段文字在两边是两条不同的语句。
        sql = "SELECT 1 /* /* */ ; DROP TABLE t; /* */ */"
        self.assertTrue(check_readonly_sql(sql, dialect="postgres").allowed)
        self.assertFalse(check_readonly_sql(sql, dialect="sqlite").allowed)
        self.assertFalse(check_readonly_sql(sql).allowed)

    # ---- 拒绝：危险函数 ----

    def test_rejects_dangerous_functions_in_both_dialects(self):
        calls = [
            "load_extension('x')",
            "LOAD_EXTENSION('x')",
            "main.load_extension('x')",
            '"load_extension"(\'x\')',
            "writefile('/tmp/x', 'y')",
            "readfile('/etc/passwd')",
            "edit('x')",
            "fts3_tokenizer('simple')",
            "pg_read_file('/etc/passwd')",
            "pg_read_binary_file('/etc/passwd')",
            "pg_catalog.pg_read_file('/etc/passwd')",
            "pg_ls_dir('/')",
            "pg_sleep(10)",
            "pg_catalog.pg_sleep(10)",
            "pg_advisory_lock(1)",
            "pg_terminate_backend(1)",
            "pg_cancel_backend(1)",
            "set_config('app.user_id', 'x', false)",
            "pg_catalog.set_config('app.user_id', 'x', false)",
            "nextval('s')",
            "setval('s', 1)",
            "lo_import('/etc/passwd')",
            "lo_export(1, '/tmp/x')",
            "lo_put(1, 0, 'x')",
            "dblink('host=x', 'select 1')",
            "dblink_exec('host=x', 'delete from t')",
            "query_to_xml('select 1', true, true, '')",
            "table_to_xml('t', true, true, '')",
            "crosstab('select 1')",
        ]
        for call in calls:
            self.assertRejected(f"SELECT {call}", reason="禁止的函数")
            self.assertRejected(f"SELECT * FROM t WHERE x = {call}", reason="禁止的函数")
        for call in ("pg_ls_dir('/')", "fsdir('/')", "dblink('x', 'select 1') AS d(a int)"):
            self.assertRejected(f"SELECT * FROM {call}", reason="禁止的函数")

    def test_function_inside_a_subquery_or_cte_is_found(self):
        self.assertRejected("SELECT * FROM (SELECT pg_sleep(1)) s", reason="pg_sleep")
        self.assertRejected("WITH a AS (SELECT load_extension('x')) SELECT * FROM a", reason="load_extension")

    def test_harmless_functions_and_lookalikes_stay_allowed(self):
        for sql in [
            "SELECT replace(a, 'x', 'y'), lower(a), upper(a), length(a), coalesce(a, 0) FROM t",
            "SELECT now(), current_date, version()",
            "SELECT count(*), sum(a), avg(a), min(a), max(a) FROM t",
            "SELECT zeroblob(10), randomblob(10), hex(randomblob(4))",
            "SELECT * FROM generate_series(1, 3)",
            # 列名、别名撞上函数名但不是调用
            "SELECT pg_sleep FROM t",
            'SELECT 1 AS "load_extension" FROM t',
            "SELECT * FROM t WHERE note = 'call pg_sleep(1)'",
        ]:
            self.assertAllowed(sql)

    def test_unicode_escape_spellings_are_rejected(self):
        # `U&"pg\\005fsleep"` 就是 pg_sleep；校验不去解码，直接不收
        for sql in [
            'SELECT U&"pg\\005fsleep"(1)',
            "SELECT u&'x' FROM t",
            'SELECT * FROM U&"\\0074"',
        ]:
            self.assertRejected(sql, reason="Unicode")

    # ---- 拒绝：解析不了 ----

    def test_unparseable_sql_fails_closed_with_a_clear_reason(self):
        for sql in [
            "SELECT ((( FROM",
            "SELECT 1 FROM",
            "SELEC 1",
            "SELECT 1 /* 没有结尾的注释",
            "SELECT 'unterminated",
            "SELECT \"unterminated FROM t",
            "SELECT 1 +",
        ]:
            self.assertRejected(sql, reason="无法解析")

    def test_empty_and_comment_only_sql_is_rejected(self):
        for sql in ["", "   ", ";", ";;", "-- nothing", "/* nothing */", " ; -- x"]:
            self.assertRejected(sql, reason="为空")

    # ---- 方言 ----

    def test_dialect_decides_what_parses(self):
        sql = "SELECT * FROM t LIMIT %s"  # psycopg 占位符：只有 PostgreSQL 认
        self.assertTrue(check_readonly_sql(sql, dialect="postgres").allowed)
        self.assertFalse(check_readonly_sql(sql, dialect="sqlite").allowed)
        self.assertIn("无法解析", check_readonly_sql(sql, dialect="sqlite").reason)

    def test_without_a_dialect_both_dialects_must_accept(self):
        # 不知道目标库就不能挑解读：只有一种方言认得的写法，不传方言时拒绝
        self.assertFalse(check_readonly_sql("SELECT * FROM t LIMIT %s").allowed)
        reason = check_readonly_sql("SELECT * FROM t LIMIT %s").reason
        self.assertIn("无法解析", reason)
        self.assertIn("sqlite", reason)
        self.assertTrue(check_readonly_sql("SELECT * FROM t LIMIT ?").allowed)

    def test_unknown_dialect_must_not_loosen_the_check(self):
        # 认不出的方言名按「两种都判」处理，不会因此放过任何一条
        for sql in ["SELECT 1; DROP TABLE t", "WITH x AS (DELETE FROM t RETURNING *) SELECT 1", "SELECT pg_sleep(1)"]:
            self.assertFalse(check_readonly_sql(sql, dialect="oracle").allowed, sql)

    def test_result_shape_is_unchanged(self):
        allowed = check_readonly_sql("SELECT a FROM t WHERE b = 'x'; -- tail")
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.reason, "")
        self.assertEqual(allowed.normalized_sql, "SELECT a FROM t WHERE b = ''")
        denied = check_readonly_sql("DROP TABLE t")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.normalized_sql, "")


# ---- 安全审计给出的用例 ----
# 用例直接内联在这里，不依赖外部文件。
# 元素是 (编号, 方言, SQL)；方言 any 表示 sqlite / postgres / 不传方言三种都要判。
# 审计里标 pg-unverified 的用例没有在真实 PostgreSQL 上跑过，这里只验证校验层的判定。

AUDIT_MUST_REJECT = [
    ('G01', 'any', "WITH a AS (SELECT '--') DELETE FROM t"),
    ('G02', 'any', "WITH a AS (SELECT '--') UPDATE t SET v = 0"),
    ('G03', 'any', "WITH a AS (SELECT '/*') DELETE FROM t WHERE '*/' = '*/'"),
    ('G04', 'sqlite', 'WITH a AS (SELECT 1 AS "x\'") DELETE FROM t WHERE \'\'=\'\''),
    ('G05', 'sqlite', "WITH a AS (SELECT 1 AS [x']) DELETE FROM t WHERE ''=''"),
    ('G06', 'sqlite', "WITH a AS (SELECT 1 AS `x'`) DELETE FROM t WHERE ''=''"),
    ('G07', 'postgres', "WITH a AS (SELECT '--'), x AS (DELETE FROM t RETURNING 1) SELECT 1 FROM x"),
    ('G08', 'postgres', "WITH a AS (SELECT $$'$$), x AS (DELETE FROM t RETURNING 1) SELECT '' FROM x"),
    ('G09', 'postgres', "WITH a AS (SELECT E'\\''), x AS (DELETE FROM t RETURNING 1) SELECT '' FROM x"),
    ('G10', 'any', 'WITH a AS (SELECT 1) DELETE FROM payroll'),
    ('G11', 'any', "SELECT '/*' , 1 ; drop table t -- */"),
    ('G12', 'postgres', 'SELECT pg_sleep(1000)'),
    ('G13', 'postgres', "SELECT pg_read_binary_file('/etc/passwd')"),
    ('G14', 'postgres', "SELECT pg_stat_file('/etc/passwd')"),
    ('G15', 'postgres', 'SELECT * FROM pg_ls_waldir()'),
    ('G16', 'postgres', "SELECT dblink_exec('dbname=app','DELETE FROM t')"),
    ('G17', 'postgres', "SELECT query_to_xml('select * from payroll', true, false, '')"),
    ('G18', 'postgres', "SELECT set_config('statement_timeout','0',true)"),
    ('G19', 'postgres', 'SELECT pg_advisory_lock(42)'),
    ('G22', 'postgres', "SELECT j.* FROM (SELECT set_config('app.user_id','bob',true)) s, job j"),
    ('G23', 'postgres', "SELECT set_config('transaction_read_only','off',true)"),
    ('G24', 'sqlite', "VACUUM INTO '/tmp/exfil.db'"),
    ('G25', 'sqlite', "ATTACH DATABASE '/path/other.db' AS o"),
]

AUDIT_MUST_STILL_ALLOW = [
    ('L01', 'any', "SELECT replace(name, 'a', 'b') FROM t"),
    ('L02', 'any', 'SELECT "set" FROM t'),
    ('L03', 'any', 'SELECT "a;b" FROM t'),
    ('L04', 'any', 'SELECT name AS copy FROM t'),
    ('L05', 'any', "SELECT * FROM t WHERE comment = 'x' AND call = 1"),
    ('L06', 'any', "SELECT 'it''s; fine'"),
]

# 资源炸弹：它们归为执行层上限问题（SQLite 的 progress handler 打断不了
# 单次函数调用，结果集只有行数上限没有字节上限）。只读校验**放行**，由执行层负责——
# 执行层的字节预算、单值长度上限在 tests/test_execution_budget.py 里逐条断言。
# 不往禁用函数名单里加 randomblob / zeroblob：它们是无副作用的纯函数，名单拦不住
# `printf('%.*c', 1e9, 'x')`、`replace(hex(zeroblob(..)), ..)`、无界递归 CTE、
# 笛卡尔积这些同类写法，只会误伤正常用法，对根因（没有字节上限）没有帮助。
AUDIT_RESOURCE_BOMBS = [
    ('G20', 'sqlite', 'SELECT randomblob(1000000000)'),
    ('G21', 'any', 'WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r) SELECT count(*) FROM r'),
    ('G26', 'sqlite', 'SELECT x, zeroblob(5000000) FROM t'),
    ('G27', 'sqlite', 'SELECT hex(randomblob(150000000))'),
]


class SqlGuardAuditCasesTest(unittest.TestCase):
    """安全审计给出的绕过清单与误杀清单，逐条断言（方言见每条的第二项）。"""

    @staticmethod
    def _dialects(declared):
        # `any` 三种设置都测；只在一种方言下成立的用例，除了那种方言也测不传方言
        # （不传时两种方言都要放行，只会更严）。
        if declared == "any":
            return ("", "sqlite", "postgres")
        return (declared, "")

    def test_every_audit_bypass_case_is_rejected(self):
        self.assertEqual(len(AUDIT_MUST_REJECT), 23)
        for case_id, declared, sql in AUDIT_MUST_REJECT:
            for dialect in self._dialects(declared):
                result = check_readonly_sql(sql, dialect=dialect)
                label = f"{case_id} [{dialect or 'any'}] {sql!r}"
                self.assertFalse(result.allowed, f"{label} 不该放行")
                self.assertTrue(result.reason, f"{label} 拒绝时必须给原因")

    def test_every_audit_false_positive_is_now_allowed(self):
        self.assertEqual(len(AUDIT_MUST_STILL_ALLOW), 6)
        for case_id, _, sql in AUDIT_MUST_STILL_ALLOW:
            for dialect in ("", "sqlite", "postgres"):
                result = check_readonly_sql(sql, dialect=dialect)
                self.assertTrue(
                    result.allowed, f"{case_id} [{dialect or 'any'}] {sql!r}: {result.reason}"
                )

    def test_resource_bombs_are_the_execution_layers_job_not_the_guards(self):
        # 见 AUDIT_RESOURCE_BOMBS 上的说明。这条测试存在的意义是把「放行」写成明确的
        # 决定：以后有人想靠加函数黑名单来「修」它，会先在这里撞上理由。
        self.assertEqual(len(AUDIT_RESOURCE_BOMBS), 4)
        for case_id, declared, sql in AUDIT_RESOURCE_BOMBS:
            for dialect in self._dialects(declared):
                self.assertTrue(
                    check_readonly_sql(sql, dialect=dialect).allowed,
                    f"{case_id} [{dialect or 'any'}] {sql!r}",
                )

    def test_unquoted_keyword_lookalikes_are_identifiers_not_statements(self):
        # 语法树把它们认成了列名 / 别名 / 表名：不是 COPY / CALL / UPDATE 语句
        for sql in [
            "SELECT name AS copy FROM t",
            "SELECT t.copy, t.call FROM t",
            "SELECT * FROM t WHERE comment = 'x' AND call = 1",
            "SELECT a AS update, b AS delete FROM t",
            "SELECT a FROM copy",
            "WITH x AS (SELECT 1 AS copy) SELECT copy FROM x",
            "SELECT * FROM t AS call (a, b)",
        ]:
            for dialect in ("", "sqlite", "postgres"):
                result = check_readonly_sql(sql, dialect=dialect)
                self.assertTrue(result.allowed, f"[{dialect or 'any'}] {sql!r}: {result.reason}")

    def test_the_statements_themselves_are_still_rejected(self):
        for sql in [
            "COPY t TO '/tmp/x'",
            "COPY t FROM '/tmp/x'",
            "CALL p()",
            "SELECT copy FROM t; COPY t TO '/tmp/x'",
            "SELECT call FROM t; CALL p()",
            # 在函数调用的位置不是标识符
            "SELECT call(1) FROM t",
            # 在两种数据库里都是保留字：不给「被认领」的豁免
            "SELECT 1 AS into",
            "SELECT 1 AS create",
        ]:
            for dialect in ("", "sqlite", "postgres"):
                self.assertFalse(
                    check_readonly_sql(sql, dialect=dialect).allowed, f"[{dialect or 'any'}] {sql!r}"
                )


class SQLiteExecutorSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "safety.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(1, 11)])
        conn.commit()
        conn.close()
        self.executor = SQLiteMCPExecutor(
            database="test_db", db_path=self.db_path, max_rows=3
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def execute(self, sql):
        # 执行器入口现在有强制授权关卡：不绑作用域一律拒绝（这是有意的默认值，
        # 见 ai_sql_agent.auth.enforcement）。这组用例验的是只读约束与行数上限，
        # 所以显式绑一个全放开的策略，把权限这一维度排除掉。
        with authorization_scope(ALLOW_ALL):
            return self.executor.execute(
                MCPExecutionRequest(database="test_db", sql=sql)
            )

    def test_select_succeeds(self):
        result = self.execute("SELECT id FROM t ORDER BY id")
        self.assertTrue(result.success)
        self.assertEqual(result.row_count, 3)
        self.assertTrue(result.truncated)

    def test_row_cap_not_marked_truncated_when_exact(self):
        result = self.execute("SELECT id FROM t ORDER BY id LIMIT 3")
        self.assertTrue(result.success)
        self.assertFalse(result.truncated)

    def test_write_is_rejected_by_guard(self):
        result = self.execute("UPDATE t SET name = 'x'")
        self.assertFalse(result.success)

    def test_connection_layer_is_readonly(self):
        # 第二道防线：即使绕过应用层校验，连接本身也写不进去。
        # 执行器的连接上装了默认拒绝的授权器，写语句在准备阶段就被拒
        # （DatabaseError: not authorized），比 mode=ro 的「attempt to write a readonly
        # database」更早——DatabaseError 是 OperationalError 的父类，断言取宽的那个
        conn = self.executor._connect()
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE t SET name = 'x'")
        finally:
            conn.close()

    def test_mode_ro_alone_still_refuses_writes(self):
        # 授权器之下的那一层各自成立：不经过执行器，只用只读 URI 打开
        from ai_sql_agent.execution.sql_guard import readonly_sqlite_uri

        conn = sqlite3.connect(readonly_sqlite_uri(str(self.db_path)), uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE t SET name = 'x'")
        finally:
            conn.close()

    def test_missing_database_file_reports_clearly(self):
        executor = SQLiteMCPExecutor(database="test_db", db_path="/nonexistent/x.db")
        with authorization_scope(ALLOW_ALL):
            result = executor.execute(
                MCPExecutionRequest(database="test_db", sql="SELECT 1")
            )
        self.assertFalse(result.success)
        self.assertIn("不存在", result.error)

    def test_wrong_database_is_not_executed(self):
        with authorization_scope(ALLOW_ALL):
            result = self.executor.execute(
                MCPExecutionRequest(database="other_db", sql="SELECT 1")
            )
        self.assertFalse(result.success)
        self.assertIn("路由错误", result.error)


class DialectTest(unittest.TestCase):
    def test_lookup_and_aliases(self):
        self.assertIs(get_dialect("postgres"), POSTGRES)
        self.assertIs(get_dialect("postgresql"), POSTGRES)
        self.assertIs(get_dialect("PG"), POSTGRES)
        self.assertIs(get_dialect("sqlite3"), SQLITE)

    def test_unknown_dialect_falls_back_to_sqlite(self):
        self.assertIs(get_dialect("oracle"), SQLITE)
        self.assertIs(get_dialect(""), SQLITE)

    def test_prompt_rules_mention_dialect_specifics(self):
        self.assertIn("PostgreSQL", POSTGRES.to_prompt_rules())
        self.assertIn("INTERVAL", POSTGRES.to_prompt_rules())
        self.assertIn("JULIANDAY", SQLITE.to_prompt_rules())


class DialectInPromptTest(unittest.TestCase):
    def test_generation_prompt_carries_target_dialect(self):
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        request = SqlGenerationRequest(
            cot_step=CotStep(
                database="pm_db",
                processing_objects="work_order.schedule_date",
                operation_instruction="筛选排产日期在2024-04-01之后的工单",
                output_target="work_order.order_no",
            ),
            local_schema=LocalSchema(),
            sql_dialect="postgres",
        )
        prompt = SqlPromptBuilder().build(request)

        self.assertIn("PostgreSQL", prompt)
        self.assertIn("方言约束", prompt)
        self.assertNotIn("JULIANDAY", prompt)

    def test_generation_prompt_asks_for_readable_column_names(self):
        """
        结果的列名直接成了页面上的表头。规划是中文写的，模型顺手就给计算列起中文别名，
        用英文提问的人看到的是一排读不懂的表头——所以别名的写法得明说。
        """
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        prompt = SqlPromptBuilder().build(
            SqlGenerationRequest(
                cot_step=CotStep(
                    database="erp_db",
                    processing_objects="payment.paid_amt",
                    operation_instruction="按采购单汇总已付金额",
                    output_target="已付金额合计",
                ),
                local_schema=LocalSchema(),
            )
        )

        self.assertIn("英文小写加下划线", prompt)
        self.assertIn("不要用中文别名", prompt)
        self.assertIn("直接输出的字段保持原名", prompt)


if __name__ == "__main__":
    unittest.main()


class SchemaFingerprintTest(unittest.TestCase):
    """Schema 指纹：只反映结构变化，不受数据变化影响。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "fp.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _fingerprint(self):
        from ai_sql_agent.retrieval.schema_fingerprint import sqlite_schema_fingerprint

        return sqlite_schema_fingerprint(str(self.db_path))

    def _execute(self, sql):
        conn = sqlite3.connect(self.db_path)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def test_inserting_rows_does_not_change_fingerprint(self):
        before = self._fingerprint()
        self._execute("INSERT INTO t VALUES (1, 'x')")
        self.assertEqual(before, self._fingerprint())

    def test_adding_a_column_changes_fingerprint(self):
        before = self._fingerprint()
        self._execute("ALTER TABLE t ADD COLUMN age INTEGER")
        self.assertNotEqual(before, self._fingerprint())

    def test_adding_a_table_changes_fingerprint(self):
        before = self._fingerprint()
        self._execute("CREATE TABLE t2 (id INTEGER)")
        self.assertNotEqual(before, self._fingerprint())

    def test_unreachable_database_yields_empty_fingerprint(self):
        from ai_sql_agent.retrieval.schema_fingerprint import database_fingerprint

        # 取不到指纹时返回空串，调用方据此跳过重建而不是误判为“变了”
        self.assertEqual(
            database_fingerprint(dialect="sqlite", db_path="/nonexistent/x.db"), ""
        )


class SchemaHotReloadTest(unittest.TestCase):
    def test_pipeline_detects_and_reloads_schema_changes(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()
        self.assertEqual(pipeline.detect_schema_changes(), [])

        conn = sqlite3.connect("runtime_data/erp_demo.db")
        conn.execute("ALTER TABLE sales_order ADD COLUMN hot_reload_probe TEXT")
        conn.commit()
        conn.close()

        self.assertEqual(pipeline.detect_schema_changes(), ["erp_db"])

        changed = pipeline.reload_schema(reason="unittest")
        self.assertEqual(changed, ["erp_db"])
        self.assertEqual(pipeline.detect_schema_changes(), [])

    def test_watcher_is_disabled_by_default(self):
        from ai_sql_agent.api.schema_watcher import SchemaWatcher

        watcher = SchemaWatcher(pipeline=None, interval_seconds=0)
        self.assertFalse(watcher.enabled)
        watcher.start()  # 不应启动线程，也不应报错
        watcher.stop()


class CrossDatabaseReferenceGuardTest(unittest.TestCase):
    """生成的 SQL 只能引用当前步骤目标库里的表。

    跨库问题里模型容易把两个库压进一条 SQL（在 erp 的步骤里写
    `... IN (SELECT project_code FROM project)`），执行时报 no such table。
    实测这类错误在跨库类目里占比很高，静态就能判出来。
    """

    def setUp(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        self.pipeline = Text2SQLPipeline()

    def test_tables_of_returns_only_target_database_tables(self):
        erp = self.pipeline._tables_of("erp_db")
        self.assertIn("purchase_order", erp)
        self.assertNotIn("project", erp)      # project 属于 pm_db
        self.assertNotIn("work_order", erp)   # work_order 属于 mes_db

    def test_unknown_database_returns_empty_and_skips_check(self):
        self.assertEqual(self.pipeline._tables_of("nope_db"), set())

    def test_foreign_table_reference_triggers_regeneration(self):
        """引用外库表时应带提示重生成；改好了就用新 SQL。"""
        from ai_sql_agent.generation.objects import CotStep

        good_sql = "SELECT SUM(po_amt) FROM purchase_order WHERE job_no IN ('J24001')"

        class _Generator:
            def __init__(self):
                self.prompts = []

            def generate(
                self,
                cot_step,
                sql_dialect="",
                previous_results="",
                certified_examples="",
            ):
                self.prompts.append(cot_step.operation_instruction)
                return type("R", (), {"sql": good_sql})()

        generator = _Generator()
        bad = type("R", (), {
            "sql": "SELECT SUM(po_amt) FROM purchase_order "
                   "WHERE job_no IN (SELECT project_code FROM project)"
        })()

        fixed = self.pipeline._enforce_step_database(
            sql_result=bad,
            cot_step=CotStep(
                database="erp_db",
                processing_objects="purchase_order.po_amt",
                operation_instruction="汇总采购金额",
                output_target="purchase_order.po_amt",
            ),
            generator=generator,
            dialect="sqlite",
            previous_results='{"rows": [{"project_code": "J24001"}]}',
            step_index=2,
        )

        self.assertEqual(fixed.sql, good_sql)
        self.assertIn("project", generator.prompts[0])
        self.assertIn("不要写跨库子查询", generator.prompts[0])

    def test_clean_sql_is_left_untouched(self):
        from ai_sql_agent.generation.objects import CotStep

        class _Generator:
            def generate(self, *a, **k):
                raise AssertionError("SQL 合法时不应触发重生成")

        original = type("R", (), {"sql": "SELECT po_amt FROM purchase_order"})()
        result = self.pipeline._enforce_step_database(
            sql_result=original,
            cot_step=CotStep(
                database="erp_db",
                processing_objects="purchase_order.po_amt",
                operation_instruction="查询",
                output_target="purchase_order.po_amt",
            ),
            generator=_Generator(),
            dialect="sqlite",
            previous_results="",
            step_index=1,
        )
        self.assertIs(result, original)


class DockerImageHygieneTest(unittest.TestCase):
    """
    凭证不能进镜像。

    实测踩到过：`.dockerignore` 排除了 `deploy/api_keys.json`，却漏了 `.env`，
    于是带真实 API Key 的 `.env` 被 `COPY . /app` 打进了镜像——
    起容器后不传任何 key，`/readyz` 照样显示 `model_credentials: ok`。
    镜像一旦推到 registry，密钥就跟着分发到每台拉取的机器上。

    `docker build` 成功发现不了这个，只有真的起一个容器进去看才会发现。
    """

    IGNORE = Path(__file__).resolve().parents[1] / ".dockerignore"

    def setUp(self):
        self.patterns = [
            line.strip()
            for line in self.IGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_env_file_is_excluded_from_the_image(self):
        self.assertIn(".env", self.patterns, ".env 会带着真实 API Key 进镜像")

    def test_api_key_file_is_excluded(self):
        self.assertIn("deploy/api_keys.json", self.patterns)

    def test_env_example_is_still_shipped(self):
        """模板要留着，否则镜像里没有任何配置参考。"""
        self.assertIn("!.env.example", self.patterns)
