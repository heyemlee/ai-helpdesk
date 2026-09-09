"""
启动前置校验。

这一组盯的是同一类缺陷：**配错了不报错，只是安全属性静默消失**。
它们没有运行时症状，所以只能在启动那一刻判——测试也就只能测启动那一刻。
"""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from core.preflight import (
    DEPLOYMENT_SCOPE_VAR,
    SCOPE_MULTI_TENANT,
    SCOPE_SINGLE_TENANT,
    TOKEN_ISSUER_CIDRS_VAR,
    ContractViolationError,
    InsecureDeploymentError,
    a2a_send_budget_s,
    check_deployment_scope,
    check_security_config,
    check_timeout_budgets,
    check_token_issuer_binding,
    is_production,
    outer_tool_timeout_s,
    run_startup_checks,
    security_config_problems,
    token_issuer_networks,
)

COMPLETE = {
    "APP_ENV": "production",
    "AI_HELPDESK_API_KEY": "k",
    "AI_HELPDESK_AUTH_SECRET": "s",
    "AI_HELPDESK_PROTECT_CHAT": "true",
    TOKEN_ISSUER_CIDRS_VAR: "10.0.0.0/8",
    # 部署范围是"完整的生产配置"的一部分——加这一行的时候，末尾那条
    # `run_startup_checks(COMPLETE)  # 不抛` 会先红一次，那正是断言接上了的证据。
    DEPLOYMENT_SCOPE_VAR: SCOPE_SINGLE_TENANT,
}


class TestProductionRequiresCompleteAuthConfig:
    """P0-2：漏配任一认证项，生产模式必须启动失败。"""

    def test_complete_config_passes(self):
        assert security_config_problems(COMPLETE) == []
        check_security_config(COMPLETE)  # 不抛

    @pytest.mark.parametrize(
        "missing",
        ["AI_HELPDESK_API_KEY", "AI_HELPDESK_AUTH_SECRET", "AI_HELPDESK_PROTECT_CHAT"],
    )  # 来源白名单是独立的一条检查，见 TestAuthTokenIsBoundToTheGateway
    def test_each_missing_item_fails_startup(self, missing):
        env = {k: v for k, v in COMPLETE.items() if k != missing}
        assert security_config_problems(env), f"{missing} 缺失应当被检出"
        with pytest.raises(InsecureDeploymentError) as ex:
            check_security_config(env)
        assert missing in str(ex.value), "报错要指名道姓说缺的是哪一项"

    def test_the_dangerous_combination_is_caught(self):
        """
        审查实测的那个组合：配了 AUTH_SECRET、漏配 API_KEY。

        两个开关分别看都像是打开的——令牌鉴权开着，写端点"有"鉴权依赖。
        合起来是 `/auth/token` 无鉴权，而它按请求体里的 user_id 签发令牌。
        """
        env = dict(COMPLETE)
        env.pop("AI_HELPDESK_API_KEY")
        with pytest.raises(InsecureDeploymentError):
            check_security_config(env)

    def test_blank_string_counts_as_missing(self):
        """`AI_HELPDESK_API_KEY=` 和没写这一行是同一件事。"""
        with pytest.raises(InsecureDeploymentError):
            check_security_config(dict(COMPLETE, AI_HELPDESK_API_KEY="   "))

    def test_protect_chat_must_be_truthy_not_merely_present(self):
        with pytest.raises(InsecureDeploymentError):
            check_security_config(dict(COMPLETE, AI_HELPDESK_PROTECT_CHAT="false"))

    @pytest.mark.parametrize("app_env", ["", "development", "dev", "staging"])
    def test_non_production_stays_open_for_local_work(self, app_env):
        """demo 模式的全部意义就是打开就能用，不要因为这条校验把本地联调堵死。"""
        check_security_config({"APP_ENV": app_env})  # 不抛

    @pytest.mark.parametrize("app_env,expected", [
        ("production", True), ("prod", True), ("PRODUCTION", True),
        ("development", False), ("", False),
    ])
    def test_production_detection(self, app_env, expected):
        assert is_production({"APP_ENV": app_env}) is expected


class TestAuthTokenNeverFailsOpen:
    """
    `/auth/token` 的鉴权**不看 APP_ENV**：没配管理 key 就一律 503。

    只靠启动校验不够——`app` 交给一个不跑 lifespan 的 ASGI 容器就会绕过它，
    而绕过之后原来的行为是**放行**。这一组走真实路由验证兜底那一层。
    """

    @staticmethod
    def _load(monkeypatch, **env):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
        monkeypatch.setenv("AI_HELPDESK_API_KEY", env.get("AI_HELPDESK_API_KEY", ""))
        monkeypatch.setenv("APP_ENV", env.get("APP_ENV", "development"))
        monkeypatch.setenv(TOKEN_ISSUER_CIDRS_VAR, env.get(TOKEN_ISSUER_CIDRS_VAR, ""))
        sys.modules.pop("api.main", None)
        return importlib.import_module("api.main")

    def test_no_admin_key_means_no_token_issuing(self, monkeypatch):
        """审查实测拿到 HTTP 200 的那条请求，现在必须拿不到令牌。"""
        m = self._load(monkeypatch, AI_HELPDESK_API_KEY="")
        resp = TestClient(m.app).post("/auth/token", json={"user_id": "victim"})
        assert resp.status_code == 503
        assert "token" not in resp.text

    def test_admin_key_configured_still_demands_it(self, monkeypatch):
        m = self._load(monkeypatch, AI_HELPDESK_API_KEY="s3cret")
        resp = TestClient(m.app).post("/auth/token", json={"user_id": "victim"})
        assert resp.status_code == 401

    def test_correct_admin_key_reaches_the_handler(self, monkeypatch):
        """有管理 key 就越过鉴权，落到业务层（未配 AUTH_SECRET → 409）。"""
        m = self._load(monkeypatch, AI_HELPDESK_API_KEY="s3cret")
        resp = TestClient(m.app).post(
            "/auth/token", json={"user_id": "u"}, headers={"X-API-Key": "s3cret"}
        )
        assert resp.status_code == 409

    def test_ordinary_write_endpoints_still_fail_closed_in_production(self, monkeypatch):
        """
        普通写端点在 demo 模式仍然放行（保持旧行为），但生产模式不放行。

        生产模式本来根本起不到这里，这条测的是 lifespan 被绕过时的兜底。
        """
        # 两种模式都会 503，但原因不同——所以断言看的是原因，不是状态码：
        # 生产模式被鉴权依赖挡住，demo 模式是越过了鉴权、落到"服务未就绪"。
        m = self._load(monkeypatch, AI_HELPDESK_API_KEY="", APP_ENV="production")
        resp = TestClient(m.app).post("/skills/reload")
        assert resp.status_code == 503 and "AI_HELPDESK_API_KEY" in resp.text

        m = self._load(monkeypatch, AI_HELPDESK_API_KEY="", APP_ENV="development")
        resp = TestClient(m.app).post("/skills/reload")
        assert "AI_HELPDESK_API_KEY" not in resp.text, "demo 模式应当越过鉴权"


class TestAuthTokenSourceIsCheckedAtRequestTime:
    """
    来源白名单在请求路径上真的生效——启动断言只保证它**配了**。

    TestClient 的对端是 127.0.0.1，所以白名单里有没有 127.0.0.1 就决定放不放行。
    """

    @staticmethod
    def _post(monkeypatch, cidrs: str, peer: str | None = "192.168.1.7", **headers):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
        monkeypatch.setenv("AI_HELPDESK_API_KEY", "s3cret")
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv(TOKEN_ISSUER_CIDRS_VAR, cidrs)
        sys.modules.pop("api.main", None)
        m = importlib.import_module("api.main")
        # TestClient 默认把对端报成字符串 "testclient"，不是 IP——
        # 显式给一个地址，这一层测的才是真的地址判定
        client = TestClient(m.app, client=(peer, 33333)) if peer else TestClient(m.app)
        return client.post(
            "/auth/token",
            json={"user_id": "u"},
            headers={"X-API-Key": "s3cret", **headers},
        )

    def test_source_outside_the_allowlist_is_refused(self, monkeypatch):
        resp = self._post(monkeypatch, "10.0.0.0/8", peer="203.0.113.9")
        assert resp.status_code == 403
        assert "token" not in resp.text

    def test_source_inside_the_allowlist_reaches_the_handler(self, monkeypatch):
        """在白名单里就越过这一层，落到业务层（未配 AUTH_SECRET → 409）。"""
        resp = self._post(monkeypatch, "192.168.1.0/24", peer="192.168.1.7")
        assert resp.status_code == 409

    def test_x_forwarded_for_cannot_get_past_it(self, monkeypatch):
        """
        **XFF 不作数。** 它是一个请求头，任何人都能写；拿它当来源判据，
        等于把白名单交给调用方自己填。判的必须是直连对端。
        """
        resp = self._post(
            monkeypatch, "10.0.0.0/8", peer="203.0.113.9",
            **{"X-Forwarded-For": "10.0.0.5"},
        )
        assert resp.status_code == 403, "伪造 X-Forwarded-For 不该能绕过来源检查"

    def test_unresolvable_peer_is_refused(self, monkeypatch):
        """
        拿不到对端地址时**拒绝**，不是放行。

        "无法确认来源"和"确认了来源不合法"，对一个签发身份的端点是同一件事。
        """
        resp = self._post(monkeypatch, "10.0.0.0/8", peer=None)
        assert resp.status_code == 403

    def test_empty_allowlist_is_unrestricted_in_dev(self, monkeypatch):
        """demo 模式保持旧行为：不配 = 不限制（生产模式下根本起不来）。"""
        resp = self._post(monkeypatch, "", peer="203.0.113.9")
        assert resp.status_code == 409


class TestAuthTokenIsBoundToTheGateway:
    """
    `/auth/token` 只应该被网关调到——这条**不是文档约定，是启动断言**。

    上一轮把它写进了 README 和部署文档。那是对的做法，但它是**说明**不是
    **修复**：约定不会在配错的时候报错。真正的修复是把签发接到登录系统，
    而本仓库里不存在那个组件（对接方案见 docs/auth-token-integration.md），
    所以在它出现之前，先把"可达范围"这一条做成代码层面的强制。
    """

    def test_production_without_an_allowlist_refuses_to_start(self):
        env = {k: v for k, v in COMPLETE.items() if k != TOKEN_ISSUER_CIDRS_VAR}
        with pytest.raises(InsecureDeploymentError) as ex:
            check_token_issuer_binding(env)
        assert "/auth/token" in str(ex.value)

    @pytest.mark.parametrize("wildcard", ["0.0.0.0/0", "::/0", "10.0.0.0/8,0.0.0.0/0"])
    def test_a_wildcard_allowlist_is_rejected(self, wildcard):
        """
        允许一切等于没有约束——而且**比不配还糟**：配置文件里看起来是有的。

        这是"为了让启动校验闭嘴"最容易被写出来的那个值，所以要单独拦。
        """
        with pytest.raises(InsecureDeploymentError) as ex:
            check_token_issuer_binding(dict(COMPLETE, **{TOKEN_ISSUER_CIDRS_VAR: wildcard}))
        assert "没有约束" in str(ex.value)

    def test_a_real_allowlist_passes(self):
        check_token_issuer_binding(dict(COMPLETE, **{
            TOKEN_ISSUER_CIDRS_VAR: "10.0.0.0/8, 127.0.0.1, 192.168.1.0/24",
        }))

    def test_a_typo_is_an_error_not_a_silently_dropped_entry(self):
        """
        拼错的 CIDR 不能被忽略。忽略 = 白名单被悄悄放宽，而配置看起来是对的
        ——和这个模块要防的其它几条是同一个错误模式。
        """
        with pytest.raises(InsecureDeploymentError) as ex:
            token_issuer_networks({TOKEN_ISSUER_CIDRS_VAR: "10.0.0.0/8,10.0.0.0/99"})
        assert "10.0.0.0/99" in str(ex.value)

    def test_non_production_stays_unrestricted(self):
        """本地联调不配这个变量也要能起来。"""
        check_token_issuer_binding({"APP_ENV": "development"})
        assert token_issuer_networks({}) == []


class TestTimeoutBudgetInterlock:
    """
    P1-1 / 契约 v1.6 §1③：外层工具超时必须**严格大于** send 预算。

    契约把它列为硬要求，但 CONSUMER-TESTS 明写"没有任何自动检查在防它"
    ——切档时忘了抬外层，改 send 完全无效且没有任何症状。这就是那个检查。
    """

    def test_todays_values_are_compliant(self):
        """今天：外层 47s（45+2）> send 45s。第二档，顺序对。"""
        assert outer_tool_timeout_s({}) == 47.0
        assert a2a_send_budget_s({}) == 45.0
        assert check_timeout_budgets({}) == []

    def test_raising_send_alone_is_rejected(self):
        """
        切到第三档只改 send 不改外层——这是契约点名的那个坑。

        47 ≤ 90 时外层先杀，send 预算等于被悄悄钉死在 47s。
        """
        env = {"SQL_AGENT_A2A_SEND_TIMEOUT_S": "90", "SQL_AGENT_TRANSPORT": "a2a"}
        with pytest.raises(ContractViolationError) as ex:
            check_timeout_budgets(env)
        assert "90" in str(ex.value) and "47" in str(ex.value)

    def test_equal_values_are_also_rejected(self):
        """"严格大于"不是"大于等于"：相等时谁先超时不确定。"""
        with pytest.raises(ContractViolationError):
            check_timeout_budgets(
                {"SQL_AGENT_TIMEOUT_S": "43", "SQL_AGENT_A2A_SEND_TIMEOUT_S": "45"}
            )

    def test_third_tier_done_properly_passes(self):
        """两个数一起动才是合规的切档。"""
        env = {
            "SQL_AGENT_TIMEOUT_S": "90",           # 外层 = 92
            "SQL_AGENT_A2A_SEND_TIMEOUT_S": "90",
            "SQL_AGENT_TRANSPORT": "a2a",
        }
        assert check_timeout_budgets(env) == []

    def test_widening_the_budget_before_streaming_carries_traffic_warns(self):
        """
        契约 §1③ 的硬要求：先能看到进度，再放宽超时。

        `SQL_AGENT_TRANSPORT` 还是 query 时用户看不到任何进度，
        这时候把等待放宽到 90s 只是把空白屏拉长——比现状更糟。
        不抛（这是部署判断不是配置错误），但必须留下告警。
        """
        env = {
            "SQL_AGENT_TIMEOUT_S": "95",
            "SQL_AGENT_A2A_SEND_TIMEOUT_S": "90",
            "SQL_AGENT_TRANSPORT": "query",
        }
        warnings = check_timeout_budgets(env)
        assert warnings and "承接流量" in warnings[0]

    def test_run_startup_checks_wires_all_of_them(self):
        with pytest.raises(InsecureDeploymentError):
            run_startup_checks(dict(COMPLETE, AI_HELPDESK_API_KEY=""))
        with pytest.raises(InsecureDeploymentError):
            run_startup_checks({k: v for k, v in COMPLETE.items()
                                if k != TOKEN_ISSUER_CIDRS_VAR})
        with pytest.raises(InsecureDeploymentError):
            run_startup_checks({k: v for k, v in COMPLETE.items()
                                if k != DEPLOYMENT_SCOPE_VAR})
        with pytest.raises(ContractViolationError):
            run_startup_checks(dict(COMPLETE, SQL_AGENT_A2A_SEND_TIMEOUT_S="90"))
        run_startup_checks(COMPLETE)  # 不抛


class TestDeploymentScopeIsDeclaredNotAssumed:
    """
    部署范围必须显式声明，且与其它配置**双向**校验。

    这一组守的是一个"没有症状"的失效：2026-09 定的试点范围
    （同权限内部人员 / 只读）让三条硬阻断降级为可接受——`/auth/token` 没接
    登录系统、下游最小授权没落到真实库、行范围不启用。**那三条降级只在系统
    确实跑在这个范围里时才成立**，而在这条检查之前，"这是单租户部署"只写在
    README 和 .env.example 里。文档不会在配错的时候报错。
    """

    def test_production_without_a_declared_scope_refuses_to_start(self):
        env = {k: v for k, v in COMPLETE.items() if k != DEPLOYMENT_SCOPE_VAR}
        with pytest.raises(InsecureDeploymentError) as exc:
            check_deployment_scope(env)
        assert DEPLOYMENT_SCOPE_VAR in str(exc.value)

    def test_there_is_no_default_on_purpose(self):
        """
        漏配和"想清楚了配成单租户"必须能区分——所以不给默认值。

        同样的理由下游用过一次：`predicate: "*"` 是**显式声明**"这张表不做
        行隔离"，与"没配"是两回事。默认值会把这两件事合并成同一个状态。
        """
        env = {k: v for k, v in COMPLETE.items() if k != DEPLOYMENT_SCOPE_VAR}
        with pytest.raises(InsecureDeploymentError):
            check_deployment_scope(env)
        check_deployment_scope(dict(env, **{DEPLOYMENT_SCOPE_VAR: SCOPE_SINGLE_TENANT}))

    def test_an_unknown_scope_value_is_rejected(self):
        with pytest.raises(InsecureDeploymentError):
            check_deployment_scope(dict(COMPLETE, **{DEPLOYMENT_SCOPE_VAR: "prod-ish"}))

    def test_single_tenant_rejects_impersonation(self):
        """
        声明单租户却打开 impersonation ⇒ 有人**以为**这套系统在做数据隔离。

        那个误解本身就是风险：它会让人把系统用在范围之外，而系统照常工作。
        """
        with pytest.raises(InsecureDeploymentError) as exc:
            check_deployment_scope(dict(COMPLETE, SQL_AGENT_IMPERSONATE="true"))
        assert "SQL_AGENT_IMPERSONATE" in str(exc.value)

    def test_multi_tenant_requires_impersonation(self):
        """
        反方向同样要拦：声明多租户却不传最终用户身份，所有查询都以服务身份
        发出，隔离根本不成立——**而链路看起来完全正常**。
        """
        env = dict(COMPLETE, **{DEPLOYMENT_SCOPE_VAR: SCOPE_MULTI_TENANT})
        with pytest.raises(InsecureDeploymentError) as exc:
            check_deployment_scope(env)
        assert "SQL_AGENT_IMPERSONATE" in str(exc.value)
        check_deployment_scope(dict(env, SQL_AGENT_IMPERSONATE="true"))  # 不抛

    def test_non_production_is_not_policed(self):
        check_deployment_scope({"APP_ENV": "development"})
