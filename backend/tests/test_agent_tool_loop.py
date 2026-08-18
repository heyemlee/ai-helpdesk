"""
工具循环接进专家和编排器之后（BaseAgent._stream_with_tools、run / run_stream / run_parallel）。

模型是脚本化的假模型（conftest.ScriptedModel），工具是真的 KnowledgeSearchTool /
OrderDataQueryTool，底下接一个记账的假 ToolManager：格式化、信任判定、白名单和策略门
都走线上那一份代码，只是不出网。
"""
import importlib
import json
import sys

from conftest import ScriptedModel, make_request, text_step, tool_step
from prometheus_client import REGISTRY

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType, RoutingDecision
from app.agents.tool_loop import LoopConfig, LoopSetup, TurnContext
from app.agents.turn_prep import TurnDeps, prepare_turn
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.core.llm_guard import REASON_CREDENTIAL, LLMUnavailableError
from app.core.openai_compat import ToolsUnsupportedError
from app.memory.conversation_memory import MemoryContext
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import ToolResult
from app.tools.turn_tools import Prefetch, loop_tools

KNOWLEDGE = [{"title": "补件流程", "content": "缺件补发在核实后安排。", "score": 0.6,
              "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}]
ORDER_DATA = {
    "answer": "SO-1 的补件已安排，3个工作日内送到。", "queried_database": True,
    "execution_status": "success", "has_risk": False, "degraded": False,
    "selected_databases": ["wms_db"], "trace_id": "t-1",
    "steps": [{"database": "wms_db", "success": True}],
}
UNAVAILABLE = {"answer": "", "queried_database": False, "unavailable": True, "error": "ReadTimeout"}


class _Tools:
    """记账的假 ToolManager：知识库给一段片段，业务数据按构造时给的结果回。"""

    def __init__(self, order_data=None, order_success=True):
        self.order_data = dict(ORDER_DATA) if order_data is None else order_data
        self.order_success = order_success
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, params, context=None, **_):
        self.calls.append((name, params))
        if name == "knowledge_search":
            return ToolResult(success=True, data=KNOWLEDGE, tool_name=name)
        return ToolResult(success=self.order_success, data=dict(self.order_data), tool_name=name)

    def delegations(self):
        return [params for name, params in self.calls if name == "order_data_query"]


def _turn(tools, *, intent="missing_part", data=True):
    prefetch = Prefetch(tool_manager=tools, sql_agent=SQLAgentClient(base_url="http://x:8000") if data else None)
    return TurnContext(user_id="u1", conv_id="c1", tools=loop_tools(prefetch), config=LoopConfig(), intent=intent)


def _orchestrator(*models: tuple[AgentType, ScriptedModel]) -> AgentOrchestrator:
    orch = AgentOrchestrator(api_key="test-key")
    for agent_type, model in models:
        for agent in orch._pool[agent_type]:
            agent._client = model
    return orch


async def _stream(orch, req):
    return [e async for e in orch.run_stream(req)]


def _deltas(events):
    return "".join(e["text"] for e in events if e["type"] == "delta")


async def test_without_a_turn_the_specialist_makes_one_plain_call():
    """循环关着（默认）：请求里没有 tools，提示词里没有工具那一段，结果上没有轨迹。"""
    model = ScriptedModel(text_step("补件已登记。"))
    result = await _orchestrator((AgentType.ORDER, model)).run(
        make_request("补件到哪了", IntentCategory.MISSING_PART))

    assert result.response.startswith("补件已登记")
    [request] = model.requests
    assert "tools" not in request and "[工具使用" not in request["system"][0]["text"]
    assert (result.trajectory, result.answer_usage) == ([], {})


async def test_data_the_specialist_fetched_itself_grounds_its_answer():
    tools = _Tools()
    model = ScriptedModel(
        tool_step(("call_1", "order_data_query", {"query": "SO-1 补件进度"})),
        text_step("SO-1 的补件会在3个工作日内送到。", "\n[[handoff:no]]"),
    )
    req = make_request("我那单补件到哪了", IntentCategory.MISSING_PART, turn=_turn(tools))
    events = await _stream(_orchestrator((AgentType.ORDER, model)), req)

    # 这句时效承诺本来没有出处、会被替换；工具查回来的数据当场成了出处，原样放行
    assert "3个工作日内送到" in _deltas(events)
    result = events[-1]["result"]
    assert result.policy_violations == [] and result.handoff is False
    start, end = [e for e in events if e["type"] == "tool"]
    assert (start["phase"], start["tool"], start["args"]) == ("start", "order_data_query", {"query": "SO-1 补件进度"})
    assert (end["phase"], end["tool"], end["evidence"]) == ("end", "order_data_query", True)
    # 身份是服务端给的，不是模型填的
    assert tools.delegations() == [{"query": "SO-1 补件进度", "user_id": "u1", "session_id": "c1"}]
    assert result.trajectory[-1]["outcome"] == "evidence"
    assert result.answer_usage["model_calls"] == 2
    assert [t["name"] for t in model.requests[0]["tools"]] == ["knowledge_search", "order_data_query"]
    assert "[工具使用" in model.requests[0]["system"][0]["text"]


async def test_without_evidence_the_same_promise_is_still_replaced():
    tools = _Tools(order_data=UNAVAILABLE, order_success=False)
    model = ScriptedModel(
        tool_step(("call_1", "order_data_query", {"query": "SO-1 补件进度"})),
        text_step("SO-1 的补件会在3个工作日内送到。"),
    )
    req = make_request("我那单补件到哪了", IntentCategory.MISSING_PART, turn=_turn(tools))
    events = await _stream(_orchestrator((AgentType.ORDER, model)), req)

    assert "3个工作日" not in _deltas(events)
    result = events[-1]["result"]
    assert [v.rule for v in result.policy_violations] == ["lead_time_commitment"]
    assert result.trajectory[-1]["is_error"] is True


async def test_a_half_sentence_written_before_a_tool_call_is_dropped():
    model = ScriptedModel(
        tool_step(("call_1", "knowledge_search", {"query": "柜门铰链怎么调"}), text=["我帮您查一下。", "先看"]),
        text_step("按说明书调整铰链螺丝即可。"),
    )
    req = make_request("柜门关不严怎么办", IntentCategory.INSTALLATION, turn=_turn(_Tools()))
    events = await _stream(_orchestrator((AgentType.TECHNICAL, model)), req)

    # 写完的整句已经发出去了，照常算；没写完的"先看"作废，不和下一步黏在一起
    assert _deltas(events) == "我帮您查一下。按说明书调整铰链螺丝即可。"
    assert events[-1]["result"].response == _deltas(events)


async def test_stream_and_non_stream_give_the_same_answer():
    def script():
        return ScriptedModel(
            tool_step(("call_1", "knowledge_search", {"query": "柜门铰链怎么调"}), text=["我帮您查一下。", "先看"]),
            text_step("按说明书调整铰链螺丝。", "调好后再试。"),
        )

    def request():
        return make_request("柜门关不严怎么办", IntentCategory.INSTALLATION, turn=_turn(_Tools()))

    streamed = (await _stream(_orchestrator((AgentType.TECHNICAL, script())), request()))[-1]["result"]
    handled = await _orchestrator((AgentType.TECHNICAL, script())).run(request())
    assert streamed.response == handled.response == "我帮您查一下。按说明书调整铰链螺丝。调好后再试。"


async def test_each_specialist_only_sees_its_own_tools():
    for agent_type, intent, message, data, expected in [
        (AgentType.TECHNICAL, IntentCategory.INSTALLATION, "柜门关不严怎么办", True, ["knowledge_search"]),
        (AgentType.ORDER, IntentCategory.MISSING_PART, "补件到哪了", True,
         ["knowledge_search", "order_data_query"]),
        # 没接业务数据的实例（客户实例）：订单专家也只有知识库
        (AgentType.ORDER, IntentCategory.MISSING_PART, "补件到哪了", False, ["knowledge_search"]),
    ]:
        model = ScriptedModel(text_step("好的。"))
        await _orchestrator((agent_type, model)).run(
            make_request(message, intent, turn=_turn(_Tools(), data=data)))
        assert [t["name"] for t in model.requests[0]["tools"]] == expected, (agent_type, data)


async def test_quote_questions_cannot_query_business_data():
    tools = _Tools()
    model = ScriptedModel(
        tool_step(("call_1", "order_data_query", {"query": "加岛台多少钱"})),
        text_step("报价要以销售出具的正式报价单为准。"),
    )
    req = make_request("这套厨房加个岛台大概要加多少钱？", IntentCategory.QUOTE_PRICING,
                       turn=_turn(tools, intent="quote_pricing"))
    await _orchestrator((AgentType.BILLING, model)).run(req)

    assert tools.delegations() == []
    [result] = model.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True and "报价" in result["content"]


async def test_parallel_specialists_share_one_lookup():
    tools = _Tools()
    script = [tool_step(("call_1", "order_data_query", {"query": "SO-1 补件和尾款"})), text_step("已查到。")]
    orch = _orchestrator((AgentType.ORDER, ScriptedModel(*script)), (AgentType.BILLING, ScriptedModel(*script)))
    req = make_request("SO-1 补件和尾款", IntentCategory.MISSING_PART, turn=_turn(tools))
    decision = RoutingDecision(primary_agent=AgentType.ORDER, supporting_agents=[AgentType.BILLING],
                               confidence=0.9)
    await orch.run_parallel(req, decision)

    assert len(tools.delegations()) == 1
    assert sorted(r["dedup"] for r in req.turn.trajectory if r["actor"] == "model") == [False, True]


class _Intent:
    intent = IntentCategory.MISSING_PART
    intent_group = "missing_part"
    urgency = UrgencyLevel.LOW
    confidence = 0.9
    entities = {"order_id": ["SO-1"]}
    fast_path = True
    strategy = "ensemble"
    source_scores: dict = {}
    latency_ms = 1.0


async def test_the_trajectory_starts_with_what_the_rules_fetched():
    tools = _Tools()
    prefetch = Prefetch(tool_manager=tools, sql_agent=SQLAgentClient(base_url="http://x:8000"))

    async def load_memory(user_id, conv_id, query=""):
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    async def recognize(message, history=None):
        return _Intent()

    deps = TurnDeps(load_memory=load_memory, recognize_intent=recognize, outage_reason=lambda: "",
                    prefetch=prefetch, loop=LoopSetup(tools=loop_tools(prefetch), config=LoopConfig()))
    prepared = await prepare_turn("SO-1 补件到哪了", user_id="u1", conv_id="c1", request_id="r", deps=deps)

    # 模型拿客户原话再查一遍：直接复用预取的结果，不再委派一次
    model = ScriptedModel(
        tool_step(("call_1", "order_data_query", {"query": "SO-1 补件到哪了"})),
        text_step("SO-1 的补件已安排，3个工作日内送到。"),
    )
    result = await _orchestrator((AgentType.ORDER, model)).run(prepared.request)

    assert [(r["actor"], r["tool"], r["dedup"]) for r in result.trajectory] == [
        ("rule", "knowledge_search", False),
        ("rule", "order_data_query", False),
        ("model", "order_data_query", True),
    ]
    assert len(tools.delegations()) == 1
    assert result.answer_usage == {"model_calls": 2, "input_tokens": 50, "output_tokens": 20}


async def test_a_model_outage_mid_loop_is_not_a_handoff():
    model = ScriptedModel(
        tool_step(("call_1", "knowledge_search", {"query": "补件流程"})),
        fail_at=(2, LLMUnavailableError(REASON_CREDENTIAL, status_code=401, sent=True)),
    )
    req = make_request("补件到哪了", IntentCategory.MISSING_PART, turn=_turn(_Tools()))
    result = (await _stream(_orchestrator((AgentType.ORDER, model)), req))[-1]["result"]

    assert result.service_unavailable is True and result.unavailable_reason == REASON_CREDENTIAL
    assert result.handoff is False


async def test_a_gateway_without_tool_support_falls_back_to_one_plain_call():
    """网关明确拒绝 tools：这一轮退回单次生成，而不是报"没答完"。"""
    model = ScriptedModel(text_step("补件已登记。"), fail_at=(1, ToolsUnsupportedError("不支持工具调用")))
    req = make_request("补件到哪了", IntentCategory.MISSING_PART, turn=_turn(_Tools()))
    result = (await _stream(_orchestrator((AgentType.ORDER, model)), req))[-1]["result"]

    assert result.response == "补件已登记。" and result.service_unavailable is False
    assert "tools" in model.requests[0] and "tools" not in model.requests[1]


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_loop_metrics_keep_their_labels_bounded():
    """模型编出来的工具名记成 <unknown>，不让它把时序数撑开。"""
    unknown = ("agent_loop_tool_calls_total", {"tool": "<unknown>", "outcome": "unavailable_tool"})
    evidence = ("agent_loop_tool_calls_total", {"tool": "order_data_query", "outcome": "evidence"})
    loops = ("agent_loop_model_calls_count", {"agent": "order"})
    before = [_sample(name, **labels) for name, labels in (unknown, evidence, loops)]

    model = ScriptedModel(
        tool_step(("c1", "made_up_tool", {"query": "x"}), ("c2", "order_data_query", {"query": "SO-1 补件进度"})),
        text_step("已查到。"),
    )
    await _orchestrator((AgentType.ORDER, model)).run(
        make_request("补件到哪了", IntentCategory.MISSING_PART, turn=_turn(_Tools())))

    after = [_sample(name, **labels) for name, labels in (unknown, evidence, loops)]
    assert [a - b for a, b in zip(after, before)] == [1, 1, 1]
    assert REGISTRY.get_sample_value("agent_loop_tool_calls_total",
                                     {"tool": "made_up_tool", "outcome": "unavailable_tool"}) is None


# ── 接口层：/chat/stream 把工具调用发成进度，/chat 和流式结果都带轨迹 ────────────

class _Memory:
    cross_session = False

    async def get_context(self, user_id, conv_id, query=""):
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    async def add_message(self, user_id, conv_id, role, content):
        return None

    async def update_profile(self, user_id, conv_id):
        return None


def _desk(monkeypatch, model):
    """开着工具循环的 api/main：订单专家接脚本化的假模型，工具底下是记账的假 ToolManager。"""
    for name, value in {"ANTHROPIC_API_KEY": "test-key", "AI_HELPDESK_API_KEY": "",
                        "AI_HELPDESK_PROTECT_CHAT": "false", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                        "AI_HELPDESK_TOOL_LOOP": "on"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("APP_ENV", raising=False)
    sys.modules.pop("app.api.main", None)
    main = importlib.import_module("app.api.main")

    orch = _orchestrator((AgentType.ORDER, model))

    async def recognize(message, history=None):
        return _Intent()

    orch.recognize_intent = recognize
    tools = _Tools()
    for name, value in {
        "_orchestrator": orch, "_memory": _Memory(), "_tool_manager": tools,
        "_sql_agent": SQLAgentClient(base_url="http://x:8000"), "_ticket_store": None,
        "_ticket_notifier": None, "_conv_log": None, "_user_auth": None, "_rate_limiter": None,
    }.items():
        monkeypatch.setattr(main, name, value)
    return main, tools


async def _sse_events(response):
    events = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for frame in text.split("\n\n"):
            lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
            if "event" in lines:
                events.append((lines["event"], json.loads(lines["data"])))
    return events


def _script():
    return ScriptedModel(
        tool_step(("call_1", "order_data_query", {"query": "SO-1 补件和运费"})),
        text_step("SO-1 的补件已安排，3个工作日内送到。"),
    )


async def test_stream_reports_tool_calls_as_progress_and_returns_the_trajectory(monkeypatch):
    main, tools = _desk(monkeypatch, _script())
    response = await main.chat_stream(
        main.ChatRequest(message="SO-1 补件到哪了", user_id="u1", conv_id="c1"), None)
    events = await _sse_events(response)
    kinds = [kind for kind, _ in events]

    tool_frames = [(i, data) for i, (kind, data) in enumerate(events)
                   if kind == "progress" and data["stage"].startswith("helpdesk.tool_")]
    assert [data["stage"] for _, data in tool_frames] == ["helpdesk.tool_call", "helpdesk.tool_result"]
    (call_at, call), (_, done) = tool_frames
    assert (call["tool"], call["query"], call["agent"]) == ("order_data_query", "SO-1 补件和运费", "order")
    assert (done["call_id"], done["outcome"], done["evidence"]) == (call["call_id"], "evidence", True)
    assert "trace" not in done                       # 完整的那一条进 result，进度里只放结局
    # 工具进度夹在路由和结果之间：专家已经选定、回答还没结束
    assert kinds.index("routing") < call_at < kinds.index("result")

    result = events[-1][1]
    assert [(r["actor"], r["tool"]) for r in result["trajectory"]] == [
        ("rule", "knowledge_search"), ("rule", "order_data_query"), ("model", "order_data_query")]
    assert result["answer_usage"] == {"model_calls": 2, "input_tokens": 50, "output_tokens": 20}
    assert len(tools.delegations()) == 2             # 预取一次，模型换了个问法又查一次


async def test_the_plain_response_and_health_report_the_loop_too(monkeypatch):
    main, _ = _desk(monkeypatch, _script())
    answer = await main.chat(main.ChatRequest(message="SO-1 补件到哪了", user_id="u1", conv_id="c1"), None)

    assert [r["actor"] for r in answer.trajectory] == ["rule", "rule", "model"]
    assert answer.answer_usage["model_calls"] == 2
    assert (await main.health())["tool_loop"] == {"enabled": True, "tools_rejected": False}
