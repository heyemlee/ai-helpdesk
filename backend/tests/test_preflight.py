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
    ContractViolationError,
    InsecureDeploymentError,
    a2a_send_budget_s,
    check_security_config,
    check_timeout_budgets,
    is_production,
    outer_tool_timeout_s,
    run_startup_checks,
    security_config_problems,
)

COMPLETE = {
    "APP_ENV": "production",
    "AI_HELPDESK_API_KEY": "k",
    "AI_HELPDESK_AUTH_SECRET": "s",
    "AI_HELPDESK_PROTECT_CHAT": "true",
}


class TestProductionRequiresCompleteAuthConfig:
    """P0-2：漏配任一认证项，生产模式必须启动失败。"""

    def test_complete_config_passes(self):
        assert security_config_problems(COMPLETE) == []
        check_security_config(COMPLETE)  # 不抛

    @pytest.mark.parametrize(
        "missing",
        ["AI_HELPDESK_API_KEY", "AI_HELPDESK_AUTH_SECRET", "AI_HELPDESK_PROTECT_CHAT"],
    )
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

    def test_run_startup_checks_wires_both(self):
        with pytest.raises(InsecureDeploymentError):
            run_startup_checks(dict(COMPLETE, AI_HELPDESK_API_KEY=""))
        with pytest.raises(ContractViolationError):
            run_startup_checks(dict(COMPLETE, SQL_AGENT_A2A_SEND_TIMEOUT_S="90"))
        run_startup_checks(COMPLETE)  # 不抛
