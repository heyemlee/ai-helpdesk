"""
生产模式启动闸：发布时的安全默认值。

原来这些都是警告：readiness 明确把「缺模型凭证走 Mock」当成可就绪
（`app.py` 的 readyz），启动只读审计发现违规也只 log 一条 WARNING。
**警告在生产里等于没有**——没人会在凌晨看日志，而这几项里任何一项不合格，
服务都会以一种看起来完全正常的方式给出错误答案。
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from ai_sql_agent.api.app import ProductionReadinessError, check_production_readiness
from ai_sql_agent.core import get_settings, reload_settings


class _FakeApp:
    def __init__(self, readonly_audits=None, records=None, policies=None, databases=None):
        store = SimpleNamespace(records=records or {}, policies=policies or {}, users={})
        pipeline = SimpleNamespace(
            databases=[SimpleNamespace(name=n) for n in (databases or [])],
            business_metas={},
        )
        self.state = SimpleNamespace(
            auth=SimpleNamespace(store=store),
            pipeline=pipeline,
            readonly_audits=readonly_audits or {},
        )


class _EnvPatch:
    """临时改环境变量并 reload settings；测试之间不能互相污染。"""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        return False


class DevelopmentModeTest(unittest.TestCase):
    def test_problems_are_only_warnings_outside_production(self):
        """本地跑 demo 与测试不该被这些检查挡住。"""
        with _EnvPatch(AI_SQL_APP_ENV="development", AI_SQL_AUTH_MODE="disabled"):
            problems = check_production_readiness(_FakeApp())
        self.assertTrue(problems, "开发模式也应该把问题列出来，只是不阻断")


class ProductionModeTest(unittest.TestCase):
    def _assert_blocks(self, needle: str, **env):
        env.setdefault("AI_SQL_APP_ENV", "production")
        with _EnvPatch(**env):
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(_FakeApp())
        self.assertIn(needle, str(caught.exception))

    def test_mock_model_blocks_startup(self):
        self._assert_blocks("Mock", AI_SQL_ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
                            AI_SQL_OPENAI_API_KEY="", ANTHROPIC_API_KEY="")

    def test_auth_disabled_blocks_startup(self):
        self._assert_blocks("AI_SQL_AUTH_MODE", AI_SQL_AUTH_MODE="disabled")

    def test_unknown_role_full_access_blocks_startup(self):
        self._assert_blocks("UNKNOWN_ROLE_POLICY",
                            AI_SQL_UNKNOWN_ROLE_POLICY="full_access")

    def test_demo_semantic_layer_blocks_startup(self):
        self._assert_blocks("AI_SQL_SEMANTIC_DIR", AI_SQL_SEMANTIC_DIR="")

    def test_writable_business_account_blocks_startup(self):
        with _EnvPatch(AI_SQL_APP_ENV="production"):
            app = _FakeApp(readonly_audits={"app_rw": "writable"})
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        self.assertIn("app_rw", str(caught.exception))
        self.assertIn("不是只读", str(caught.exception))

    def test_api_key_without_roles_blocks_startup(self):
        from ai_sql_agent.auth import ApiKeyRecord

        with _EnvPatch(AI_SQL_APP_ENV="production"):
            app = _FakeApp(records={
                "k": ApiKeyRecord(key="k", caller="no-role-caller", roles=())
            })
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        self.assertIn("no-role-caller", str(caught.exception))

    def test_policy_pointing_at_a_missing_database_blocks_startup(self):
        """
        策略引用语义层里没有的库：语法完全合法，deny by default 之下那把 Key
        什么都查不到，而且**没有任何症状**。deploy/api_keys.example.json 一度
        就是这个形态（写着契约 v1.6 已退役的 pm_db / mes_db）。
        """
        from ai_sql_agent.auth import DataPolicy

        with _EnvPatch(AI_SQL_APP_ENV="production"):
            app = _FakeApp(
                policies={"analyst": DataPolicy(allow_tables=("pm_db.*",))},
                databases=["erp_db", "prod_db"],
            )
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        self.assertIn("analyst:pm_db", str(caught.exception))

    def test_wildcard_database_segment_is_not_flagged(self):
        """`*` 本来就是「所有库」，不该被当成一个不存在的库名。"""
        from ai_sql_agent.auth import DataPolicy

        from ai_sql_agent.api.app import _policy_databases_not_in_semantic_layer

        app = _FakeApp(
            policies={"r": DataPolicy(allow_tables=("*",), deny_columns=("*.*.id_card_no",))},
            databases=["erp_db"],
        )
        self.assertEqual(_policy_databases_not_in_semantic_layer(app), [])

    def test_production_without_a_declared_scope_blocks_startup(self):
        """
        部署范围必须显式声明。

        试点范围（同权限内部人员 / 只读）让三条硬阻断降级为可接受——最小授权
        没落到真实业务库、行范围不启用、impersonation 不启用。**那三条降级只在
        系统确实跑在这个范围里时才成立**，而在这条断言之前，"这是单租户部署"
        只写在文档里。文档不会在配错的时候报错。
        """
        with _EnvPatch(AI_SQL_APP_ENV="production"):
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(_FakeApp())
        self.assertIn("AI_SQL_DEPLOYMENT_SCOPE", str(caught.exception))

    def test_single_tenant_scope_rejects_impersonating_keys(self):
        """
        单租户范围里 impersonation 不解决任何问题（所有人权限相同）。
        打开它说明有人**以为**这里在按最终用户隔离数据——那个误解会让人把
        系统用在范围之外，而系统照常工作。
        """
        from ai_sql_agent.auth import ApiKeyRecord

        with _EnvPatch(
            AI_SQL_APP_ENV="production",
            AI_SQL_DEPLOYMENT_SCOPE="internal_single_tenant",
        ):
            app = _FakeApp(records={
                "k": ApiKeyRecord(key="k", caller="helpdesk", roles=("analyst",),
                                  can_impersonate=True)
            })
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        self.assertIn("helpdesk", str(caught.exception))

    def test_a_fully_configured_production_setup_passes(self):
        """反向断言：一个「永远拒绝」的闸门是没用的闸门。"""
        from ai_sql_agent.auth import ApiKeyRecord

        with _EnvPatch(
            AI_SQL_APP_ENV="production",
            AI_SQL_AUTH_MODE="api_key",
            AI_SQL_UNKNOWN_ROLE_POLICY="deny",
            AI_SQL_SEMANTIC_DIR="tools/sim_erp/semantic",
            AI_SQL_ANTHROPIC_API_KEY="test-key",
            AI_SQL_MODEL_PROVIDER="anthropic",
            # 范围是"完整生产配置"的一部分。加这一行的时候本条会先红一次，
            # 那正是断言接上了的证据。
            AI_SQL_DEPLOYMENT_SCOPE="internal_single_tenant",
        ):
            from ai_sql_agent.auth import DataPolicy

            app = _FakeApp(
                readonly_audits={"ai_sql_ro": "readonly"},
                records={"k": ApiKeyRecord(key="k", caller="bi", roles=("analyst",))},
                policies={"analyst": DataPolicy(allow_tables=("erp_db.*",))},
                databases=["erp_db"],
            )
            self.assertEqual(check_production_readiness(app), [])


if __name__ == "__main__":
    unittest.main()


class AuthorizationModelGateTest(unittest.TestCase):
    """
    行范围与 impersonation 在生产模式下的启动检查。设计见 docs/authz-model.md。

    两条都是「运行时宽松、配置校验时严格」：不破坏现有部署，
    但**宽松必须是有人写下来的**，不能是默认值兜出来的。
    """

    def _app(self, **kwargs):
        return _FakeApp(**kwargs)

    def test_impersonating_key_without_users_section_blocks_startup(self):
        from ai_sql_agent.auth import ApiKeyRecord

        with _EnvPatch(AI_SQL_APP_ENV="production"):
            app = self._app(
                records={"k": ApiKeyRecord(key="k", caller="bi-portal",
                                           roles=("analyst",), can_impersonate=True)},
            )
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        message = str(caught.exception)
        self.assertIn("bi-portal", message)
        self.assertIn("impersonation", message)

    def test_declaring_users_clears_it(self):
        from ai_sql_agent.api.app import _impersonating_keys_without_user_policies
        from ai_sql_agent.auth import ApiKeyRecord

        app = self._app(
            records={"k": ApiKeyRecord(key="k", caller="bi", can_impersonate=True)},
        )
        app.state.auth.store.users = {"alice": ("analyst",)}
        self.assertEqual(_impersonating_keys_without_user_policies(app), [])

    def test_non_impersonating_keys_are_not_flagged(self):
        from ai_sql_agent.api.app import _impersonating_keys_without_user_policies
        from ai_sql_agent.auth import ApiKeyRecord

        app = self._app(
            records={"k": ApiKeyRecord(key="k", caller="li", can_impersonate=False)},
        )
        self.assertEqual(_impersonating_keys_without_user_policies(app), [])

    def test_table_without_row_scope_is_flagged(self):
        from ai_sql_agent.api.app import _tables_without_row_scope
        from ai_sql_agent.auth import DataPolicy

        app = self._app(policies={"analyst": DataPolicy(allow_tables=("erp_db.*",))})
        app.state.pipeline.business_metas = {"erp_db": {"job": {}, "invoice": {}}}
        self.assertEqual(
            _tables_without_row_scope(app),
            ["analyst:erp_db.invoice", "analyst:erp_db.job"],
        )

    def test_explicit_all_rows_declaration_clears_it(self):
        """
        `predicate: "*"` 是**显式**声明「这张表不做行隔离」——它和「没配」
        不是一回事，正是这条区分让默认值不再兜出一个静默的全放行。
        """
        from ai_sql_agent.api.app import _tables_without_row_scope
        from ai_sql_agent.auth import DataPolicy

        policy = DataPolicy.from_dict({
            "allow_tables": ["erp_db.*"],
            "row_filters": [
                {"table": "erp_db.job",
                 "predicate": "owner_id = current_setting('app.user_id', true)"},
                {"table": "erp_db.*", "predicate": "*"},
            ],
        })
        app = self._app(policies={"analyst": policy})
        app.state.pipeline.business_metas = {"erp_db": {"job": {}, "invoice": {}}}
        self.assertEqual(_tables_without_row_scope(app), [])

    def test_tables_the_policy_denies_are_not_flagged(self):
        from ai_sql_agent.api.app import _tables_without_row_scope
        from ai_sql_agent.auth import DataPolicy

        app = self._app(policies={"r": DataPolicy(allow_tables=("erp_db.job",))})
        app.state.pipeline.business_metas = {"erp_db": {"job": {}, "secret": {}}}
        self.assertEqual(_tables_without_row_scope(app), ["r:erp_db.job"])
