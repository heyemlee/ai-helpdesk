import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.auth import ApiKeyStore, DataPolicy, extract_referenced_tables
from ai_sql_agent.core import reload_settings

KEY_CONFIG = {
    "keys": [
        {
            "key": "k-portal",
            "caller": "bi-portal",
            "roles": ["analyst"],
            "can_impersonate": True,
        },
        {"key": "k-li", "caller": "li", "user_id": "li", "roles": ["analyst"]},
        {"key": "k-intern", "caller": "intern", "user_id": "intern", "roles": ["intern"]},
    ],
    "policies": {
        "analyst": {"allow_tables": ["erp_db.*", "pm_db.*"]},
        "intern": {
            "allow_tables": ["erp_db.sales_order"],
            "deny_columns": ["*.*.supplier_name"],
        },
    },
}


class DataPolicyTest(unittest.TestCase):
    def test_glob_allow_and_deny(self):
        policy = DataPolicy(
            allow_tables=("erp_db.*",),
            deny_tables=("erp_db.secret_*",),
        )
        self.assertTrue(policy.allows_table("erp_db", "sales_order"))
        self.assertFalse(policy.allows_table("erp_db", "secret_payroll"))
        self.assertFalse(policy.allows_table("hr_db", "employee"))

    def test_deny_beats_allow(self):
        policy = DataPolicy(allow_tables=("*",), deny_tables=("hr_db.*",))
        self.assertFalse(policy.allows_table("hr_db", "employee"))
        self.assertTrue(policy.allows_table("erp_db", "t"))

    def test_column_denied_but_table_visible(self):
        policy = DataPolicy(
            allow_tables=("hr_db.*",), deny_columns=("*.employee.salary",)
        )
        self.assertTrue(policy.allows_table("hr_db", "employee"))
        self.assertTrue(policy.allows_column("hr_db", "employee", "name"))
        self.assertFalse(policy.allows_column("hr_db", "employee", "salary"))

    def test_merge_of_multiple_roles(self):
        merged = DataPolicy(allow_tables=("a.*",)).merge(
            DataPolicy(allow_tables=("b.*",), deny_columns=("b.t.c",))
        )
        self.assertTrue(merged.allows_table("a", "x"))
        self.assertTrue(merged.allows_table("b", "t"))
        self.assertFalse(merged.allows_column("b", "t", "c"))


class ApiKeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = ApiKeyStore.from_dict(KEY_CONFIG)

    def test_service_key_can_impersonate(self):
        principal = self.store.resolve("k-portal", "employee-42")
        self.assertEqual(principal.caller, "bi-portal")
        self.assertEqual(principal.user_id, "employee-42")

    def test_personal_key_cannot_impersonate(self):
        with self.assertRaises(PermissionError):
            self.store.resolve("k-li", "someone-else")

    def test_personal_key_defaults_to_bound_user(self):
        principal = self.store.resolve("k-li", "")
        self.assertEqual(principal.user_id, "li")

    def test_invalid_key_rejected(self):
        with self.assertRaises(PermissionError):
            self.store.resolve("nope", "li")

    def test_role_policy_is_attached(self):
        principal = self.store.resolve("k-intern", "")
        self.assertTrue(principal.policy.allows_table("erp_db", "sales_order"))
        self.assertFalse(principal.policy.allows_table("erp_db", "supplier"))

    def test_missing_caller_is_rejected(self):
        with self.assertRaises(ValueError):
            ApiKeyStore.from_dict({"keys": [{"key": "x"}]})

    def test_duplicate_key_is_rejected(self):
        # 静默覆盖会让某个调用方拿到别人的身份和权限
        with self.assertRaises(ValueError):
            ApiKeyStore.from_dict(
                {
                    "keys": [
                        {"key": "same", "caller": "a"},
                        {"key": "same", "caller": "b"},
                    ]
                }
            )

    def test_non_ascii_key_is_rejected_not_crashed(self):
        # Key 直接来自请求头：非 ASCII 不能把 401 变成 500
        with self.assertRaises(PermissionError):
            self.store.resolve("中文密钥", "li")

    def test_non_ascii_configured_key_still_matches(self):
        store = ApiKeyStore.from_dict(
            {"keys": [{"key": "密钥-abc", "caller": "cn", "user_id": "cn"}]}
        )
        self.assertEqual(store.resolve("密钥-abc", "").caller, "cn")


class ReferencedTableExtractionTest(unittest.TestCase):
    def test_from_and_join(self):
        self.assertEqual(
            extract_referenced_tables(
                "SELECT a.x FROM sales_order a JOIN supplier b ON a.id=b.id"
            ),
            {"sales_order", "supplier"},
        )

    def test_cte_alias_is_not_a_table(self):
        self.assertEqual(
            extract_referenced_tables(
                "WITH t AS (SELECT * FROM salary) SELECT * FROM t"
            ),
            {"salary"},
        )

    def test_schema_qualified_and_quoted(self):
        self.assertEqual(
            extract_referenced_tables('SELECT * FROM public."employee"'), {"employee"}
        )

    def test_table_name_in_string_literal_is_ignored(self):
        self.assertEqual(
            extract_referenced_tables("SELECT * FROM t WHERE x = 'from hacker'"),
            {"t"},
        )

    def test_subquery(self):
        self.assertEqual(
            extract_referenced_tables("SELECT * FROM (SELECT * FROM inner_t) x"),
            {"inner_t"},
        )


class RetrievalFilteringTest(unittest.TestCase):
    """第一道防线：无权表不进 SchemaGraph，模型看不到。"""

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        cls.pipeline = Text2SQLPipeline()

    def test_restricted_user_sees_only_permitted_database(self):
        result = self.pipeline.run(
            "查询实际成本超过预算的项目的采购总金额",
            access=DataPolicy(allow_tables=("erp_db.*",)),
        )
        self.assertEqual(result.selected_databases, ["erp_db"])
        self.assertNotIn("work_order", result.schema_context)

    def test_full_access_user_sees_both_databases(self):
        result = self.pipeline.run("查询实际成本超过预算的项目的采购总金额")
        # 库级检索会保留若干候选库，这里关心的是「有权限时两个相关库都在」，
        # 而不是候选集恰好只有两个——后者是检索配置的实现细节。
        self.assertIn("pm_db", result.selected_databases)
        self.assertIn("erp_db", result.selected_databases)

    def test_denied_column_is_removed_from_prompt(self):
        result = self.pipeline.run(
            "查询准时率低于0.9的供应商名称",
            access=DataPolicy(
                allow_tables=("erp_db.*",),
                deny_columns=("*.supplier.supplier_name",),
            ),
        )
        self.assertNotIn("supplier_name", result.schema_context)

    def test_denied_column_is_also_rejected_at_execution(self):
        """
        上一条只证明了「模型看不到这列」，没证明「执行器会拒绝这列」——
        模型凭对话上下文或先验知识照样写得出列名。复现过的就是
        这个缺口（`SELECT private_value FROM public_data` 当时能拿到数据）。

        完整的执行器侧负面用例在 tests/test_authorization_boundary.py，
        这里补的是「检索裁剪与执行拦截是两回事」这一条。
        """
        from ai_sql_agent.auth import check_sql_access

        policy = DataPolicy(
            allow_tables=("erp_db.*",),
            deny_columns=("*.supplier.supplier_name",),
        )
        decision = check_sql_access(
            policy,
            database="erp_db",
            sql="SELECT supplier_name FROM supplier",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("supplier.supplier_name", decision.denied_columns)


class ExecutionFilteringTest(unittest.TestCase):
    """第二道防线：模型凭记忆写出无权表时，SQL 打到库之前被拦住。"""

    def test_denied_table_blocks_execution_without_repair(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        class AllowRetrievalDenyExecution:
            """检索阶段全放行，执行阶段拒绝 supplier。"""

            def __init__(self):
                self.retrieval_done = False

            def allows_table(self, database, table):
                if not self.retrieval_done:
                    return True
                return table != "supplier"

            def allows_column(self, database, table, column):
                return True

        pipeline = Text2SQLPipeline()
        access = AllowRetrievalDenyExecution()

        original_retrieve = pipeline.schema_retrieval_service.retrieve

        def retrieve(*args, **kwargs):
            result = original_retrieve(*args, **kwargs)
            access.retrieval_done = True
            return result

        pipeline.schema_retrieval_service.retrieve = retrieve

        result = pipeline.run("查询准时率低于0.9的供应商名称", access=access)

        step = result.step_logs[0]
        self.assertFalse(step.execution_result["success"])
        self.assertIn("没有访问权限", step.execution_result["error"])
        self.assertIn("supplier", step.execution_result["error"])
        # 越权不该触发修复回路：改 SQL 也拿不到权限，只会白烧模型调用
        self.assertEqual(step.repair_attempts, [])

    def test_permitted_sql_passes_the_check(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()
        denied = pipeline._check_table_access(
            database="erp_db",
            sql="SELECT user_id FROM sales_order",
            access=DataPolicy(allow_tables=("erp_db.sales_order",)),
        )
        self.assertEqual(denied, [])


class ApiAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(json.dumps(KEY_CONFIG), encoding="utf-8")

        cls.saved = {
            "AI_SQL_AUTH_MODE": os.environ.get("AI_SQL_AUTH_MODE"),
            "AI_SQL_API_KEYS_FILE": os.environ.get("AI_SQL_API_KEYS_FILE"),
        }
        os.environ["AI_SQL_AUTH_MODE"] = "api_key"
        os.environ["AI_SQL_API_KEYS_FILE"] = str(key_file)
        reload_settings()

        from ai_sql_agent.api.app import app

        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        cls.tmpdir.cleanup()

    def _query(self, headers=None, **overrides):
        body = {
            "user_id": "u1",
            "session_id": "s_auth",
            "query": "查询准时率低于0.9的供应商名称",
        }
        body.update(overrides)
        return self.client.post("/query", json=body, headers=headers or {})

    def test_missing_key_is_401(self):
        self.assertEqual(self._query().status_code, 401)

    def test_invalid_key_is_401(self):
        self.assertEqual(self._query({"X-API-Key": "bad"}).status_code, 401)

    def test_non_ascii_key_header_is_401_not_500(self):
        # HTTP 头按 latin-1 传输：这类字符能真的到达服务端，
        # 而 hmac.compare_digest 遇到它们会抛 TypeError（500）
        # 头以字节形式发送，模拟真实客户端能送到服务端的 latin-1 内容
        response = self._query({"X-API-Key": "clé-señor".encode("latin-1")})
        self.assertEqual(response.status_code, 401)

    def test_bearer_header_is_accepted(self):
        response = self._query({"Authorization": "Bearer k-portal"})
        self.assertEqual(response.status_code, 200)

    def test_impersonation_without_permission_is_403(self):
        response = self._query({"X-API-Key": "k-li"})
        self.assertEqual(response.status_code, 403)

    def test_personal_key_with_own_user_id_works(self):
        response = self._query({"X-API-Key": "k-li"}, user_id="li")
        self.assertEqual(response.status_code, 200)

    def test_restricted_role_cannot_reach_denied_table(self):
        response = self._query({"X-API-Key": "k-intern"}, user_id="intern")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # intern 只能看 sales_order，柜体明细表不在其权限内
        for step in body["step_logs"]:
            self.assertNotIn("supplier", step["sql"])

    def test_health_does_not_require_key(self):
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()


class HistoryApiTest(unittest.TestCase):
    """历史里有问题原文和 SQL，等同数据访问面，必须按人隔离。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(
            json.dumps(
                {
                    "keys": [
                        {
                            "key": "k-admin",
                            "caller": "ops",
                            "user_id": "ops",
                            "is_admin": True,
                            "can_impersonate": True,
                        },
                        {"key": "k-li", "caller": "li", "user_id": "li"},
                        {"key": "k-wang", "caller": "wang", "user_id": "wang"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        cls.saved = {
            key: os.environ.get(key)
            for key in ("AI_SQL_AUTH_MODE", "AI_SQL_API_KEYS_FILE", "AI_SQL_AUDIT_DB_PATH")
        }
        os.environ["AI_SQL_AUTH_MODE"] = "api_key"
        os.environ["AI_SQL_API_KEYS_FILE"] = str(key_file)
        os.environ["AI_SQL_AUDIT_DB_PATH"] = str(Path(cls.tmpdir.name) / "audit.db")
        reload_settings()

        from ai_sql_agent.api.app import app

        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()
        cls.client.post(
            "/query",
            headers={"X-API-Key": "k-li"},
            json={
                "user_id": "li",
                "session_id": "hist",
                "query": "查询准时率低于0.9的供应商名称",
            },
        )

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        cls.tmpdir.cleanup()

    def test_returns_own_history(self):
        response = self.client.get(
            "/history?user_id=li", headers={"X-API-Key": "k-li"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user_id"], "li")
        self.assertTrue(body["items"])
        self.assertTrue(body["items"][0]["sqls"])

    def test_cannot_read_someone_elses_history(self):
        response = self.client.get(
            "/history?user_id=li", headers={"X-API-Key": "k-wang"}
        )
        self.assertEqual(response.status_code, 403)

    def test_other_user_sees_empty_history(self):
        response = self.client.get("/history", headers={"X-API-Key": "k-wang"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    def test_admin_can_read_others(self):
        response = self.client.get(
            "/history?user_id=li", headers={"X-API-Key": "k-admin"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["items"])

    def test_requires_authentication(self):
        self.assertEqual(self.client.get("/history").status_code, 401)

    def test_limit_is_clamped(self):
        response = self.client.get(
            "/history?limit=99999&user_id=li", headers={"X-API-Key": "k-li"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.json()["items"]), 100)


class AdminEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(
            json.dumps(
                {
                    "keys": [
                        {"key": "k-admin", "caller": "ops", "user_id": "ops", "is_admin": True},
                        {"key": "k-li", "caller": "li", "user_id": "li"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        cls.saved = {
            key: os.environ.get(key)
            for key in ("AI_SQL_AUTH_MODE", "AI_SQL_API_KEYS_FILE")
        }
        os.environ["AI_SQL_AUTH_MODE"] = "api_key"
        os.environ["AI_SQL_API_KEYS_FILE"] = str(key_file)
        reload_settings()

        from ai_sql_agent.api.app import app

        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        cls.tmpdir.cleanup()

    def test_non_admin_is_rejected(self):
        for path in ("/admin/schema/status", "/admin/schema/reload", "/admin/circuit/reset"):
            method = self.client.get if path.endswith("status") else self.client.post
            self.assertEqual(
                method(path, headers={"X-API-Key": "k-li"}).status_code, 403, path
            )

    def test_admin_can_read_schema_status(self):
        response = self.client.get(
            "/admin/schema/status", headers={"X-API-Key": "k-admin"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("fingerprints", response.json())

    def test_admin_can_reset_circuits(self):
        response = self.client.post(
            "/admin/circuit/reset", headers={"X-API-Key": "k-admin"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reset"])
