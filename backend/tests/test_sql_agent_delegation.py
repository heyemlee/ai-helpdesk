"""
业务数据委派（ai-sql-agent）。不发真实网络请求。

覆盖三件事：
  1. 门控——哪些意图才值得往下游发一次查询；
  2. 客户端——发出去的身份/会话/请求头对不对，出错是不是往上抛；
  3. 上下文构建——什么样的响应才配进 prompt 和 grounding sources。

第 3 点是这条链路最容易出安全事故的地方：注多了会让一段没有出处的模型
输出冒充"实时数据"，注少了会让真实交期被答案策略当成无依据承诺替换掉。
"""
import importlib
import sys

import pytest

from tools.sql_agent_client import (
    DEFAULT_DATA_INTENTS,
    SQLAgentClient,
    should_query_business_data,
)

# ── 假的 httpx 客户端 ────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """记录最后一次请求，返回预置响应。"""

    last_call: dict = {}

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status = status_code

    def __call__(self, *args, **kwargs):
        _FakeAsyncClient.last_call["init"] = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.last_call.update({"url": url, "headers": headers or {}, "json": json or {}})
        return _FakeResponse(self._payload, self._status)


def _patch_httpx(monkeypatch, payload, status_code=200):
    import tools.sql_agent_client as mod
    fake = _FakeAsyncClient(payload, status_code)
    monkeypatch.setattr(mod.httpx, "AsyncClient", fake)
    _FakeAsyncClient.last_call = {}
    return fake


OK_PAYLOAD = {
    "trace_id": "t-123",
    "route": "database_query",
    "answer": "订单 SO-2024-8891 已排产，预计 9 月 15 日出厂。",
    "queried_database": True,
    "selected_databases": ["erp_db", "mes_db"],
    "has_risk": False,
    "degraded": False,
}


# ── 1. 门控 ──────────────────────────────────────────────────────────────────

class TestGate:
    @pytest.mark.parametrize("intent", sorted(DEFAULT_DATA_INTENTS))
    def test_data_intents_pass(self, intent):
        assert should_query_business_data("我的柜子什么时候到", intent=intent)

    @pytest.mark.parametrize("intent", ["greeting", "technical", "installation", "quote_pricing", "other"])
    def test_non_data_intents_skipped(self, intent):
        assert not should_query_business_data("你好", intent=intent)

    def test_quote_pricing_excluded_by_default(self):
        """报价是对外承诺，默认不能由一次自然语言查询直接出数。"""
        assert "quote_pricing" not in DEFAULT_DATA_INTENTS

    def test_empty_message_skipped(self):
        assert not should_query_business_data("   ", intent="order_status")

    def test_broad_intent_with_order_id_passes(self, monkeypatch):
        """
        LLM 不可用时意图会退到宽泛的 query。这时候一句带订单号的问题依然
        该查库——否则模型降级的代价就转嫁到客户身上了。
        """
        assert should_query_business_data(
            "订单号 SO-2024-006 到哪了", intent="query",
            entities={"order_id": ["SO-2024-006"]},
        )

    def test_broad_intent_without_entity_skipped(self):
        """宽泛意图 + 没有单据号 = 泛泛的问题，不值得为它敲一次下游。"""
        assert not should_query_business_data("你们几点上班", intent="query", entities={})
        assert not should_query_business_data("你们几点上班", intent="query")

    def test_entity_bypass_does_not_apply_to_greeting(self):
        assert not should_query_business_data(
            "你好", intent="greeting", entities={"order_id": ["SO-1"]}
        )

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SQL_AGENT_INTENTS", "technical, installation")
        assert should_query_business_data("装反了", intent="technical")
        assert not should_query_business_data("我的单子呢", intent="order_status")


# ── 2. 客户端 ────────────────────────────────────────────────────────────────

class TestClient:
    def test_disabled_without_url(self, monkeypatch):
        monkeypatch.delenv("SQL_AGENT_URL", raising=False)
        assert not SQLAgentClient.from_env().enabled

    async def test_disabled_client_raises(self):
        with pytest.raises(RuntimeError):
            await SQLAgentClient().query("我的订单呢")

    async def test_sends_service_identity_by_default(self, monkeypatch):
        """impersonate 关闭时报服务身份：对面 key 多半没有 can_impersonate。"""
        _patch_httpx(monkeypatch, OK_PAYLOAD)
        client = SQLAgentClient(base_url="http://sql-agent:8000", api_key="k1")
        await client.query("我的订单呢", user_id="customer-42", session_id="conv-7")

        sent = _FakeAsyncClient.last_call
        assert sent["url"] == "http://sql-agent:8000/query"
        assert sent["headers"]["X-API-Key"] == "k1"
        assert sent["json"]["user_id"] == "ai-helpdesk"
        assert sent["json"]["session_id"] == "conv-7"
        assert sent["json"]["query"] == "我的订单呢"

    async def test_impersonate_forwards_real_user(self, monkeypatch):
        _patch_httpx(monkeypatch, OK_PAYLOAD)
        client = SQLAgentClient(base_url="http://sql-agent:8000", impersonate=True)
        await client.query("我的订单呢", user_id="customer-42", session_id="conv-7")
        assert _FakeAsyncClient.last_call["json"]["user_id"] == "customer-42"

    async def test_no_api_key_sends_no_header(self, monkeypatch):
        _patch_httpx(monkeypatch, OK_PAYLOAD)
        await SQLAgentClient(base_url="http://x:8000").query("q", session_id="s")
        assert "X-API-Key" not in _FakeAsyncClient.last_call["headers"]

    async def test_normalizes_response(self, monkeypatch):
        _patch_httpx(monkeypatch, OK_PAYLOAD)
        out = await SQLAgentClient(base_url="http://x:8000").query("q", session_id="s")
        assert out["answer"].startswith("订单 SO-2024-8891")
        assert out["queried_database"] is True
        assert out["selected_databases"] == ["erp_db", "mes_db"]
        assert out["trace_id"] == "t-123"

    async def test_http_error_propagates(self, monkeypatch):
        """失败必须往上抛，否则 ToolManager 的熔断器永远看不到故障。"""
        import httpx
        _patch_httpx(monkeypatch, {}, status_code=500)
        with pytest.raises(httpx.HTTPStatusError):
            await SQLAgentClient(base_url="http://x:8000").query("q", session_id="s")

    async def test_non_object_response_rejected(self, monkeypatch):
        _patch_httpx(monkeypatch, ["not", "an", "object"])
        with pytest.raises(ValueError):
            await SQLAgentClient(base_url="http://x:8000").query("q", session_id="s")


# ── 3. 上下文构建 ────────────────────────────────────────────────────────────

def _load_main(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
    sys.modules.pop("api.main", None)
    return importlib.import_module("api.main")


class _StubToolManager:
    def __init__(self, data, success=True):
        self._data = data
        self._success = success
        self.calls = []

    async def call(self, name, params, context=None, **kwargs):
        self.calls.append((name, params))
        from tools.tool_manager import ToolResult
        return ToolResult(success=self._success, data=self._data, tool_name=name)


def _wire(m, monkeypatch, data, success=True):
    monkeypatch.setattr(m, "_sql_agent", SQLAgentClient(base_url="http://x:8000"))
    tm = _StubToolManager(data, success)
    monkeypatch.setattr(m, "_tool_manager", tm)
    return tm


class TestContextBuilder:
    async def test_real_data_is_injected(self, monkeypatch):
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD))
        text, used = await m._build_order_data_context(
            "我的订单呢", intent="order_status", user_id="u1", conv_id="c1"
        )
        assert used is True
        # 答案原文必须在里面：答案策略靠它给交期日期找出处
        assert "9 月 15 日" in text
        assert "erp_db" in text and "t-123" in text

    async def test_conversation_id_is_used_as_session(self, monkeypatch):
        m = _load_main(monkeypatch)
        tm = _wire(m, monkeypatch, dict(OK_PAYLOAD))
        await m._build_order_data_context("q", intent="order_status", user_id="u1", conv_id="c9")
        assert tm.calls[0][1]["session_id"] == "c9"

    async def test_entity_bypass_reaches_downstream(self, monkeypatch):
        m = _load_main(monkeypatch)
        tm = _wire(m, monkeypatch, dict(OK_PAYLOAD))
        _text, used = await m._build_order_data_context(
            "订单号 SO-2024-006 到哪了",
            intent="query",
            entities={"order_id": ["SO-2024-006"]},
        )
        assert used is True
        assert tm.calls, "带订单号的宽泛意图应该发起下游调用"

    async def test_skipped_for_non_data_intent(self, monkeypatch):
        m = _load_main(monkeypatch)
        tm = _wire(m, monkeypatch, dict(OK_PAYLOAD))
        text, used = await m._build_order_data_context("你好", intent="greeting")
        assert (text, used) == ("", False)
        assert tm.calls == [], "非数据类意图不该发起下游调用"

    async def test_chat_route_answer_is_not_grounding(self, monkeypatch):
        """
        下游判断这问题不需要查库时，它的 answer 是模型说的话而不是数据。
        把它当依据注入，等于给一段没有出处的文本盖上"实时数据"的章。
        """
        m = _load_main(monkeypatch)
        payload = dict(OK_PAYLOAD, queried_database=False, route="data_qa")
        _wire(m, monkeypatch, payload)
        assert await m._build_order_data_context("q", intent="order_status") == ("", False)

    async def test_unavailable_becomes_a_visible_notice(self, monkeypatch):
        """
        委派失败不能静默。

        实测里最危险的一种形态：下游其实查完了（HTTP 200，40.5s），只是调用方
        在超时点先放弃，客户拿到一段听起来完全合理、实则没有任何系统依据的
        回答——客户和坐席都分辨不出来这次降级发生过。所以失败要明写进上下文。
        """
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch,
              {"answer": "", "queried_database": False, "unavailable": True, "error": "ReadTimeout"},
              success=False)
        text, is_evidence = await m._build_order_data_context("q", intent="order_status")
        assert text, "失败必须留下可见提示，不能返回空"
        assert "ReadTimeout" in text, "提示里要带上原因，否则排查时看不出是超时"
        assert "不要给出任何具体的订单状态" in text
        assert is_evidence is False, "降级提示不是证据，不能进 grounding_sources"

    async def test_notice_never_becomes_evidence(self, monkeypatch):
        """
        提示块绝不能当出处。

        它进了 grounding_sources 的话，答案策略会拿"查询失败了"这句话去给
        一个凭空的价格或日期找到"出处"——等于用一条故障通知批准一次无依据承诺。
        """
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, {"unavailable": True, "error": "boom"}, success=False)
        _text, is_evidence = await m._build_order_data_context("q", intent="order_status")
        assert is_evidence is False

    async def test_real_data_is_evidence(self, monkeypatch):
        """反过来：真数据必须是证据，否则真实交期会被答案策略替换成安全话术。"""
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD))
        _text, is_evidence = await m._build_order_data_context("q", intent="order_status")
        assert is_evidence is True

    async def test_empty_answer_dropped(self, monkeypatch):
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD, answer="   "))
        assert await m._build_order_data_context("q", intent="order_status") == ("", False)

    async def test_risk_flag_becomes_a_caution_line(self, monkeypatch):
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD, has_risk=True))
        text, used = await m._build_order_data_context("q", intent="order_status")
        assert used is True
        assert "异常提示" in text

    async def test_degraded_flag_becomes_a_caution_line(self, monkeypatch):
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD, degraded=True, degrade_reason="模型不可用"))
        text, _ = await m._build_order_data_context("q", intent="order_status")
        assert "降级" in text and "模型不可用" in text

    async def test_disabled_delegation_is_a_noop(self, monkeypatch):
        m = _load_main(monkeypatch)
        monkeypatch.setattr(m, "_sql_agent", SQLAgentClient())   # 没有 base_url
        monkeypatch.setattr(m, "_tool_manager", _StubToolManager(dict(OK_PAYLOAD)))
        assert await m._build_order_data_context("q", intent="order_status") == ("", False)


class TestCurrencyIsDeclared:
    """
    下游只返回数字不返回币种（`{"contract_amount": 103007.55}`），客服无从得知，
    中文回答里会默认写成「元」——接美国厂的库时，报给 builder 的金额就带了错误
    的币种。这不是能从数据推断的东西，只能由部署方声明。
    """

    async def test_currency_appears_in_the_context_when_declared(self, monkeypatch):
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD))
        monkeypatch.setattr(
            m, "_sql_agent", SQLAgentClient(base_url="http://x:8000", currency="USD（美元）")
        )
        text, is_evidence = await m._build_order_data_context("q", intent="order_status")
        assert "USD（美元）" in text
        assert "不要换算" in text
        assert is_evidence is True, "币种说明不能把这块降级成非证据"

    async def test_nothing_is_said_when_not_declared(self, monkeypatch):
        """未声明时一个字不加——行为与加这个字段之前完全一致。"""
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD))
        text, _ = await m._build_order_data_context("q", intent="order_status")
        assert "币种" not in text

    def test_from_env_picks_the_declaration_up(self, monkeypatch):
        monkeypatch.setenv("SQL_AGENT_URL", "http://x:8000")
        monkeypatch.setenv("SQL_AGENT_CURRENCY", "USD（美元）")
        assert SQLAgentClient.from_env().currency == "USD（美元）"

    def test_from_env_defaults_to_silent(self, monkeypatch):
        monkeypatch.setenv("SQL_AGENT_URL", "http://x:8000")
        monkeypatch.delenv("SQL_AGENT_CURRENCY", raising=False)
        assert SQLAgentClient.from_env().currency == ""


class TestA2ATransport:
    """传输开关：默认 query（今天验证过的路）；a2a 时 -32004 在流前 → 改发 send，流中失败不重发。"""

    def test_default_transport_is_query(self, monkeypatch):
        monkeypatch.setenv("SQL_AGENT_URL", "http://x:8000")
        monkeypatch.delenv("SQL_AGENT_TRANSPORT", raising=False)
        assert SQLAgentClient.from_env().transport == "query"

    async def test_prestream_32004_falls_back_to_send(self, monkeypatch):
        from tools.a2a_client import StreamUnsupported
        c = SQLAgentClient(base_url="http://x:8000", transport="a2a")
        calls = []

        class Fake:
            async def streaming_supported(self):
                return True

            async def stream(self, *a, **k):
                calls.append("stream")
                raise StreamUnsupported("off")

            async def send(self, *a, **k):
                calls.append("send")
                return {"answer": "ok", "queried_database": True, "selected_databases": [],
                        "has_risk": False, "degraded": False, "degrade_reason": "",
                        "trace_id": "t", "route": "database_query"}
        c._a2a = Fake()
        out = await c.handler({"query": "q"}, {})
        assert calls == ["stream", "send"] and out["answer"] == "ok"

    async def test_midstream_failure_does_not_resend(self, monkeypatch):
        from tools.a2a_client import A2AError
        c = SQLAgentClient(base_url="http://x:8000", transport="a2a")
        calls = []

        class Fake:
            async def streaming_supported(self):
                return True

            async def stream(self, *a, **k):
                calls.append("stream")
                raise A2AError("终态为 failed")

            async def send(self, *a, **k):
                calls.append("send")
                return {}
        c._a2a = Fake()
        with pytest.raises(A2AError):
            await c.handler({"query": "q"}, {})
        assert calls == ["stream"], "流中途失败不得重发 send（契约 §5.4）"

    async def test_progress_cb_reaches_the_stream(self):
        c = SQLAgentClient(base_url="http://x:8000", transport="a2a")
        got = {}

        class Fake:
            async def streaming_supported(self):
                return True

            async def stream(self, q, u, s, on_progress=None):
                got["cb"] = on_progress
                return {"answer": "ok", "queried_database": True, "selected_databases": [],
                        "has_risk": False, "degraded": False, "degrade_reason": "",
                        "trace_id": "t", "route": "x"}

        def cb(_evt):
            return None

        c._a2a = Fake()
        await c.handler({"query": "q"}, {"progress_cb": cb})
        assert got["cb"] is cb
