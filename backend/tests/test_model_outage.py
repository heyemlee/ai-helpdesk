"""
模型服务不可用时，整条链路怎么表现。

起因是中继停用 Key 之后的那一晚：一条提问打出去 6～8 次请求，全部
401；每个调用点都在自己的 except 里"成功降级"；没答上来的问题被当成"需要人工"
各开一张工单；摘要失败把客户的原话换成了一句占位。

这一组全部离线：模型客户端接在 conftest 的 FakeRelay 上（SDK 的传输层，不开
套接字），存储用 FakeRedis 和临时目录里的 SQLite。`relay.sent()` 数的是真的
发出去了的请求，所以"不再出网"是数出来的，不是推断出来的。
"""
import asyncio
import importlib
import json
import logging
import os
import pathlib
import re
import sys
from types import SimpleNamespace

import pytest
from conftest import FakeRedis, make_request, stub_agents, stub_intent

import app.memory.conversation_memory as memory_module
import app.tools.tool_manager as tool_module
from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core import llm_guard, llm_utils
from app.core.intent_recognizer import IntentCategory, IntentRecognizer
from app.core.llm_guard import (
    REASON_CREDENTIAL,
    REASON_MODEL_ERROR,
    LLMUnavailableError,
)
from app.evaluation.evaluator import (
    EndToEndEvaluator,
    IntentEvaluator,
    IntentTestCase,
    LLMJudge,
    QualityScores,
)
from app.memory.conversation_memory import MemoryManager, MsgRole
from app.tickets.ticket_store import TicketStore
from app.tools.tool_manager import (
    QueryRewritePayload,
    RerankPayload,
    Tool,
    ToolManager,
)

URL = "https://relay.example"
KEY = "sk-outage-0123456789abcdef"

# 不命中规则通道的问法：意图那一路必须真的去问模型
QUESTION = "柜门装上去之后总是自己弹开一条缝，调了好几次都不行"
OTHER_QUESTIONS = [
    "台面和墙之间留的那道缝一般怎么收口比较好",
    "吊柜下面想加一排灯带，线要从哪里走",
    "抽屉拉出来感觉有点晃，是滑轨的问题吗",
    "水槽柜底板受潮鼓起来了应该怎么处理",
]


# ── 夹具 ──────────────────────────────────────────────────────────────────────

class _Store:
    """情景记忆 + 画像的假存储。"""

    def __init__(self):
        self.docs, self.profiles = [], []

    async def episodic_add(self, *args, **kwargs):
        self.docs.append(args)

    async def episodic_search(self, *args, **kwargs):
        return []

    async def profile_get(self, *args, **kwargs):
        return {}

    async def profile_set(self, *args, **kwargs):
        self.profiles.append(args)


class _Embedder:
    async def embed(self, text):
        return [0.0] * 8


def _knowledge_tool(queries: list[str]) -> Tool:
    """
    每个子查询召回 6 段（多于 top_k，所以会走到重排），分数都很低——
    按分数阈值看是"证据不足"，以前模型调用失败时正是退回这个阈值去补检的。
    """
    async def _handler(params, context):
        queries.append(params["query"])
        return [{"title": f"安装手册 {i}", "content": f"铰链调节说明 第 {i} 段", "score": 0.2}
                for i in range(6)]

    return Tool(name="knowledge_search", description="stub", handler=_handler,
                schema={"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]},
                supports_rerank=True)


@pytest.fixture
def stack(relay, monkeypatch):
    """同一份模型配置（地址 + 凭证）上的全套组件：意图、检索、编排、记忆、判分。"""
    # test_memory_compression_race 会把这个模块属性换成桩且不还原；这里要走真的
    # structured_create → client.messages.create，才测得到保护
    monkeypatch.setattr(memory_module, "structured_create", llm_utils.structured_create)

    recognizer = IntentRecognizer(api_key=KEY, base_url=URL)
    searched: list[str] = []
    tools = ToolManager(api_key=KEY, base_url=URL)
    tools.register(_knowledge_tool(searched))
    store = _Store()
    memory = MemoryManager(store=store, embedder=_Embedder(), api_key=KEY, base_url=URL)
    memory._redis = FakeRedis()
    orchestrator = AgentOrchestrator(api_key=KEY, base_url=URL, recognizer=recognizer)
    return SimpleNamespace(
        relay=relay, recognizer=recognizer, tools=tools, memory=memory, store=store,
        orchestrator=orchestrator, searched=searched,
        guard=llm_guard.guard_for("anthropic", URL, KEY),
    )


async def _seed_history(memory: MemoryManager, n: int, conv_id: str = "c1") -> list[str]:
    """直接写进列表，不走 add_message——那条路会自己触发压缩。返回按时间顺序的原话。"""
    said = [f"客户说的第 {i} 句：订单 SO-{1000 + i} 的门板颜色要改" for i in range(n)]
    for i, text in enumerate(said):
        await memory._redis.lpush(memory._wm_key("u1", conv_id), json.dumps({
            "role": "user" if i % 2 == 0 else "assistant", "content": text,
            "ts": "2026-10-04T20:00:00", "metadata": {},
        }))
    return said


async def _history(memory: MemoryManager, conv_id: str = "c1") -> list[str]:
    return [m.content for m in await memory._get_working_memory("u1", conv_id)]


# ── 首次拒绝之后，后续环节不再出网 ────────────────────────────────────────────

async def test_after_the_first_rejection_no_later_stage_reaches_the_network(stack):
    """
    意图识别那一次被 401 拒绝之后，改写、重排、专家、兜底、摘要、画像、判分
    都照常被调用，但没有一个再把请求发出去。
    """
    relay, guard = stack.relay, stack.guard
    relay.reject(KEY)

    intent = await stack.recognizer.recognize(QUESTION)
    assert relay.sent() == 1                              # 第一次：真的发了，被拒
    assert intent.llm_failed and intent.llm_unavailable == REASON_CREDENTIAL

    req = make_request(QUESTION, IntentCategory.INSTALLATION)
    candidates = [{"title": str(i), "content": f"第 {i} 段", "score": 0.2} for i in range(6)]
    await _seed_history(stack.memory, MemoryManager.COMPRESS_AT)
    technical = stack.orchestrator._best_agent(AgentType.TECHNICAL)
    judge = LLMJudge(llm_utils.create_llm_client(KEY, URL), "claude-test")

    async def stream_specialist():
        return [event async for event in technical.stream(req)]

    stages = {
        "意图识别（另一句话）": lambda: stack.recognizer.recognize(OTHER_QUESTIONS[0]),
        "查询改写": lambda: stack.tools.rewrite_query(QUESTION),
        "重排": lambda: stack.tools._llm_rerank(QUESTION, candidates, 3),
        "检索整链路": lambda: stack.tools.search_with_rewrite("knowledge_search", QUESTION, top_k=3),
        "专家 Agent": lambda: stack.orchestrator._execute(req, AgentType.TECHNICAL),
        "专家 Agent（流式）": stream_specialist,
        "通用 Agent 兜底": lambda: stack.orchestrator._execute(req, AgentType.GENERAL),
        "会话摘要": lambda: stack.memory._compress("u1", "c1"),
        "用户画像": lambda: stack.memory.update_profile("u1", "c1"),
        "评测判分": lambda: judge.judge(QUESTION, "回答"),
    }
    for name, run in stages.items():
        blocked_before = guard.blocked_calls
        await run()
        assert relay.sent() == 1, f"{name} 仍然把请求发出去了"
        # 这个环节确实走到了模型调用那一步并被挡下——否则上一行什么也没证明
        assert guard.blocked_calls > blocked_before, f"{name} 没有走到模型调用"

    assert stack.store.profiles == []                     # 画像没有被写成别的东西


@pytest.mark.parametrize("first_stage", ["rewrite", "specialist", "summary", "profile"])
async def test_whichever_stage_is_rejected_first_the_rest_stay_home(stack, first_stage):
    """先撞上的不一定是意图识别（规则通道命中、缓存命中时它根本不调模型）。"""
    relay = stack.relay
    relay.reject(KEY)
    req = make_request(QUESTION, IntentCategory.INSTALLATION)
    await _seed_history(stack.memory, MemoryManager.COMPRESS_AT)

    first = {
        "rewrite": lambda: stack.tools.rewrite_query(QUESTION),
        "specialist": lambda: stack.orchestrator._execute(req, AgentType.TECHNICAL),
        "summary": lambda: stack.memory._compress("u1", "c1"),
        "profile": lambda: stack.memory.update_profile("u1", "c1"),
    }[first_stage]
    await first()
    assert relay.sent() == 1

    assert (await stack.recognizer.recognize(QUESTION)).llm_unavailable == REASON_CREDENTIAL
    await stack.tools.search_with_rewrite("knowledge_search", QUESTION, top_k=3)
    result = await stack.orchestrator.run(req)
    assert result.service_unavailable and result.unavailable_reason == REASON_CREDENTIAL
    assert relay.sent() == 1


# ── 检索：调用失败不是"证据不足" ──────────────────────────────────────────────

def _scripted_retrieval(monkeypatch, *, rewrite, rerank):
    """
    把检索链路里的两种模型调用换成脚本：每次调用按顺序取一项，
    是异常就抛，否则当作模型的返回。返回实际调用记录。
    """
    scripts = {QueryRewritePayload: list(rewrite), RerankPayload: list(rerank)}
    calls: list[str] = []

    async def _parse(client, *, output_format, **kwargs):
        calls.append("rewrite" if output_format is QueryRewritePayload else "rerank")
        step = scripts[output_format].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(tool_module, "structured_parse", _parse)
    return calls


REWRITTEN = QueryRewritePayload(queries=["铰链怎么调", "门缝标准是多少"])
INSUFFICIENT = RerankPayload(order=[0, 1, 2], sufficient=False, missing="门缝标准")
REJECTED = LLMUnavailableError(REASON_CREDENTIAL, status_code=401, sent=True)


@pytest.mark.parametrize("rewrite, rerank, expected_calls, searches", [
    # 改写没调成：只有原始查询；重排照常，但它的结论不拿来触发补检
    ([REJECTED], [INSUFFICIENT], ["rewrite", "rerank"], 1),
    # 重排没调成：没有任何关于证据够不够的判断
    ([REWRITTEN], [TimeoutError("timed out")], ["rewrite", "rerank"], 3),
    # 两次都没调成
    ([TimeoutError("timed out")], [TimeoutError("timed out")], ["rewrite", "rerank"], 1),
])
async def test_a_failed_model_call_is_not_treated_as_insufficient_evidence(
        monkeypatch, rewrite, rerank, expected_calls, searches):
    """
    以前这里会退回分数阈值、判成"证据不足"，再跑一轮改写 + 重排——同一个故障
    打两遍，回来还对外报一句证据不足。现在如实报"没判"，不补检。
    """
    calls = _scripted_retrieval(monkeypatch, rewrite=rewrite, rerank=rerank)
    queries: list[str] = []
    manager = ToolManager(api_key="test-key")
    manager.register(_knowledge_tool(queries))

    result = await manager.search_with_rewrite("knowledge_search", "柜门关不严", top_k=3)

    assert calls == expected_calls                 # 没有第二轮的改写和重排
    assert len(queries) == searches                # 也没有第二轮召回
    assert result.success and len(result.data) == 3
    assert result.corrected is False
    assert result.evidence_ok is None              # "没判"，不是"不足"


async def test_a_failed_correction_round_keeps_the_first_rounds_verdict(monkeypatch):
    """第一轮模型确实判了不足、去补检，补检的改写没调成：第一轮的结论照旧，不再重排。"""
    calls = _scripted_retrieval(monkeypatch, rewrite=[REWRITTEN, REJECTED], rerank=[INSUFFICIENT])
    manager = ToolManager(api_key="test-key")
    manager.register(_knowledge_tool([]))

    result = await manager.search_with_rewrite("knowledge_search", "柜门关不严", top_k=3)

    assert calls == ["rewrite", "rerank", "rewrite"]
    assert (result.evidence_ok, result.corrected) == (False, False)


async def test_genuinely_insufficient_evidence_still_gets_its_one_extra_round(monkeypatch):
    """模型真的看过、真的判了不足——补检一轮的行为不变。"""
    calls = _scripted_retrieval(
        monkeypatch, rewrite=[REWRITTEN, QueryRewritePayload(queries=["门缝 2-3mm 标准"])],
        rerank=[INSUFFICIENT, RerankPayload(order=[0, 1, 2], sufficient=True, missing="")])
    manager = ToolManager(api_key="test-key")
    manager.register(_knowledge_tool([]))

    result = await manager.search_with_rewrite("knowledge_search", "柜门关不严", top_k=3)

    assert calls == ["rewrite", "rerank", "rewrite", "rerank"]
    assert (result.evidence_ok, result.corrected) == (True, True)


# ── 专家被拒之后不用同一把凭证找通用 Agent ────────────────────────────────────

def _fail_specialists(orch: AgentOrchestrator, error: Exception) -> list[str]:
    """专家一律抛 error；通用 Agent 能答，并记下自己被调了几次。"""
    general_calls: list[str] = []
    for agent_type, agents in orch._pool.items():
        for agent in agents:
            if agent_type == AgentType.GENERAL:
                async def _general(req, skill_prompt=""):
                    general_calls.append(req.message)
                    return "通用 Agent 的回答"
                agent._call_llm = _general
            else:
                async def _boom(req, skill_prompt="", _e=error):
                    raise _e
                agent._call_llm = _boom
    return general_calls


async def test_a_rejected_specialist_does_not_fall_back_to_general(orchestrator):
    general_calls = _fail_specialists(orchestrator, REJECTED)
    specialist = orchestrator._best_agent(AgentType.TECHNICAL)

    result = await orchestrator.run(make_request(QUESTION, IntentCategory.INSTALLATION))

    assert general_calls == []                         # 同一把凭证，不再试
    assert result.service_unavailable and result.unavailable_reason == REASON_CREDENTIAL
    assert result.handoff is False and result.agent_type == AgentType.SYSTEM
    assert "没有为它创建工单" in result.response and "转人工" in result.response
    # 这个专家是真的被选中、真的调过的：路由结论照实报，只是答的不是它
    assert result.primary_agent == AgentType.TECHNICAL and result.routing_confidence > 0
    # 不是这个 Agent 的错，不记到它头上——否则恢复之后它还要顶着这笔惩罚被降权
    assert (specialist.stats.total, specialist.stats.success) == (0, 0)


async def test_an_ordinary_specialist_failure_still_falls_back_to_general(orchestrator):
    """和凭证无关的失败（这个专家自己出了错）：降级到通用 Agent 的行为不变。"""
    general_calls = _fail_specialists(orchestrator, RuntimeError("专家自己的故障"))

    result = await orchestrator.run(make_request(QUESTION, IntentCategory.INSTALLATION))

    assert general_calls == [QUESTION]
    assert result.service_unavailable is False and result.agent_type == AgentType.GENERAL
    assert result.response == "通用 Agent 的回答"


async def test_one_rejected_request_per_question_not_two(stack):
    """真实的 SDK 路径：专家 401 之后，这条提问一共只发出去过这一次。"""
    stack.relay.reject(KEY)
    result = await stack.orchestrator.run(make_request(QUESTION, IntentCategory.INSTALLATION))
    assert result.service_unavailable and stack.relay.sent() == 1


# ── 摘要失败：历史原样留着 ────────────────────────────────────────────────────

async def test_a_rejected_summary_leaves_the_history_untouched(stack):
    stack.relay.reject(KEY)
    said = await _seed_history(stack.memory, MemoryManager.COMPRESS_AT)

    await stack.memory.add_message("u1", "c1", MsgRole.USER, "第 16 句：这单什么时候能发")

    history = await _history(stack.memory)
    assert history == [*said, "第 16 句：这单什么时候能发"]      # 一句不少，顺序不变
    assert await stack.memory._redis.get(stack.memory._summary_key("u1", "c1")) is None
    assert stack.store.docs == []                               # 也没有往情景记忆里存一份占位
    assert "摘要生成失败" not in json.dumps(
        [stack.memory._redis.lists, stack.memory._redis.strings], ensure_ascii=False)
    assert stack.relay.sent() == 1


@pytest.mark.parametrize("failure", ["timeout", "empty"])
async def test_any_summary_failure_keeps_the_original_messages(stack, monkeypatch, failure):
    """不只是凭证：超时、模型回了空正文，同样不裁历史。"""
    async def _summarise(client, **kwargs):
        if failure == "timeout":
            raise TimeoutError("timed out")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="   ")])

    monkeypatch.setattr(memory_module, "structured_create", _summarise)
    said = await _seed_history(stack.memory, MemoryManager.COMPRESS_AT + 3)

    await stack.memory._compress("u1", "c1")

    assert await _history(stack.memory) == said
    assert await stack.memory._redis.get(stack.memory._summary_key("u1", "c1")) is None


async def test_compression_resumes_once_the_model_is_back(stack):
    """停机期间攒下来的历史，恢复后的第一次压缩照常摘要、照常裁——没有落下永久的后遗症。"""
    stack.relay.reject(KEY)
    said = await _seed_history(stack.memory, MemoryManager.COMPRESS_AT + 4)
    await stack.memory._compress("u1", "c1")
    assert await _history(stack.memory) == said

    stack.relay.allow(KEY, text="客户要改 SO-1000 等订单的门板颜色")
    llm_guard.reset_all()
    await _summary_cooldown_expires(stack.memory)
    await stack.memory._compress("u1", "c1")

    assert await _history(stack.memory) == said[-MemoryManager.KEEP_RECENT:]
    summary = await stack.memory._redis.get(stack.memory._summary_key("u1", "c1"))
    assert "门板颜色" in summary


async def _summary_cooldown_expires(memory: MemoryManager, conv_id: str = "c1") -> None:
    """FakeRedis 不模拟过期：把冷却键删掉，等同于真 Redis 上 TTL 到了。"""
    await memory._redis.delete(memory._summary_cooldown_key(memory._wm_key("u1", conv_id)))


async def test_a_failed_summary_is_not_retried_on_every_message(stack, monkeypatch):
    """
    历史留着意味着列表一直超阈值。不加冷却的话，之后每条消息都会再等一次摘要——
    模型一直超时的时候就是每轮对话白等两次（压缩在请求路径上）。
    """
    attempts: list[int] = []

    async def _summarise(client, **kwargs):
        attempts.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(memory_module, "structured_create", _summarise)
    said = await _seed_history(stack.memory, MemoryManager.COMPRESS_AT)

    for i in range(6):                                       # 三轮对话，六条消息
        await stack.memory.add_message("u1", "c1", MsgRole.USER, f"后续第 {i} 句")
    assert len(attempts) == 1                                # 只试了一次
    assert await _history(stack.memory) == [*said, *(f"后续第 {i} 句" for i in range(6))]

    await _summary_cooldown_expires(stack.memory)            # 冷却过了：下一条消息再试一次
    await stack.memory.add_message("u1", "c1", MsgRole.USER, "又一句")
    assert len(attempts) == 2
    assert stack.memory._redis.strings[
        stack.memory._summary_cooldown_key(stack.memory._wm_key("u1", "c1"))] == "1"


# ── 故障不进意图缓存 ──────────────────────────────────────────────────────────

async def test_a_degraded_intent_is_not_cached_and_recovers_with_the_model(stack):
    relay, recognizer = stack.relay, stack.recognizer
    relay.reject(KEY)

    degraded = await recognizer.recognize(QUESTION)
    assert degraded.llm_failed
    assert recognizer._cache == {}                         # 降级结果没进缓存
    again = await recognizer.recognize(QUESTION)
    assert again.llm_failed and recognizer.cache_hits == 0  # 第二次是重新算的，不是缓存
    assert relay.sent() == 1                                # 但没有再出网

    relay.allow(KEY, text=json.dumps(
        {"intent": "installation", "confidence": 0.93, "reasoning": "柜门调整"}))
    llm_guard.reset_all()

    recovered = await recognizer.recognize(QUESTION)
    assert not recovered.llm_failed
    assert recovered.intent == IntentCategory.INSTALLATION  # 同一句话，拿到的是模型的判断
    assert recovered.source_scores["llm"] > 0
    assert relay.sent() == 2

    cached = await recognizer.recognize(QUESTION)           # 正常结果照常缓存
    assert cached is recovered and recognizer.cache_hits == 1 and relay.sent() == 2


async def test_any_infrastructure_failure_keeps_the_result_out_of_the_cache(recognizer):
    """超时、5xx 同理：缓存没有过期时间，记下的降级结果会一直命中到进程重启。"""
    async def _failed(message, history=None):
        return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败",
                "failed": True, "unavailable": ""}

    recognizer._llm_recognize = _failed
    result = await recognizer.recognize(QUESTION)
    assert result.llm_failed and result.llm_unavailable == ""
    assert recognizer._cache == {}

    stub_intent(recognizer, IntentCategory.INSTALLATION)
    assert (await recognizer.recognize(QUESTION)).intent == IntentCategory.INSTALLATION
    assert len(recognizer._cache) == 1


async def test_rule_matches_are_unaffected_by_the_outage(stack):
    """规则通道不经模型：停机时照常识别、照常缓存。显式转人工靠的就是它。"""
    stack.relay.reject(KEY)
    await stack.recognizer.recognize(QUESTION)              # 触发保护

    for message in ("转人工", "I want to talk to a person"):
        result = await stack.recognizer.recognize(message)
        assert result.intent == IntentCategory.HUMAN_HANDOFF
        assert result.strategy == "fast_path" and not result.llm_failed
    assert len(stack.recognizer._cache) == 2 and stack.relay.sent() == 1


# ── 不追问、不批量建工单；显式转人工照常 ──────────────────────────────────────

async def test_a_degraded_intent_is_not_blamed_on_the_customer(orchestrator):
    """
    模型那一路没答上来、凑出来的意图落到"不明"：以前会追问"您问的是哪类问题"，
    等于把模型的故障说成客户没讲清楚。
    """
    stub_agents(orchestrator)
    degraded = make_request(QUESTION, IntentCategory.OTHER, confidence=0.15, intent_degraded=True)
    result = await orchestrator.run(degraded)
    assert result.service_unavailable and result.unavailable_reason == REASON_MODEL_ERROR
    assert result.needs_clarification is False and result.handoff is False
    # 按降级意图算出来的"主 Agent"不当成一次路由结论报出去
    assert result.primary_agent == AgentType.SYSTEM and result.routing_confidence == 0.0
    assert result.supporting_agents == [] and "未调用任何 Agent" in result.routing_reason

    # 模型正常时的"意图不明"仍然照常追问
    unclear = make_request(QUESTION, IntentCategory.OTHER, confidence=0.15)
    assert (await orchestrator.run(unclear)).needs_clarification is True


def _load_main(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_HELPDESK_API_KEY", "")
    monkeypatch.setenv("AI_HELPDESK_PROTECT_CHAT", "false")
    monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
    monkeypatch.delenv("APP_ENV", raising=False)
    sys.modules.pop("app.api.main", None)
    return importlib.import_module("app.api.main")


@pytest.fixture
async def desk(stack, monkeypatch, tmp_path):
    """/chat 和 /chat/stream 背后的全套真组件，工单落在临时目录的 SQLite 里。"""
    main = _load_main(monkeypatch)
    tickets = TicketStore(db_path=str(tmp_path / "tickets.db"))
    profiled: list[tuple] = []
    update_profile = stack.memory.update_profile

    def _update_profile(user_id, conv_id):
        profiled.append((user_id, conv_id))
        return update_profile(user_id, conv_id)

    stack.memory.update_profile = _update_profile
    for name, value in {
        "_orchestrator": stack.orchestrator, "_memory": stack.memory,
        "_tool_manager": stack.tools, "_ticket_store": tickets, "_ticket_notifier": None,
        "_sql_agent": None, "_conv_log": None, "_user_auth": None, "_rate_limiter": None,
    }.items():
        monkeypatch.setattr(main, name, value)

    async def ask(message, conv_id="c1"):
        return await main.chat(main.ChatRequest(message=message, user_id="u1", conv_id=conv_id), None)

    async def ask_stream(message, conv_id="c1"):
        response = await main.chat_stream(
            main.ChatRequest(message=message, user_id="u1", conv_id=conv_id), None)
        events: dict[str, list[dict]] = {}
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            for frame in text.split("\n\n"):
                lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
                if "event" in lines:
                    events.setdefault(lines["event"], []).append(json.loads(lines["data"]))
        return events

    yield SimpleNamespace(main=main, tickets=tickets, profiled=profiled, ask=ask,
                          ask_stream=ask_stream, **vars(stack))
    await tickets.close()


async def test_an_outage_does_not_open_a_ticket_for_every_question(desk):
    desk.relay.reject(KEY)

    answers = [await desk.ask(q) for q in [QUESTION, *OTHER_QUESTIONS]]

    for answer in answers:
        assert answer.service_unavailable is True
        assert answer.unavailable_reason == REASON_CREDENTIAL
        assert (answer.handoff, answer.ticket_id, answer.ticket_status) == (False, None, "")
        assert answer.agent_type == "system" and answer.needs_clarification is False
        assert (answer.primary_agent, answer.routing_confidence) == ("system", 0.0)
        assert "没有为它创建工单" in answer.response
    assert await desk.tickets.list() == []                  # 五条提问，零张工单
    assert desk.relay.sent() == 1                           # 一共只出过一次网
    assert desk.profiled == []                              # 没答上来的轮次不提炼画像
    # 第一条之后连检索和委派都不做了：拿回来的东西没有人读
    assert desk.searched == [] and answers[-1].knowledge_used is False
    assert answers[-1].business_data["outcome"] == "model_unavailable"
    # 客户问过什么还记着
    history = await _history(desk.memory)
    assert [m for m in history if m in (QUESTION, *OTHER_QUESTIONS)] == [QUESTION, *OTHER_QUESTIONS]


async def test_streaming_reports_the_outage_the_same_way(desk):
    desk.relay.reject(KEY)

    for question in (QUESTION, OTHER_QUESTIONS[0], OTHER_QUESTIONS[1]):
        events = await desk.ask_stream(question)
        [result] = events["result"]
        assert result["service_unavailable"] is True
        assert result["unavailable_reason"] == REASON_CREDENTIAL
        assert (result["handoff"], result["ticket_id"]) == (False, None)
        assert events["routing"][0]["primary_agent"] == result["primary_agent"] == "system"
        assert events["routing"][0]["confidence"] == result["routing_confidence"] == 0.0
        assert "".join(d["text"] for d in events["delta"]) == result["response"]
        assert "error" not in events

    assert await desk.tickets.list() == []
    assert desk.relay.sent() == 1 and desk.profiled == []


@pytest.mark.parametrize("message, notice", [
    ("转人工", "需要由人工客服来处理"),
    ("I want to talk to a person", "member of our team"),
])
async def test_asking_for_a_person_still_opens_a_ticket_during_the_outage(desk, message, notice):
    """故障提示里教客户说的那句话必须真的管用：它走规则通道，不经模型。"""
    desk.relay.reject(KEY)
    unanswered = await desk.ask(QUESTION)
    assert unanswered.service_unavailable and await desk.tickets.list() == []

    answer = await desk.ask(message)

    assert answer.handoff is True and answer.service_unavailable is False
    assert answer.ticket_status == "open" and notice in answer.response
    [ticket] = await desk.tickets.list()
    assert ticket.id == answer.ticket_id and ticket.last_message == message
    assert desk.relay.sent() == 1                           # 建单没有再碰模型

    streamed = (await desk.ask_stream(message, conv_id="c2"))["result"][0]
    assert streamed["handoff"] is True and streamed["ticket_id"]
    assert len(await desk.tickets.list()) == 2 and desk.relay.sent() == 1


async def test_a_failure_unrelated_to_the_credential_is_not_a_handoff_either(desk):
    """不走保护的那类故障（超时、5xx）：没答上来同样只是"没答上来"，不建工单。"""
    stub_intent(desk.recognizer, IntentCategory.INSTALLATION)

    async def _timeout(req, skill_prompt=""):
        raise TimeoutError("upstream timed out")

    for agents in desk.orchestrator._pool.values():
        for agent in agents:
            agent._call_llm = _timeout
    desk.tools.search_with_rewrite = _no_knowledge
    answer = await desk.ask(QUESTION)
    assert answer.service_unavailable and answer.unavailable_reason == REASON_MODEL_ERROR
    assert answer.handoff is False and await desk.tickets.list() == []
    assert desk.guard.status()["available"] is True         # 凭证没问题，保护没有动


async def _no_knowledge(*args, **kwargs):
    from app.tools.tool_manager import ToolResult
    return ToolResult(success=False, data=[], tool_name="knowledge_search", error="stub")


async def test_health_reports_the_outage_and_reload_clears_it(desk, monkeypatch, tmp_path):
    from app.core.skill_loader import SkillManager

    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(desk.main, "_skill_manager", SkillManager(root_dir=str(tmp_path / "skills")))
    monkeypatch.setitem(desk.main._llm_info, "model", "claude-test")
    desk.relay.reject(KEY)
    await desk.ask(QUESTION)

    llm = (await desk.main.health())["llm"]
    assert llm["available"] is False and llm["unavailable_reason"] == REASON_CREDENTIAL
    assert llm["model"] == "claude-test"
    assert KEY not in json.dumps(llm) and "relay.example" not in json.dumps(llm)
    assert desk.relay.sent() == 1                           # 查状态本身不调模型

    reloaded = await desk.main.reload_skills()
    assert reloaded["llm_guard_reset"] == 1
    assert (await desk.main.health())["llm"]["available"] is True
    assert desk.relay.sent() == 1                           # 解除也不探测：等下一条真实请求

    await desk.ask(OTHER_QUESTIONS[0])                      # 还没修好：这一条真的去试了，又被拒
    assert desk.relay.sent() == 2
    assert (await desk.main.health())["llm"]["available"] is False


async def test_outage_turns_are_logged_quietly(desk, caplog):
    """保护在挡的时候，每条提问不再让每个环节各占一条 warning/error。"""
    desk.relay.reject(KEY)
    await desk.ask(QUESTION)                                # 触发保护的那一条照常记
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="app")

    for question in OTHER_QUESTIONS:
        await desk.ask(question)

    noisy = [r for r in caplog.records if r.name.startswith("app.")]
    assert len(noisy) == len(OTHER_QUESTIONS)               # 每条提问一条"未作答"，仅此而已
    assert all("未作答" in r.getMessage() for r in noisy)
    assert KEY not in caplog.text


# ── 评测：凭证不可用就停，已完成的留下，标注未完成 ────────────────────────────

INTENT_CASES = [IntentTestCase(q, "installation") for q in [QUESTION, *OTHER_QUESTIONS]]
DIALOG_CASES = [{"question": q} for q in [QUESTION, *OTHER_QUESTIONS]]


async def test_evaluation_stops_at_the_first_rejection(stack, tmp_path):
    """开跑之前凭证就已经不可用：整轮只打出去一次，不是"并发数 × 环节数"次。"""
    stack.relay.reject(KEY)
    baseline = tmp_path / "baseline.json"
    evaluator = EndToEndEvaluator(
        orchestrator=stack.orchestrator, recognizer=stack.recognizer, api_key=KEY, base_url=URL,
        baseline_path=str(baseline), concurrency=4)

    report = await evaluator.run(intent_cases=INTENT_CASES, dialog_cases=DIALOG_CASES, promote=True)

    assert stack.relay.sent() == 1
    assert report.incomplete is True and report.incomplete_reason == REASON_CREDENTIAL
    assert (report.total, report.planned) == (0, 1 + len(DIALOG_CASES))
    assert report.regressions == [] and "未完成" in report.recommendations[0]
    assert report.avg_scores == {}                          # 没有拿 0% 或 0.5 默认分充数
    # 不写基线（哪怕传了 promote、哪怕还没有基线），也提升不了
    assert not baseline.exists() and report.promoted is False
    assert evaluator.promote_baseline() is None and not baseline.exists()
    saved = json.loads(evaluator.incomplete_path.read_text(encoding="utf-8"))
    assert saved["incomplete"] is True and saved["incomplete_reason"] == REASON_CREDENTIAL


class _FlakyOrchestrator:
    """前 fail_at 个用例正常作答；轮到第 fail_at 个时凭证被拒。"""

    def __init__(self, guard, fail_at: int):
        self._guard, self._fail_at = guard, fail_at
        self.asked: list[str] = []

    async def run(self, req):
        self.asked.append(req.message)
        unavailable = len(self.asked) > self._fail_at
        if unavailable:
            self._guard.record_failure(SimpleNamespace(status_code=401, body=None))
        return SimpleNamespace(
            response="故障提示" if unavailable else "调节铰链上的两颗螺丝。",
            agent_type=AgentType.SYSTEM if unavailable else AgentType.TECHNICAL,
            primary_agent=None, supporting_agents=[], intent=None, handoff=False,
            answer_confidence=0.0 if unavailable else 0.9, policy_violations=[],
            service_unavailable=unavailable,
        )


async def test_evaluation_keeps_what_finished_before_the_outage(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({
        "timestamp": "2026-10-01T00:00:00", "total": 5, "passed": 5, "pass_rate": 1.0,
        "avg_scores": {"relevance": 0.9}, "release": "v-baseline",
    }), encoding="utf-8")
    before = baseline.read_text(encoding="utf-8")

    guard = llm_guard.guard_for("anthropic", None, "test-key")
    orchestrator = _FlakyOrchestrator(guard, fail_at=2)
    evaluator = EndToEndEvaluator(orchestrator=orchestrator, recognizer=None, api_key="test-key",
                                  baseline_path=str(baseline), concurrency=1)
    judged: list[str] = []

    async def _judge(question, response, context=None):
        judged.append(question)
        return QualityScores(0.9, 0.9, 0.9, 0.9)

    evaluator._judge.judge = _judge

    report = await evaluator.run(dialog_cases=DIALOG_CASES, promote=True)

    assert orchestrator.asked == [QUESTION, *OTHER_QUESTIONS[:2]]   # 第三个撞上之后不再开始新用例
    assert judged == [QUESTION, OTHER_QUESTIONS[0]]                 # 没答上来的那一轮不判分
    assert [r.test_id for r in report.results] == ["dialog_0", "dialog_1"]   # 已完成的留下
    assert report.incomplete and report.incomplete_reason == REASON_CREDENTIAL
    assert (report.total, report.passed, report.planned) == (2, 2, 5)
    assert report.regressions == []                                # 半套用例不和基线比
    assert baseline.read_text(encoding="utf-8") == before          # 基线原封不动
    saved = json.loads(evaluator.incomplete_path.read_text(encoding="utf-8"))
    assert [r["test_id"] for r in saved["results"]] == ["dialog_0", "dialog_1"]
    assert saved["planned"] == 5


async def test_intent_evaluation_does_not_score_degraded_results(stack):
    """
    凭证不可用时，规则和向量两路凑出来的降级结果不算"这条用例的识别结果"——
    那测的不是模型。
    """
    stack.relay.reject(KEY)
    from app.evaluation.evaluator import RunAbort

    abort = RunAbort(stack.guard.blocked_reason)
    metrics = await IntentEvaluator(stack.recognizer, concurrency=4).evaluate(INTENT_CASES, abort=abort)

    assert stack.relay.sent() == 1
    assert (metrics["total"], metrics["skipped"]) == (0, len(INTENT_CASES))
    assert abort.reason == REASON_CREDENTIAL


async def test_a_complete_run_is_reported_as_complete(tmp_path):
    """没出故障的一轮：incomplete 为假，基线照常落盘。"""
    baseline = tmp_path / "baseline.json"
    guard = llm_guard.guard_for("anthropic", None, "test-key")
    evaluator = EndToEndEvaluator(orchestrator=_FlakyOrchestrator(guard, fail_at=99), recognizer=None,
                                  api_key="test-key", baseline_path=str(baseline), concurrency=2)

    async def _judge(question, response, context=None):
        return QualityScores(0.9, 0.9, 0.9, 0.9)

    evaluator._judge.judge = _judge
    report = await evaluator.run(dialog_cases=DIALOG_CASES)

    assert (report.incomplete, report.incomplete_reason) == (False, "")
    assert (report.total, report.planned) == (5, 5)
    assert baseline.exists() and not evaluator.incomplete_path.exists()


def test_the_api_reports_an_incomplete_run_and_refuses_to_promote_it(monkeypatch):
    from fastapi.testclient import TestClient

    from app.evaluation.evaluator import EvalReport

    main = _load_main(monkeypatch)
    report = EvalReport(
        timestamp="2026-10-04T21:00:00", total=2, passed=2, pass_rate=1.0, avg_scores={},
        regressions=[], recommendations=["评测未完成"], results=[],
        incomplete=True, incomplete_reason=REASON_CREDENTIAL, planned=5)
    payload = main._eval_report_payload(report)
    assert (payload["incomplete"], payload["incomplete_reason"], payload["planned"], payload["total"]) \
        == (True, REASON_CREDENTIAL, 5, 2)

    monkeypatch.setattr(main, "_evaluator", SimpleNamespace(
        history=[report], promote_baseline=lambda: pytest.fail("未完成的评测不该走到提升")))
    response = TestClient(main.app).post("/eval/promote")
    assert response.status_code == 409 and REASON_CREDENTIAL in response.json()["detail"]


# ── 前端回放用的夹具：就是后端在这个场景下实际发出去的东西 ────────────────────
#
# 前端的测试和界面核对要回放"停机时后端到底发了什么"。手写的事件永远长不成真后端
# 的样子，所以夹具由这里的真处理函数生成（接的仍是 FakeRelay，没有模型参与），
# 存在 frontend/src/test/fixtures/outage/ 下面；平时这条用例核对夹具和后端现在的
# 输出一致——后端改了事件形状而夹具没跟上，这里会红。
#
# 重新生成：
#   AI_HELPDESK_RECORD_FIXTURES=<仓库>/frontend/src/test/fixtures/outage pytest tests/test_model_outage.py -k fixtures

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "test" / "fixtures" / "outage"
EN_QUESTION = "The cabinet door keeps popping open a little after I close it"


def _stable(text: str) -> str:
    """把每次运行都不一样的值换成固定的：耗时、随机的工单号、时间戳。别的原样不动。"""
    text = re.sub(r'("(?:intent_)?latency_ms": )[\d.]+', r"\g<1>0.0", text)
    text = re.sub(r'("(?:created_at|updated_at)": )[\d.]+', r"\g<1>1759636800.0", text)
    text = re.sub(r'("timestamp": ")[^"]+', r"\g<1>2026-10-04T21:00:00", text)
    seen: dict[str, str] = {}
    return re.sub(r"tk_[0-9a-f]{12}",
                  lambda m: seen.setdefault(m.group(), f"tk_{len(seen) + 1:012d}"), text)


async def _record_outage(desk, tmp_path) -> dict[str, str]:
    from app.core.request_context import request_id_var
    from app.storage.conversation_log import ConversationLog

    main = desk.main
    log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main._conv_log = log
    main._llm_info.update(provider="anthropic", model="claude-test")
    desk.relay.reject(KEY)

    async def turn(request_id: str, message: str, *, stream: bool):
        request_id_var.set(request_id)
        req = main.ChatRequest(message=message, user_id="u1001", conv_id="conv-outage")
        if stream:
            response = await main.chat_stream(req, None)
            out = "".join([c.decode() if isinstance(c, bytes) else c
                           async for c in response.body_iterator])
        else:
            out = json.dumps((await main.chat(req, None)).model_dump(), ensure_ascii=False, indent=2)
        await asyncio.gather(*list(main._background_tasks))      # 审计日志是后台写的，等它落完
        return out

    try:
        files = {
            # 第一条：意图那一次调用真的发了、被 401 拒绝，保护由此触发
            "zh-chat-stream-unavailable.sse": await turn("0a1b2c3d4e01", QUESTION, stream=True),
            # 保护已经在挡：关闭流式、英文提问
            "en-chat-unavailable.json": await turn("0a1b2c3d4e02", EN_QUESTION, stream=False),
            # 停机期间客户明确要人工：规则通道，照常建单
            "zh-chat-stream-handoff.sse": await turn("0a1b2c3d4e03", "转人工", stream=True),
        }
        request_id_var.set("-")
        files["health.json"] = json.dumps(await main.health(), ensure_ascii=False, indent=2)
        files["conversation.json"] = json.dumps(
            await main.get_conversation("conv-outage"), ensure_ascii=False, indent=2)
        files["tickets.json"] = json.dumps(await main.list_tickets(), ensure_ascii=False, indent=2)
        # 这时候有人点了"运行评测"：保护在挡，一条都没跑，一次网也不出
        evaluator = EndToEndEvaluator(
            orchestrator=desk.orchestrator, recognizer=desk.recognizer, api_key=KEY, base_url=URL,
            baseline_path=str(tmp_path / "baseline.json"), release="outage-fixture", concurrency=4)
        report = await evaluator.run(intent_cases=INTENT_CASES, dialog_cases=DIALOG_CASES)
        files["eval-incomplete.json"] = json.dumps(
            main._eval_report_payload(report), ensure_ascii=False, indent=2)
    finally:
        await log.close()
    assert desk.relay.sent() == 1                                # 整段录制只出过一次网（假的）
    return {name: _stable(text) for name, text in files.items()}


async def test_frontend_outage_fixtures_are_what_the_backend_sends(desk, tmp_path):
    files = await _record_outage(desk, tmp_path)

    target = os.getenv("AI_HELPDESK_RECORD_FIXTURES", "").strip()
    if target:
        out = pathlib.Path(target)
        out.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (out / name).write_text(text, encoding="utf-8")
        return
    if not FIXTURE_DIR.is_dir():
        pytest.skip("这份检出里没有前端夹具目录")

    for name, text in files.items():
        recorded = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        assert recorded == text, (
            f"{name} 和后端现在的输出不一致：后端改了这个场景下发出去的内容。"
            "确认改动是有意的之后重新生成夹具（见本节开头的说明），并检查前端的展示。"
        )
