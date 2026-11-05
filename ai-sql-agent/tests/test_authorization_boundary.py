"""
授权边界的负面用例——**每一条都是复现过的越权**。

对照 `Agentic-AI-Audit-2026-09-04/evidence/sql_probes.json`：那份结果里
`comma_join_table_bypass` / `denied_column_bypass` / `star_column_bypass` 都是
`denied: []` + `success: true`，`unknown_role_fail_open` 是 `true`，
`repair_bypasses_authorization` 拿到了 1 行数据。这个文件把它们逐条钉成红线。

放进 CI 而不是留成一次性脚本，是因为这类缺陷**退化的时候没有症状**：
越权成功和正常查询长得一模一样，不会有人发现。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sql_agent.auth import ApiKeyStore, DataPolicy, authorization_scope, check_sql_access
from ai_sql_agent.core import reload_settings
from ai_sql_agent.execution import MCPExecutionRequest, MCPRouter, SQLiteMCPExecutor


def _pick_tables(pipeline):
    """
    从 pipeline 自己解析出来的元数据里挑一个库和它的两张表。

    用例要的是「允许 A、禁止 B，看修复能不能摸到 B」，具体叫什么名字无关紧要；
    写死库名会让用例在另一套语义层配置下静默空过。
    """
    for database in pipeline.databases:
        tables = sorted(pipeline._tables_of(database.name))
        if len(tables) >= 2:
            return database.name, tables[0], tables[1]
    raise unittest.SkipTest("元数据里没有一个库有两张以上的表")


def _fixture_db(directory: str) -> Path:
    """两张表：一张有权限、一张没有；有权限那张里还有一列是禁列。"""
    path = Path(directory) / "test.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE public_data(id INT, private_value TEXT);"
            "INSERT INTO public_data VALUES(1,'PRIVATE_COLUMN');"
            "CREATE TABLE secret_data(value TEXT);"
            "INSERT INTO secret_data VALUES('PRIVATE_TABLE');"
        )
    return path


POLICY = DataPolicy(
    allow_tables=("test.public_data",),
    deny_columns=("test.public_data.private_value",),
)


class ReproducedBypassTest(unittest.TestCase):
    """复现过的那三条 SQL：现在必须一条都执行不了。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executor = SQLiteMCPExecutor(
            database="test", db_path=_fixture_db(self.tmp.name)
        )

    def _run(self, sql: str):
        with authorization_scope(POLICY, caller="audit", user_id="audit"):
            return self.executor.execute(
                MCPExecutionRequest(database="test", sql=sql)
            )

    def test_comma_join_second_table_is_denied(self):
        # 旧正则只捕获 FROM/JOIN 后面第一个标识符，逗号连接的第二张表整个看不见
        result = self._run("SELECT s.value FROM public_data p, secret_data s")
        self.assertFalse(result.success)
        self.assertIn("secret_data", result.error)
        self.assertEqual(result.rows, [])

    def test_denied_column_is_denied(self):
        result = self._run("SELECT private_value FROM public_data")
        self.assertFalse(result.success)
        self.assertIn("private_value", result.error)

    def test_star_over_a_table_with_denied_columns_is_denied(self):
        # 执行器这层没有 schema，展不开 `*`；展不开就不能证明它没读到禁列
        result = self._run("SELECT * FROM public_data")
        self.assertFalse(result.success)
        self.assertIn("public_data", result.error)

    def test_permitted_query_still_runs(self):
        # 反向断言：一个「永远拒绝」的实现也能让上面三条变绿，必须同时验这条
        result = self._run("SELECT id FROM public_data")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [{"id": 1}])

    def test_count_star_is_not_treated_as_reading_every_column(self):
        # COUNT(*) 不读任何列。把它算成读全表会让带列限制的角色连计数都做不了
        result = self._run("SELECT COUNT(*) AS n FROM public_data")
        self.assertTrue(result.success, result.error)


class UnparseableSqlTest(unittest.TestCase):
    """「提取不到表」不等于「没有越权」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executor = SQLiteMCPExecutor(
            database="test", db_path=_fixture_db(self.tmp.name)
        )

    def test_unparseable_sql_is_rejected(self):
        with authorization_scope(DataPolicy.full_access()):
            result = self.executor.execute(
                MCPExecutionRequest(database="test", sql="SELECT ((( FROM")
            )
        self.assertFalse(result.success)
        self.assertIn("无法解析", result.error)


class MissingScopeTest(unittest.TestCase):
    """没有绑定授权上下文 = 拒绝。这是「必然经过关卡」的支点。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executor = SQLiteMCPExecutor(
            database="test", db_path=_fixture_db(self.tmp.name)
        )

    def test_execution_without_scope_is_denied(self):
        result = self.executor.execute(
            MCPExecutionRequest(database="test", sql="SELECT id FROM public_data")
        )
        self.assertFalse(result.success)
        self.assertIn("授权上下文", result.error)

    def test_router_without_scope_is_denied(self):
        router = MCPRouter()
        router.register_executor("test", self.executor)
        result = router.execute({"database": "test", "sql": "SELECT id FROM public_data"})
        self.assertFalse(result.success)
        self.assertIn("授权上下文", result.error)


class SchemaQualifiedTableTest(unittest.TestCase):
    """
    不同 schema 的同名表必须**能被策略区分**。

    旧实现的 `_normalize()` 把 `secret.orders` 砍成 `orders`，schema 信息在进入
    策略之前就没了——两张表共用一条规则，而且没有任何写法能把它们分开。
    现在 schema 保留到判定这一步，所以「分得开」成立。

    真实 PostgreSQL 上的对应用例见 tests/test_authorization_postgres.py。
    """

    def test_a_qualified_rule_only_covers_its_own_schema(self):
        policy = DataPolicy(allow_tables=("db.public.orders",))
        self.assertTrue(
            check_sql_access(
                policy, database="db", sql="SELECT id FROM public.orders",
                dialect="postgres",
            ).allowed
        )
        denied = check_sql_access(
            policy, database="db", sql="SELECT id FROM secret.orders",
            dialect="postgres",
        )
        self.assertFalse(denied.allowed)
        self.assertIn("secret.orders", denied.denied_tables)

    def test_deny_rule_reaches_a_qualified_name(self):
        """`deny_tables` 是把同名表分开的另一种写法，两种都要真的生效。"""
        policy = DataPolicy(
            allow_tables=("db.orders",), deny_tables=("db.secret.*",)
        )
        self.assertTrue(
            check_sql_access(
                policy, database="db", sql="SELECT id FROM orders",
                dialect="postgres",
            ).allowed
        )
        self.assertFalse(
            check_sql_access(
                policy, database="db", sql="SELECT id FROM secret.orders",
                dialect="postgres",
            ).allowed
        )

    def test_a_bare_rule_keeps_its_historical_meaning(self):
        """
        不带 schema 的规则说的是「叫 orders 的那张表」，**包括别的 schema 里的
        同名表**。这是它一直以来的含义，这次不在无声无息中收紧它——要区分
        schema，写带 schema 的规则或加一条 deny（上面两条用例）。

        写下来是因为它是一个有意的边界，不是遗漏。
        """
        policy = DataPolicy(allow_tables=("db.orders",))
        self.assertTrue(
            check_sql_access(
                policy, database="db", sql="SELECT id FROM secret.orders",
                dialect="postgres",
            ).allowed
        )

    def test_wildcard_rule_still_covers_qualified_names(self):
        policy = DataPolicy(allow_tables=("db.*",))
        decision = check_sql_access(
            policy, database="db", sql="SELECT id FROM secret.orders",
            dialect="postgres",
        )
        self.assertTrue(decision.allowed, decision.reason)


class NestedQueryTest(unittest.TestCase):
    """CTE / 子查询 / UNION / LATERAL 里的表一样要判。"""

    def setUp(self):
        self.policy = DataPolicy(allow_tables=("db.public_data",))

    def _denied(self, sql: str):
        return check_sql_access(self.policy, database="db", sql=sql).denied_tables

    def test_cte_body(self):
        self.assertIn(
            "secret_data",
            self._denied("WITH t AS (SELECT * FROM secret_data) SELECT * FROM t"),
        )

    def test_subquery(self):
        self.assertIn(
            "secret_data",
            self._denied("SELECT * FROM (SELECT value FROM secret_data) x"),
        )

    def test_union(self):
        self.assertIn(
            "secret_data",
            self._denied(
                "SELECT id FROM public_data UNION SELECT value FROM secret_data"
            ),
        )

    def test_in_subquery(self):
        self.assertIn(
            "secret_data",
            self._denied(
                "SELECT id FROM public_data WHERE id IN (SELECT value FROM secret_data)"
            ),
        )

    def test_cte_name_is_not_mistaken_for_a_table(self):
        policy = DataPolicy(allow_tables=("db.public_data",))
        decision = check_sql_access(
            policy,
            database="db",
            sql="WITH t AS (SELECT id FROM public_data) SELECT id FROM t",
        )
        self.assertTrue(decision.allowed, decision.reason)


class UnknownRoleTest(unittest.TestCase):
    """用一个不存在的角色曾经能拿到全部权限。"""

    def test_unknown_role_gets_nothing(self):
        store = ApiKeyStore.from_dict({
            "keys": [{"key": "audit-only", "caller": "service", "roles": ["typo-role"]}],
            "policies": {"valid-role": {"allow_tables": ["test.public_data"]}},
        })
        policy = store.resolve("audit-only", "").policy
        self.assertFalse(policy.allows_table("test", "secret_data"))
        self.assertFalse(policy.allows_table("test", "public_data"))

    def test_known_role_still_gets_its_policy(self):
        store = ApiKeyStore.from_dict({
            "keys": [{"key": "k", "caller": "service", "roles": ["valid-role"]}],
            "policies": {"valid-role": {"allow_tables": ["test.public_data"]}},
        })
        policy = store.resolve("k", "").policy
        self.assertTrue(policy.allows_table("test", "public_data"))
        self.assertFalse(policy.allows_table("test", "secret_data"))

    def test_legacy_full_access_needs_an_explicit_switch(self):
        import os

        saved = os.environ.get("AI_SQL_UNKNOWN_ROLE_POLICY")
        os.environ["AI_SQL_UNKNOWN_ROLE_POLICY"] = "full_access"
        reload_settings()
        try:
            store = ApiKeyStore.from_dict({
                "keys": [{"key": "k", "caller": "service", "roles": ["typo"]}],
                "policies": {"valid": {"allow_tables": ["test.public_data"]}},
            })
            self.assertTrue(store.resolve("k", "").policy.allows_table("test", "x"))
        finally:
            if saved is None:
                os.environ.pop("AI_SQL_UNKNOWN_ROLE_POLICY", None)
            else:
                os.environ["AI_SQL_UNKNOWN_ROLE_POLICY"] = saved
            reload_settings()


class RepairPathTest(unittest.TestCase):
    """
    最要命的一条：直接检查会拒的 SQL，走完整修复路径却成功返回了 1 行。

    修复回调直接调执行器，没有重新鉴权。现在关卡在执行器入口，这条路绕不过去。
    """

    def test_repair_cannot_reach_a_denied_table(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline
        from ai_sql_agent.generation.objects import SqlGenerationResult
        from ai_sql_agent.generation.sql_generator import SqlGenerator

        pipeline = Text2SQLPipeline()
        # 库名不写死：内置演示元数据与 tools/sim_erp/semantic 里 supplier 落在不同的库，
        # 写死哪一个都会在另一套配置下空过（databases=[] 时修复回路根本不触发）
        target_db, allowed_table, denied_table = _pick_tables(pipeline)
        policy = DataPolicy(allow_tables=(f"{target_db}.{allowed_table}",))
        denied_sql = f"SELECT * FROM {denied_table} LIMIT 1"

        # 修复模型被替身成「返回一条禁用表的 SQL」，与复现脚本同一手法
        pipeline.sql_repair._regenerate = lambda *a, **kw: (
            denied_sql,
            "audit-injected-model-output",
        )

        def bad_initial(self, step, **kwargs):
            return SqlGenerationResult(
                database=step.database,
                prompt="",
                raw_output="",
                sql=f"SELECT nonexistent_audit_column FROM {allowed_table}",
            )

        with patch.object(SqlGenerator, "generate", bad_initial):
            result = pipeline.run("查询准时率低于0.9的供应商名称", access=policy)

        self.assertTrue(result.step_logs)
        # 先证明修复回路真的跑了——否则这条用例会因为「初始 SQL 就被拒」而
        # 空过，看起来是绿的，实际什么都没验
        self.assertTrue(
            any(step.repair_attempts for step in result.step_logs),
            "修复回路没有触发，这条用例没验到东西",
        )
        for step in result.step_logs:
            execution = step.execution_result
            self.assertFalse(
                execution["success"],
                f"修复路径拿到了数据：{step.final_sql}",
            )
            self.assertEqual(execution.get("row_count") or 0, 0)
            self.assertNotIn(denied_table, step.final_sql)

    def test_replay_path_is_authorized(self):
        """历史重放同样过关卡：上次有权限不代表这次还有。"""
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()
        target_db, allowed_table, denied_table = _pick_tables(pipeline)
        pipeline.sql_history_lookup = lambda user_id, query: [
            (target_db, f"SELECT * FROM {denied_table} LIMIT 1")
        ]
        with authorization_scope(DataPolicy.full_access()):
            logs = pipeline._replay_history(
                query="任意问题",
                user_id="u1",
                access=DataPolicy(allow_tables=(f"{target_db}.{allowed_table}",)),
            )
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()


class RowScopeTest(unittest.TestCase):
    """
    行范围。设计见 docs/authz-model.md。

    三态是这条设计的全部要点：**「没声明」和「声明了全放」必须分得开**，
    否则默认值会兜出一个静默的全放行——那就是 `full_access` 的老毛病
    换个维度复发。
    """

    def setUp(self):
        self.policy = DataPolicy.from_dict({
            "allow_tables": ["erp_db.*"],
            "row_filters": [
                {"table": "erp_db.job",
                 "predicate": "owner_id = current_setting('app.user_id', true)"},
                {"table": "erp_db.*", "predicate": "*"},
            ],
        })

    def test_declared_predicate_is_returned(self):
        self.assertIn("owner_id", self.policy.row_predicate("erp_db", "job"))

    def test_explicit_all_rows_is_an_empty_string_not_none(self):
        self.assertEqual(self.policy.row_predicate("erp_db", "invoice"), "")

    def test_undeclared_is_none_not_an_empty_string(self):
        self.assertIsNone(self.policy.row_predicate("wms_db", "batch"))

    def test_a_policy_without_row_filters_declares_nothing(self):
        self.assertIsNone(DataPolicy().row_predicate("erp_db", "job"))

    def test_first_match_wins(self):
        """规则按声明顺序取第一条匹配——通配那条放在最后才是「其余全放」。"""
        policy = DataPolicy.from_dict({
            "allow_tables": ["erp_db.*"],
            "row_filters": [
                {"table": "erp_db.*", "predicate": "*"},
                {"table": "erp_db.job", "predicate": "owner_id = 'x'"},
            ],
        })
        self.assertEqual(policy.row_predicate("erp_db", "job"), "")


class ImpersonationIntersectionTest(unittest.TestCase):
    """
    impersonation 取交集。

    以前只换 `user_id` 不换权限，所以 alice 和 bob 权限完全相同——
    一把服务 Key 替所有员工发请求，每个员工拿到的都是服务身份的全部权限。
    """

    CONFIG = {
        "keys": [{"key": "k-svc", "caller": "portal", "roles": ["analyst"],
                  "can_impersonate": True}],
        "policies": {
            "analyst": {"allow_tables": ["erp_db.*", "wms_db.*"]},
            "estimating": {"allow_tables": ["erp_db.job"]},
            "workshop": {"allow_tables": ["wms_db.*"]},
        },
        "users": {"alice": {"roles": ["estimating"]}, "bob": {"roles": ["workshop"]}},
    }

    def setUp(self):
        self.store = ApiKeyStore.from_dict(self.CONFIG)

    def test_two_users_of_one_service_key_get_different_permissions(self):
        alice = self.store.resolve("k-svc", "alice").policy
        bob = self.store.resolve("k-svc", "bob").policy
        self.assertTrue(alice.allows_table("erp_db", "job"))
        self.assertFalse(alice.allows_table("wms_db", "batch"))
        self.assertFalse(bob.allows_table("erp_db", "job"))
        self.assertTrue(bob.allows_table("wms_db", "batch"))

    def test_intersection_not_replacement(self):
        """
        交集不是二选一：服务身份没有的，用户有也不给。
        两份策略都是上界，都要守。
        """
        store = ApiKeyStore.from_dict({
            "keys": [{"key": "k", "caller": "svc", "roles": ["narrow"],
                      "can_impersonate": True}],
            "policies": {
                "narrow": {"allow_tables": ["erp_db.job"]},
                "wide": {"allow_tables": ["erp_db.*"]},
            },
            "users": {"alice": {"roles": ["wide"]}},
        })
        policy = store.resolve("k", "alice").policy
        self.assertTrue(policy.allows_table("erp_db", "job"))
        self.assertFalse(
            policy.allows_table("erp_db", "invoice"),
            "服务身份没有的表，用户策略再宽也不该给",
        )

    def test_undeclared_user_gets_nothing(self):
        """「不知道他能看什么」不等于「他什么都能看」。"""
        policy = self.store.resolve("k-svc", "carol").policy
        self.assertFalse(policy.allows_table("erp_db", "job"))
        self.assertFalse(policy.allows_table("wms_db", "batch"))

    def test_without_a_users_section_behaviour_is_unchanged(self):
        """
        没配 `users` 的老部署照常跑（不破坏现网）——生产模式的启动检查
        会把它拦下来，见 tests/test_production_gate.py。
        """
        config = {k: v for k, v in self.CONFIG.items() if k != "users"}
        store = ApiKeyStore.from_dict(config)
        policy = store.resolve("k-svc", "alice").policy
        self.assertTrue(policy.allows_table("wms_db", "batch"))

    def test_non_impersonating_key_is_untouched(self):
        store = ApiKeyStore.from_dict({
            "keys": [{"key": "k-li", "caller": "li", "user_id": "li",
                      "roles": ["analyst"]}],
            "policies": {"analyst": {"allow_tables": ["erp_db.*"]}},
            "users": {"li": {"roles": ["estimating"]}},
            })
        self.assertTrue(store.resolve("k-li", "li").policy.allows_table("erp_db", "x"))

    def test_intersection_is_enforced_by_the_sql_gate(self):
        """交集要真的落到执行前的 SQL 判定上，不只是 allows_table 好看。"""
        policy = self.store.resolve("k-svc", "alice").policy
        self.assertTrue(
            check_sql_access(policy, database="erp_db", sql="SELECT a FROM job").allowed
        )
        self.assertFalse(
            check_sql_access(
                policy, database="wms_db", sql="SELECT a FROM batch"
            ).allowed
        )

    def test_intersection_keeps_column_restrictions_from_either_side(self):
        store = ApiKeyStore.from_dict({
            "keys": [{"key": "k", "caller": "svc", "roles": ["svc"],
                      "can_impersonate": True}],
            "policies": {
                "svc": {"allow_tables": ["erp_db.*"]},
                "u": {"allow_tables": ["erp_db.*"],
                      "deny_columns": ["erp_db.job.margin"]},
            },
            "users": {"alice": {"roles": ["u"]}},
        })
        policy = store.resolve("k", "alice").policy
        self.assertFalse(policy.allows_column("erp_db", "job", "margin"))
        self.assertTrue(policy.allows_column("erp_db", "job", "job_no"))
        # 有列级限制 ⇒ SELECT * 要被拒（执行器那层没有 schema，展不开通配符）
        self.assertTrue(policy.restricts_columns("erp_db", "job"))


class ConfigParsingTest(unittest.TestCase):
    """配置文件里的注释键不能把解析炸掉——这份文件本来就用 `$comment` 写说明。"""

    def test_comment_keys_are_skipped(self):
        store = ApiKeyStore.from_dict({
            "$comment": "顶层说明",
            "keys": [{"key": "k", "caller": "c", "roles": ["r"], "can_impersonate": True}],
            "policies": {"$comment": "策略说明", "r": {"allow_tables": ["erp_db.*"]}},
            "users": {"$comment": "用户说明", "alice": {"roles": ["r"]}},
        })
        self.assertEqual(sorted(store.policies), ["r"])
        self.assertEqual(sorted(store.users), ["alice"])

    def test_shipped_example_config_parses(self):
        """示例配置要能被自己解析——它是运维照抄的模板。"""
        import json
        from pathlib import Path

        payload = json.loads(
            (Path(__file__).resolve().parents[1] / "deploy/api_keys.example.json")
            .read_text(encoding="utf-8")
        )
        store = ApiKeyStore.from_dict(payload)
        self.assertTrue(store.policies)
        self.assertTrue(store.users)
        for role, policy in store.policies.items():
            self.assertIsNotNone(
                policy.row_predicate("erp_db", "job"),
                f"示例里的 {role} 应当显式声明行范围（哪怕是 predicate=*）",
            )
