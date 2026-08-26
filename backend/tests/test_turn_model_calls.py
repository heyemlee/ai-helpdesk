"""
一轮对话的事件：工具循环和模型调用这一层。

- guard 层（GuardedLLMClient）：create、parse、stream 成功时记用量；被保护挡下记 blocked、
  没出网；出错记原因；流式调用方没取最终消息时用量为 null；没打标签的记 unknown；
- 工具循环：带去重、拒绝、跳过、预算耗尽、编出来的工具的轮次上，`tool_call` 投影出来的
  就是响应的 trajectory，`model_call` 投影出来的就是 answer_usage；ToolStart 带去向；
- 一轮 /chat 里所有用途的模型调用都打了标签：意图、改写、重排、作答、摘要、画像，没有 unknown。

全部离线：模型是 conftest 的脚本化假模型，或者按请求要什么就回什么的假 SDK 客户端。
"""
import asyncio
import importlib
import json
import sys

import pytest
from conftest import Block, FakeRedis, ScriptedModel, text_step, tool_step
from prometheus_client import REGISTRY

import app.memory.conversation_memory as memory_module
from app.agents.agent_orchestrator import AgentOrchestrator
from app.core import llm_utils
from app.core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from app.core.llm_guard import REASON_QUOTA, GuardedLLMClient, LLMGuard, LLMUnavailableError
from app.events import EventLog, EventLogConfig, bind
from app.events.projection import mismatches, project_turn
from app.events.recorder import EventLogStats, TurnRecorder, model_call_tag, new_turn_id
from app.events.store import MemoryEventStore
from app.memory.conversation_memory import MemoryContext, MemoryManager
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import Tool, ToolManager, ToolResult

KNOWLEDGE = [{"title": "补件流程", "content": "缺件补发在核实后安排。", "score": 0.6,
              "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}]
EVIDENCE = {"answer": "SO-1 的补件已排产，三个工作日内发出。", "queried_database": True,
            "execution_status": "success", "has_risk": False, "degraded": False,
            "selected_databases": ["wms_db"], "trace_id": "t-1", "steps": [{"database": "wms_db", "success": True}]}
UNAVAILABLE = {"answer": "", "queried_database": False, "unavailable": True, "error": "ReadTimeout"}


def _turn(mode: str = "full"):
    store = MemoryEventStore("staff")
    return TurnRecorder(new_turn_id(), instance="staff", conv_id="c1", mode=mode, sink=store,
                        stats=EventLogStats()), store


async def _model_calls(store, turn) -> list[dict]:
    return [e.to_dict() for e in await store.events_of(turn.turn_id) if e.type == "model_call"]


def _guarded(inner) -> GuardedLLMClient:
    return GuardedLLMClient(inner, LLMGuard("anthropic @ test"))


# ── guard 层 ──────────────────────────────────────────────────────────────────

class _Parsed:
    def __init__(self, **fields):
        self.fields = fields

    def model_dump(self):
        return dict(self.fields)


class _Inner:
    """假 SDK 客户端：create / parse / stream 都回一段固定的东西，或者按设定抛错。"""

    def __init__(self, fail: Exception | None = None):
        self.messages = self
        self.fail = fail

    async def create(self, **kwargs):
        if self.fail:
            raise self.fail
        return Block(content=[Block(type="text", text="好的。"),
                              Block(type="tool_use", id="call_1", name="knowledge_search", input={"query": "q"})],
                     stop_reason="tool_use",
                     usage=Block(input_tokens=120, output_tokens=9, cache_read_input_tokens=64,
                                 cache_creation_input_tokens=0))

    async def parse(self, **kwargs):
        return Block(parsed_output=_Parsed(intent="installation", confidence=0.9), content=[],
                     stop_reason="end_turn", usage=Block(input_tokens=50, output_tokens=7))

    def stream(self, **kwargs):
        return ScriptedModel(text_step("第一段", "第二段。", usage=(30, 4))).stream(**kwargs)


async def test_a_create_call_records_usage_purpose_and_raw_output():
    turn, store = _turn()
    client = _guarded(_Inner())
    with bind(turn), model_call_tag(purpose="answer", agent="technical_0", step=1):
        await client.messages.create(model="m-1", max_tokens=10, messages=[], tool_choice={"type": "auto"})

    [call] = await _model_calls(store, turn)
    assert (call["agent"], call["step"]) == ("technical_0", 1)
    data = call["data"]
    assert {k: data[k] for k in ("purpose", "provider", "model", "stream", "status", "sent", "reason",
                                 "stop_reason", "tool_choice", "tool_uses", "text_chars")} == {
        "purpose": "answer", "provider": "anthropic", "model": "m-1", "stream": False, "status": "ok",
        "sent": True, "reason": "", "stop_reason": "tool_use", "tool_choice": "auto", "tool_uses": 1,
        "text_chars": 3}
    assert data["usage"] == {"input_tokens": 120, "output_tokens": 9, "cache_read_tokens": 64,
                             "cache_write_tokens": 0}
    assert call["body"]["output"][1] == {"type": "tool_use", "id": "call_1", "name": "knowledge_search",
                                         "input": {"query": "q"}}
    assert turn.totals()["model_calls"] == 1 and turn.totals()["input_tokens"] == 120


async def test_a_parse_call_records_the_parsed_json_and_an_untagged_call_is_unknown():
    turn, store = _turn()
    with bind(turn):
        await _guarded(_Inner()).messages.parse(model="m-1", max_tokens=10, messages=[], output_format=object)

    [call] = await _model_calls(store, turn)
    assert call["data"]["purpose"] == "unknown"
    assert call["body"]["output"] == {"intent": "installation", "confidence": 0.9}


async def test_a_stream_records_usage_and_first_token_from_the_final_message():
    turn, store = _turn()
    with bind(turn), model_call_tag(purpose="answer", agent="order_0", step=2):
        async with _guarded(_Inner()).messages.stream(model="m-1", max_tokens=10, messages=[]) as stream:
            text = "".join([t async for t in stream.text_stream])
            await stream.get_final_message()

    [call] = await _model_calls(store, turn)
    assert text == "第一段第二段。"
    assert (call["data"]["stream"], call["data"]["status"], call["step"]) == (True, "ok", 2)
    assert call["data"]["usage"]["input_tokens"] == 30 and call["data"]["first_token_ms"] is not None
    assert call["body"]["output"] == [{"type": "text", "text": "第一段第二段。"}]


async def test_a_real_openai_compatible_event_stream_streams_and_is_recorded_through_the_guard():
    """
    生产走的是 OpenAI 兼容的中转站：真的 openai SDK 读一段 SSE（假传输层），外面套着
    GuardedLLMClient 的记录包装。字照常流出来，最终消息的用量、首字耗时照常记下。
    """
    import httpx

    from app.core.openai_compat import AsyncOpenAICompat

    def chunk(choices, usage=None):
        payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": choices}
        if usage:
            payload["usage"] = usage
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    body = (chunk([{"index": 0, "delta": {"content": "补件"}, "finish_reason": None}])
            + chunk([{"index": 0, "delta": {"content": "已登记。"}, "finish_reason": None}])
            + chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}])
            + chunk([], usage={"prompt_tokens": 41, "completion_tokens": 6, "total_tokens": 47})
            + "data: [DONE]\n\n")

    def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    inner = AsyncOpenAICompat(api_key="sk-test", base_url="https://relay.example/v1", max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    client = GuardedLLMClient(inner, LLMGuard("openai @ test"))
    turn, store = _turn()
    with bind(turn), model_call_tag(purpose="answer", agent="order_0", step=1):
        async with client.messages.stream(model="m", max_tokens=64,
                                          messages=[{"role": "user", "content": "补件"}]) as stream:
            text = "".join([t async for t in stream.text_stream])
            final = await stream.get_final_message()

    assert text == "补件已登记。" and final.usage.input_tokens == 41
    [call] = await _model_calls(store, turn)
    assert (call["data"]["status"], call["data"]["stream"], call["data"]["stop_reason"]) == ("ok", True, "end_turn")
    assert (call["data"]["usage"]["input_tokens"], call["data"]["usage"]["output_tokens"]) == (41, 6)
    assert call["data"]["first_token_ms"] is not None
    assert call["body"]["output"] == [{"type": "text", "text": "补件已登记。"}]


async def test_a_stream_whose_caller_never_fetched_the_final_message_has_no_usage():
    turn, store = _turn()
    with bind(turn), model_call_tag(purpose="answer"):
        with pytest.raises(RuntimeError):
            async with _guarded(_Inner()).messages.stream(model="m-1", max_tokens=10, messages=[]) as stream:
                async for _ in stream.text_stream:
                    raise RuntimeError("调用方中途出错")

    [call] = await _model_calls(store, turn)
    assert (call["data"]["status"], call["data"]["usage"], call["data"]["error_type"]) == (
        "error", None, "RuntimeError")


async def test_a_blocked_call_is_recorded_without_going_out():
    guard = LLMGuard("anthropic @ test")
    guard._reason = REASON_QUOTA                      # 保护已经在挡：这次根本不该出网
    inner = ScriptedModel()                           # 一步脚本都没有：真发出去就会报错
    turn, store = _turn()
    with bind(turn), model_call_tag(purpose="intent"):
        with pytest.raises(LLMUnavailableError):
            await GuardedLLMClient(inner, guard).messages.create(model="m-1", max_tokens=10, messages=[])

    [call] = await _model_calls(store, turn)
    assert {k: call["data"][k] for k in ("purpose", "status", "sent", "reason", "usage")} == {
        "purpose": "intent", "status": "blocked", "sent": False, "reason": REASON_QUOTA, "usage": None}
    assert inner.requests == []


async def test_a_failed_call_records_the_error_type_and_keeps_the_message_in_the_body():
    turn, store = _turn()
    with bind(turn), model_call_tag(purpose="rerank", round=2):
        with pytest.raises(TimeoutError):
            await _guarded(_Inner(fail=TimeoutError("上游超时"))).messages.create(model="m-1", messages=[])

    [call] = await _model_calls(store, turn)
    assert (call["data"]["status"], call["data"]["error_type"], call["data"]["round"]) == ("error", "TimeoutError", 2)
    assert call["body"]["error"] == "上游超时"


async def test_nothing_is_recorded_with_the_log_off():
    turn, store = _turn(mode="off")
    with bind(turn):
        await _guarded(_Inner()).messages.create(model="m-1", messages=[])
    assert await store.events_of(turn.turn_id) == []


# ── 工具循环 ──────────────────────────────────────────────────────────────────

class _Intent:
    def __init__(self, intent: IntentCategory = IntentCategory.MISSING_PART, entities: dict | None = None):
        self.intent = intent
        self.intent_group = intent.value
        self.urgency = UrgencyLevel.LOW
        self.confidence = 0.9
        self.entities = {"order_id": ["SO-1"]} if entities is None else entities
        self.fast_path = True
        self.strategy = "fast_path"
        self.source_scores = {"rules": 0.9}
        self.latency_ms = 1.0


class _Memory:
    cross_session = False

    async def get_context(self, user_id, conv_id, query=""):
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    async def add_message(self, user_id, conv_id, role, content):
        return None

    async def update_profile(self, user_id, conv_id):
        return None


class _Tools:
    """知识库给一段片段；业务数据第一次（规则预取）按 first 回，之后按 then 回。"""

    def __init__(self, first=EVIDENCE, then=EVIDENCE):
        self.first, self.then = first, then
        self.queries: list[str] = []

    async def call(self, name, params, context=None, **_):
        if name == "knowledge_search":
            return ToolResult(success=True, data=[dict(k) for k in KNOWLEDGE], tool_name=name)
        self.queries.append(params["query"])
        data = self.first if len(self.queries) == 1 else self.then
        return ToolResult(success=not data.get("unavailable"), data=dict(data), tool_name=name)


MESSAGE = "SO-1 补件到哪了"


async def _loop_desk(monkeypatch, model, *, intent=None, tools=None):
    for name, value in {"ANTHROPIC_API_KEY": "test-key", "AI_HELPDESK_API_KEY": "",
                        "AI_HELPDESK_PROTECT_CHAT": "false", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                        "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_EVENT_LOG": "full",
                        "AI_HELPDESK_TOOL_LOOP": "on"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("APP_ENV", raising=False)
    sys.modules.pop("app.api.main", None)
    main = importlib.import_module("app.api.main")

    orch = AgentOrchestrator(api_key="test-key")
    client = _guarded(model)
    for agents in orch._pool.values():
        for agent in agents:
            agent._client = client

    async def recognize(message, history=None):
        return intent or _Intent()

    orch.recognize_intent = recognize
    log = await EventLog.open(EventLogConfig.from_env())
    for name, value in {
        "_orchestrator": orch, "_memory": _Memory(), "_tool_manager": tools or _Tools(),
        "_sql_agent": SQLAgentClient(base_url="http://x:8000"), "_ticket_store": None,
        "_ticket_notifier": None, "_conv_log": None, "_user_auth": None, "_rate_limiter": None,
        "_event_log": log,
    }.items():
        monkeypatch.setattr(main, name, value)
    return main, log


def _mismatch_count() -> float:
    return REGISTRY.get_sample_value("turn_projection_mismatch_total") or 0.0


async def _loop_chat(main, log):
    before = _mismatch_count()
    answer = await main.chat(main.ChatRequest(message=MESSAGE, user_id="u1", conv_id="c1"), None)
    recorded = [e.to_dict() for e in await log.store.events_of(answer.turn_id)]
    assert _mismatch_count() == before, "影子校验报了不一致"
    return answer.model_dump(), recorded


def _assert_trajectory_and_usage_project(recorded, answer):
    projected = project_turn(recorded)["result"]
    assert projected["trajectory"] == answer["trajectory"]
    assert projected["answer_usage"] == answer["answer_usage"]
    assert mismatches(projected, answer) == []


def _decisions(recorded) -> list[str]:
    return [e["data"]["decision"] for e in recorded if e["type"] == "tool_started" and e["data"]["actor"] == "model"]


async def test_a_loop_turn_records_each_step_and_projects_to_the_response(monkeypatch):
    """
    规则预取查过同一句话（复用），模型换个说法再查业务数据（执行），然后作答。调工具前写的
    半句（没有句末标点，守卫还压着没发）作废，记一条 preamble_discarded。
    """
    model = ScriptedModel(
        tool_step(("call_k", "knowledge_search", {"query": MESSAGE}),
                  ("call_o", "order_data_query", {"query": "SO-1 补件发货日期"}),
                  text=("我先查一下",)),
        text_step("已经查到 SO-1 补件的安排。"),
    )
    main, log = await _loop_desk(monkeypatch, model)
    answer, recorded = await _loop_chat(main, log)

    kinds = [e["type"] for e in recorded]
    start = kinds.index("agent_started")
    assert kinds[start:] == ["agent_started", "model_call", "preamble_discarded", "tool_started", "tool_started",
                             "tool_call", "tool_call", "model_call", "loop_ended", "agent_answered", "turn_completed"]
    discarded = next(e for e in recorded if e["type"] == "preamble_discarded")
    assert (discarded["data"], discarded["body"], discarded["step"]) == ({"chars": 5}, {"text": "我先查一下"}, 1)
    assert _decisions(recorded) == ["dedup", "execute"]
    steps = [e["step"] for e in recorded if e["type"] == "model_call"]
    assert steps == [1, 2] and {e["data"]["purpose"] for e in recorded if e["type"] == "model_call"} == {"answer"}
    started = next(e for e in recorded if e["type"] == "agent_started")["data"]
    assert started["mode"] == "loop" and started["tools"] == ["knowledge_search", "order_data_query"]
    assert started["deadline_s"] > 0 and len(started["tools_sha256"]) == 64
    ended = next(e for e in recorded if e["type"] == "loop_ended")["data"]
    assert ended == {"model_calls": 2, "tool_calls": 1, "stop_reason": "end_turn", "forced": ""}
    order_call = next(e for e in recorded if e["type"] == "tool_call" and e["call_id"] == "call_o")
    assert order_call["body"]["raw"]["answer"] == EVIDENCE["answer"]
    assert order_call["data"]["idem_key"] == f"{answer['turn_id']}:call_o"
    completed = recorded[-1]["data"]["totals"]
    assert completed["model_calls"] == 2 and completed["tool_calls"] == 4      # 两条规则预取 + 两条模型的
    _assert_trajectory_and_usage_project(recorded, answer)


@pytest.mark.parametrize("intent, tools, calls, decisions", [
    # 报价不走系统查询：策略门拒绝
    (_Intent(IntentCategory.QUOTE_PRICING, entities={}), None,
     [("c1", "order_data_query", {"query": "这套柜子多少钱"})], ["refused"]),
    # 规则预取那次委派刚超时：换个说法也不再查
    (None, _Tools(first=UNAVAILABLE), [("c1", "order_data_query", {"query": "SO-1 补件的发货日期"})], ["skipped"]),
    # 一轮最多委派一次：同一步里第二次被预算拒了
    (None, None, [("c1", "order_data_query", {"query": "SO-1 发货日期"}),
                  ("c2", "order_data_query", {"query": "SO-1 运费"})], ["execute", "budget"]),
    # 模型编了一个工具、参数不对
    (None, None, [("c1", "made_up_tool", {"query": "x"}),
                  ("c2", "knowledge_search", {"top_k": 9})], ["unavailable_tool", "invalid_args"]),
])
async def test_every_decision_lands_in_the_log_and_projects_back(monkeypatch, intent, tools, calls, decisions):
    model = ScriptedModel(tool_step(*calls), text_step("根据已有资料作答。"))
    main, log = await _loop_desk(monkeypatch, model, intent=intent, tools=tools)
    answer, recorded = await _loop_chat(main, log)

    assert _decisions(recorded) == decisions
    _assert_trajectory_and_usage_project(recorded, answer)


async def test_a_forced_answer_and_the_stream_project_too(monkeypatch):
    """每一步都调工具，直到模型调用次数用完被要求直接作答；流式和非流式都投影得回去。"""
    def script():
        return ScriptedModel(
            tool_step(("c1", "knowledge_search", {"query": "补件流程 一"})),
            tool_step(("c2", "knowledge_search", {"query": "补件流程 二"})),
            text_step("只能根据已有资料回答。"),
        )

    main, log = await _loop_desk(monkeypatch, script())
    answer, recorded = await _loop_chat(main, log)
    assert answer["answer_usage"]["forced"] == "model_calls"
    assert next(e for e in recorded if e["type"] == "loop_ended")["data"]["forced"] == "model_calls"
    _assert_trajectory_and_usage_project(recorded, answer)

    main, log = await _loop_desk(monkeypatch, script())
    response = await main.chat_stream(main.ChatRequest(message=MESSAGE, user_id="u1", conv_id="c1"), None)
    frames = {}
    async for chunk in response.body_iterator:
        for frame in chunk.split("\n\n"):
            lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
            if lines.get("event") in ("meta", "routing", "result"):
                frames[lines["event"]] = json.loads(lines["data"])
    streamed = [e.to_dict() for e in await log.store.events_of(frames["result"]["turn_id"])]
    projected = project_turn(streamed)
    assert projected["result"]["trajectory"] == frames["result"]["trajectory"]
    assert projected["result"]["answer_usage"] == frames["result"]["answer_usage"]
    assert mismatches(projected["meta"], frames["meta"]) == []


# ── 一轮 /chat 里的每次模型调用都有用途 ──────────────────────────────────────

_PAYLOADS = {
    "LLMIntentPayload": {"intent": "installation", "confidence": 0.93, "reasoning": "柜门调整"},
    "QueryRewritePayload": {"queries": ["柜门铰链怎么调", "柜门关不严"]},
    "RerankPayload": {"order": [0, 1, 2], "sufficient": True, "missing": ""},
    "UserProfilePayload": {"preferences": ["白色门板"], "products": [], "issue_types": ["安装"]},
}


class _Answers:
    """假 SDK 客户端：结构化调用按 output_format 回一个合法实例，摘要和作答回一句话。"""

    def __init__(self):
        self.messages = self

    async def parse(self, *, output_format, **kwargs):
        parsed = output_format.model_validate(_PAYLOADS[output_format.__name__])
        return Block(parsed_output=parsed, content=[], stop_reason="end_turn",
                     usage=Block(input_tokens=40, output_tokens=6))

    async def create(self, **kwargs):
        return Block(content=[Block(type="text", text="客户反映柜门关不严，已给出铰链调整步骤。")],
                     stop_reason="end_turn", usage=Block(input_tokens=30, output_tokens=12))

    def stream(self, **kwargs):
        return ScriptedModel(text_step("把铰链上的两颗螺丝各拧半圈试试。")).stream(**kwargs)


class _Store:
    async def episodic_add(self, *args, **kwargs):
        return None

    async def episodic_search(self, *args, **kwargs):
        return []

    async def profile_get(self, *args, **kwargs):
        return {}

    async def profile_set(self, *args, **kwargs):
        return None


class _Embedder:
    async def embed(self, text):
        return [0.0] * 8


async def test_every_model_call_in_a_chat_turn_says_what_it_was_for(monkeypatch):
    """
    走规则通道之外的那条路：意图问模型、检索改写加重排、专家作答；工作记忆满了要摘要，
    轮次之后还有一次画像。六种用途都要标上，一次 unknown 都不许有。
    """
    for name, value in {"ANTHROPIC_API_KEY": "test-key", "AI_HELPDESK_API_KEY": "",
                        "AI_HELPDESK_PROTECT_CHAT": "false", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                        "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_EVENT_LOG": "full"}.items():
        monkeypatch.setenv(name, value)
    for name in ("APP_ENV", "AI_HELPDESK_TOOL_LOOP"):
        monkeypatch.delenv(name, raising=False)
    # test_memory_compression_race 把这两个模块属性换成桩且不还原：这里要走真的那一份
    monkeypatch.setattr(memory_module, "structured_create", llm_utils.structured_create)
    monkeypatch.setattr(memory_module, "structured_parse", llm_utils.structured_parse)
    sys.modules.pop("app.api.main", None)
    main = importlib.import_module("app.api.main")

    guard = LLMGuard("anthropic @ test")
    recognizer = IntentRecognizer(api_key="test-key")
    recognizer.client = GuardedLLMClient(_Answers(), guard)
    tools = ToolManager(api_key="test-key")
    tools._client = GuardedLLMClient(_Answers(), guard)

    async def search(params, context):
        return [{"title": f"安装手册 {i}", "content": f"铰链调节说明 第 {i} 段", "score": 0.2} for i in range(6)]

    tools.register(Tool(name="knowledge_search", description="stub", handler=search,
                        schema={"type": "object", "properties": {"query": {"type": "string"}},
                                "required": ["query"]}, supports_rerank=True))
    memory = MemoryManager(store=_Store(), embedder=_Embedder(), api_key="test-key", cross_session=True)
    memory._redis = FakeRedis()
    memory._client = GuardedLLMClient(_Answers(), guard)
    for i in range(MemoryManager.COMPRESS_AT - 1):          # 再进两条就到压缩线
        await memory._redis.lpush(memory._wm_key("u1", "c1"), json.dumps({
            "role": "user" if i % 2 == 0 else "assistant", "content": f"前面第 {i} 句",
            "ts": "2026-10-16T09:00:00", "metadata": {}}))
    orch = AgentOrchestrator(api_key="test-key", recognizer=recognizer)
    for agents in orch._pool.values():
        for agent in agents:
            agent._client = GuardedLLMClient(_Answers(), guard)
    log = await EventLog.open(EventLogConfig.from_env())
    for name, value in {
        "_orchestrator": orch, "_memory": memory, "_tool_manager": tools, "_sql_agent": None,
        "_ticket_store": None, "_ticket_notifier": None, "_conv_log": None, "_user_auth": None,
        "_rate_limiter": None, "_event_log": log,
    }.items():
        monkeypatch.setattr(main, name, value)

    answer = await main.chat(main.ChatRequest(
        message="柜门装上去之后总是自己弹开一条缝，调了好几次都不行", user_id="u1", conv_id="c1"), None)
    await asyncio.gather(*list(main._background_tasks))       # 画像是轮次之后的后台任务

    recorded = [e.to_dict() for e in await log.store.events_of(answer.turn_id)]
    purposes = [e["data"]["purpose"] for e in recorded if e["type"] == "model_call"]
    assert "unknown" not in purposes
    assert set(purposes) == {"intent", "rewrite", "rerank", "answer", "summary", "profile"}
    kinds = [e["type"] for e in recorded]
    # 画像排在终结事件后面；摘要在 add_message 里同步跑，排在前面
    profile_at = next(i for i, e in enumerate(recorded) if e["type"] == "model_call"
                      and e["data"]["purpose"] == "profile")
    summary_at = next(i for i, e in enumerate(recorded) if e["type"] == "model_call"
                      and e["data"]["purpose"] == "summary")
    assert summary_at < kinds.index("turn_completed") < profile_at
    totals = recorded[kinds.index("turn_completed")]["data"]["totals"]
    assert totals["model_calls"] == len(purposes) - 1               # 画像那一次在结算之后
