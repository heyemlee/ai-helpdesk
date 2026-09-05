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


# 契约 v1.6 §4.6 之后，"一份可信的载荷"必须包含 `steps[]`：
# `queried_database` 的服务端定义只有一行 `pipeline_result is not None`，
# SQL 全跑挂了它也是 true，所以"跑成什么样"只能从逐步骤结果读出来。
# 没有 steps 的载荷按 execution_status=failed 处置（§4.6.7 缺失 = 不可信），
# 这不是本测试放宽了什么，是这个夹具原先漏掉了线上真的会发的字段
# （`/query` 的 `step_logs`、A2A data part 的 `steps`，同一个 StepLogResponse）。
#
# 这里写 `steps` 而不是 `step_logs`：`_build_order_data_context` 消费的是
# **工具归一之后**的结果，两条路径的名字在客户端那一层就已经对齐了
# （名字对齐本身另有用例：TestQueryClient::test_step_logs_are_normalized_to_steps）。
OK_PAYLOAD = {
    "trace_id": "t-123",
    "route": "database_query",
    "answer": "订单 SO-2024-8891 已排产，预计 9 月 15 日出厂。",
    "queried_database": True,
    "selected_databases": ["erp_db", "mes_db"],
    "has_risk": False,
    "degraded": False,
    "steps": [{
        "database": "erp_db", "sql": "SELECT ship_date FROM job WHERE job_no = 'SO-2024-8891'",
        "final_sql": "SELECT ship_date FROM job WHERE job_no = 'SO-2024-8891'",
        "success": True, "row_count": 1, "truncated": False,
        "rows": [{"ship_date": "2026-09-15"}], "error": None,
        "repair_attempts": [], "risk_findings": [],
    }],
}


# 同一份载荷的 `/query` 形状：逐步骤日志那个数组在这条路上叫 `step_logs`。
OK_PAYLOAD_QUERY_SHAPE = {
    **{k: v for k, v in OK_PAYLOAD.items() if k != "steps"},
    "step_logs": OK_PAYLOAD["steps"],
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

    async def test_step_logs_are_normalized_to_steps(self, monkeypatch):
        """
        `/query` 叫 `step_logs`，A2A 的 data part 叫 `steps`——名字必须在
        客户端这一层就对齐，之后信任判定才可能是同一套。

        对不齐的后果不是报错，是**静默降级**：`steps` 取不到 → 按缺失处置 →
        execution_status=failed → 每一次直连查询都不再是出处，而链路看起来
        完全正常。回滚（契约 §6.2）正好会把流量切到这条路上。
        """
        _patch_httpx(monkeypatch, OK_PAYLOAD_QUERY_SHAPE)
        out = await SQLAgentClient(base_url="http://x:8000").query("q", session_id="s")
        assert out["steps"] == OK_PAYLOAD_QUERY_SHAPE["step_logs"]

        from core.evidence import assess
        assert assess(out).execution_status == "success"
        assert assess(out).usable_as_source is True

    async def test_trust_booleans_are_not_coerced_by_the_client(self, monkeypatch):
        """
        客户端**不做** `bool()` 转换，原样带过去。

        原先这里是 `bool(data.get("has_risk", False))`：字符串 "false" 会变成
        True、字段缺失会变成 False（= 无风险 = 采信）。转换集中到 core.evidence
        一处之后，这里要守住的是"别又在半路上转一次"。
        """
        _patch_httpx(monkeypatch, dict(OK_PAYLOAD_QUERY_SHAPE, has_risk="false"))
        out = await SQLAgentClient(base_url="http://x:8000").query("q", session_id="s")
        assert out["has_risk"] == "false", "客户端不该把它变成布尔"

        from core.evidence import assess
        assert assess(out).has_risk is True, "字符串布尔按不可信处置"
        assert assess(out).usable_as_source is False

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

    async def test_risk_flag_is_a_real_gate_not_just_a_caution_line(self, monkeypatch):
        """
        `has_risk=true` 必须**挡住出处**，不只是追加一句提示。

        这是审查 P0-3 的 (c)：契约 §9.1 把信任门槛描述成三个布尔的与，
        而实现里只有 `queried_database` 是闸，`has_risk` / `degraded` 只往
        上下文里追加一段文本、函数照样返回 True，结果照样进 grounding_sources。
        后果不是"门槛没做严"，是**带风险的数据被当成可信出处**：答案策略拿它
        给一个语义对不上的金额背书。数据仍然进上下文（丢掉比现状更糟），
        但它不能和已核实的金额/日期共享同一个可信标志。
        """
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD, has_risk=True))
        text, used = await m._build_order_data_context("q", intent="order_status")
        assert used is False, "带风险的结果不得进 grounding_sources"
        assert "异常提示" in text
        assert "未通过可信校验" in text, "第一行就要让 Agent 看出这段不可作依据"
        assert OK_PAYLOAD["answer"] in text, "数据本身不能丢，丢了比现状更糟"

    async def test_degraded_flag_is_a_real_gate_not_just_a_caution_line(self, monkeypatch):
        """`degraded=true` 同理：历史 SQL 重放是完整可用但可能过期的结果。"""
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD, degraded=True, degrade_reason="模型不可用"))
        text, used = await m._build_order_data_context("q", intent="order_status")
        assert used is False, "降级结果不得进 grounding_sources"
        assert "降级" in text and "模型不可用" in text
        assert OK_PAYLOAD["answer"] in text

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

    async def test_unknown_currency_forbids_guessing_one(self, monkeypatch):
        """
        两边都没声明时**不是**"一个字不加"。

        原先的行为是沉默，而沉默正是那次事故的形态：中文回答会自己默认写成
        「元」。契约 §4.6.5 要求这种情况下原样呈现数字、说明币种未知，
        并且明确禁止调用方擅自补符号。所以这里必须说话，只是说的是"别猜"。
        """
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD))
        monkeypatch.setattr(m, "_sql_agent", SQLAgentClient(base_url="http://x:8000"))
        text, _ = await m._build_order_data_context("q", intent="order_status")
        assert "不要补" in text and "币种" in text
        assert "元" in text, "要点名「元」这个默认值，泛泛说「不要猜」拦不住它"

    async def test_configured_currency_is_labelled_as_configuration(self, monkeypatch):
        """
        全局配置来的币种**必须在句子里暴露来源**。

        `SQL_AGENT_CURRENCY` 是部署的属性不是数据的属性：一个部署里只要有
        多币种的库，它必然静默给错。在服务端下发 `currency` 之前不能删它
        （删了就退回默认写「元」那次事故），但也不能让它伪装成查出来的事实。
        """
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD))
        monkeypatch.setattr(
            m, "_sql_agent", SQLAgentClient(base_url="http://x:8000", currency="USD")
        )
        text, _ = await m._build_order_data_context("q", intent="order_status")
        assert "SQL_AGENT_CURRENCY" in text, "来源要写出来，不能只说「币种是 USD」"
        assert "不是" in text and "本次查询返回的字段" in text

    async def test_currency_from_the_payload_beats_the_global_config(self, monkeypatch):
        """
        服务端一旦按 §4.6.5 下发 `currency`，就以数据为准，不再提配置。

        这条今天就该有：`currency` 上线是服务端单方面的改动，调用方不会收到
        通知，没有这条用例的话会出现"数据说 EUR、回答里写 USD"而没人发现。
        """
        m = _load_main(monkeypatch)
        _wire(m, monkeypatch, dict(OK_PAYLOAD, currency="EUR"))
        monkeypatch.setattr(
            m, "_sql_agent", SQLAgentClient(base_url="http://x:8000", currency="USD")
        )
        text, _ = await m._build_order_data_context("q", intent="order_status")
        assert "EUR" in text and "随数据一同返回" in text
        assert "SQL_AGENT_CURRENCY" not in text

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
