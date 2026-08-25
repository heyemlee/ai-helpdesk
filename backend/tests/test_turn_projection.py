"""
一轮对话的事件：准备、路由、专家这一层，和从事件投影回响应（app/events/projection.py）。

八条路径各有一份"金标准事件序列"：单专家作答、并行、澄清、直接转人工、门槛转人工、
作答前就知道模型不可用、流式答到一半失败、专家失败降级到 general。每条路径上：

- 事件按这个顺序发、类型一个不多一个不少；
- `project_turn(事件)` 和当轮真正发出去的响应一致（影子校验的计数也没有动）；
- 金丝雀：原话、user_id、实体、知识片段、业务数据、模型的回答里各放一个特殊字串，
  所有事件的 data 里一处都不许出现——清空原文之后剩下的行指不到人。

全部离线：模型是 conftest 的脚本化假模型，工具是记账的假 ToolManager。
"""
import importlib
import json
import sys
from typing import Any

import pytest
from conftest import ScriptedModel, text_step
from prometheus_client import REGISTRY

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EventLog, EventLogConfig
from app.events.projection import KNOWLEDGE_NOT_SEARCHED, mismatches, project_turn
from app.memory.conversation_memory import MemoryContext
from app.tickets.ticket_store import TicketStore
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import ToolResult
from app.tools.turn_tools import Prefetch

CANARY = {
    "message": "柜门金丝雀-MSG-71c2 到哪了",
    "user": "user-canary-USR-48d0",
    "entity": "SO-CANARY-ENT-93a1",
    "knowledge": "金丝雀-KB-5e77",
    "data": "金丝雀-DATA-2b19",
    "answer": "金丝雀-ANS-c604",
}

KNOWLEDGE = [{"title": f"补件流程 {CANARY['knowledge']}", "content": f"缺件补发在核实后安排。{CANARY['knowledge']}",
              "score": 0.6, "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}]
ORDER_DATA = {
    "answer": f"补件已排产 {CANARY['data']}", "queried_database": True, "execution_status": "success",
    "has_risk": False, "degraded": False, "selected_databases": ["wms_db"], "trace_id": "t-1",
    "steps": [{"database": "wms_db", "success": True}],
}


class _Intent:
    def __init__(self, intent: IntentCategory, confidence: float = 0.9, urgency=UrgencyLevel.LOW,
                 entities: dict | None = None):
        self.intent = intent
        self.intent_group = intent.value
        self.urgency = urgency
        self.confidence = confidence
        self.entities = {"order_id": [CANARY["entity"]]} if entities is None else entities
        self.fast_path = True
        self.strategy = "fast_path"
        self.source_scores = {"rules": confidence, "embedding": 0.0, "llm": 0.0}
        self.latency_ms = 1.234
        self.reasoning = f"命中规则 {CANARY['message']}"


class _Memory:
    cross_session = False

    async def get_context(self, user_id, conv_id, query=""):
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    async def add_message(self, user_id, conv_id, role, content):
        return None

    async def update_profile(self, user_id, conv_id):
        return None


class _Tools:
    async def call(self, name, params, context=None, **_):
        if name == "knowledge_search":
            return ToolResult(success=True, data=[dict(k) for k in KNOWLEDGE], tool_name=name)
        return ToolResult(success=True, data=dict(ORDER_DATA), tool_name=name)


class _BreaksMidStream:
    """流式时先吐一整句，再断：模拟答到一半模型那边出错。"""

    def __init__(self):
        self.messages = self

    def stream(self, **kwargs):
        class _Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            @property
            def text_stream(self):
                async def _gen():
                    yield "第一句已经发出去了。"
                    raise RuntimeError("连接断了")
                return _gen()

        return _Stream()


def _answer(text: str = f"补件已经登记。{CANARY['answer']}") -> ScriptedModel:
    return ScriptedModel(*[text_step(text)] * 6)


def _sample(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


async def _desk(monkeypatch, intent: _Intent, *, models: dict[AgentType, Any] | None = None,
                data: bool = True, outage: str = "", tickets=None):
    for name, value in {"ANTHROPIC_API_KEY": "test-key", "AI_HELPDESK_API_KEY": "",
                        "AI_HELPDESK_PROTECT_CHAT": "false", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                        "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_EVENT_LOG": "full"}.items():
        monkeypatch.setenv(name, value)
    for name in ("APP_ENV", "AI_HELPDESK_TOOL_LOOP"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("app.api.main", None)
    main = importlib.import_module("app.api.main")

    orch = AgentOrchestrator(api_key="test-key")
    models = models or {}
    for agent_type, agents in orch._pool.items():
        client = models.get(agent_type) or _answer()
        for agent in agents:
            agent._client = client

    async def recognize(message, history=None):
        return intent

    orch.recognize_intent = recognize
    if outage:
        orch.llm_unavailable_reason = lambda: outage
    log = await EventLog.open(EventLogConfig.from_env())
    for name, value in {
        "_orchestrator": orch, "_memory": _Memory(), "_tool_manager": _Tools(),
        "_sql_agent": SQLAgentClient(base_url="http://x:8000") if data else None,
        "_ticket_store": tickets, "_ticket_notifier": None, "_conv_log": None,
        "_user_auth": None, "_rate_limiter": None, "_event_log": log,
    }.items():
        monkeypatch.setattr(main, name, value)
    return main, log


def _request(main, message: str = CANARY["message"]):
    return main.ChatRequest(message=message, user_id=CANARY["user"], conv_id="c1")


async def _chat(main, log, message: str = CANARY["message"]):
    """/chat：返回 (响应, 这一轮解开的事件)。"""
    before = _sample("turn_projection_mismatch_total")
    answer = await main.chat(_request(main, message), None)
    recorded = [e.to_dict() for e in await log.store.events_of(answer.turn_id)]
    assert _sample("turn_projection_mismatch_total") == before, "影子校验报了不一致"
    return answer.model_dump(), recorded


async def _stream(main, log, message: str = CANARY["message"]):
    """/chat/stream：返回 ({meta, routing, result, shown}, 这一轮解开的事件)。"""
    before = _sample("turn_projection_mismatch_total")
    response = await main.chat_stream(_request(main, message), None)
    frames: dict[str, Any] = {"shown": ""}
    async for chunk in response.body_iterator:
        for frame in chunk.split("\n\n"):
            lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
            if "event" not in lines:
                continue
            data = json.loads(lines["data"])
            if lines["event"] == "delta":
                frames["shown"] += data["text"]
            elif lines["event"] in ("meta", "routing", "result"):
                frames[lines["event"]] = data
    recorded = [e.to_dict() for e in await log.store.events_of(frames["result"]["turn_id"])]
    assert _sample("turn_projection_mismatch_total") == before, "影子校验报了不一致"
    return frames, recorded


def _kinds(recorded: list[dict]) -> list[str]:
    """事件类型序列；专家的事件带上专家类型（不带实例号：同类有几个实例，轮询选的）。"""
    out = []
    for e in recorded:
        agent = e["agent"].rpartition("_")[0] if e["agent"] else ""
        out.append(f"{e['type']}:{agent}" if agent else e["type"])
    return out


PREP = ["turn_started", "memory_loaded", "intent_voted", "prefetch_planned"]
RULES = ["tool_started", "tool_call", "tool_started", "tool_call"]


def _assert_projects_to(recorded: list[dict], *, meta: dict, result: dict, routing: dict | None = None):
    projected = project_turn(recorded)
    assert projected["complete"] and not projected["redacted"]
    assert mismatches(projected["meta"], meta) == []
    assert mismatches(projected["result"], result) == []
    # 投影出来的字段在响应里都有：不是"只比了一部分"
    assert set(projected["result"]) - {"turn_id", "request_id", "conv_id"} <= set(result)
    assert set(projected["meta"]) <= set(meta) | {"turn_id"}
    if routing is not None:
        assert mismatches(projected["routing"], routing) == []
        assert {k: projected["routing"][k] for k in ("primary_agent", "supporting_agents", "reason")} == {
            k: routing[k] for k in ("primary_agent", "supporting_agents", "reason")}


def _assert_no_canary_in_structure(recorded: list[dict]):
    structure = json.dumps([e["data"] for e in recorded], ensure_ascii=False)
    envelope = json.dumps([{k: v for k, v in e.items() if k not in ("data", "body")} for e in recorded],
                          ensure_ascii=False)
    for name, canary in CANARY.items():
        assert canary not in structure, f"{name} 的原文进了 data"
        assert canary not in envelope, f"{name} 的原文进了信封"


# ── 八条路径 ──────────────────────────────────────────────────────────────────

async def test_a_single_specialist_answer(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))

    answer, recorded = await _chat(main, log)
    assert _kinds(recorded) == [*PREP, *RULES, "routed", "agent_started:order", "agent_answered:order",
                                "turn_completed"]
    _assert_projects_to(recorded, meta=answer, result=answer)
    _assert_no_canary_in_structure(recorded)

    frames, streamed = await _stream(main, log)
    assert _kinds(streamed) == _kinds(recorded)
    _assert_projects_to(streamed, meta=frames["meta"], result=frames["result"], routing=frames["routing"])
    _assert_no_canary_in_structure(streamed)

    started = next(e for e in streamed if e["type"] == "agent_started")
    assert started["data"]["mode"] == "single" and started["data"]["stream"] is True
    assert len(started["data"]["system_sha256"]) == len(started["data"]["messages_sha256"]) == 64
    assert started["body"]["system"]
    answered = next(e for e in streamed if e["type"] == "agent_answered")
    assert answered["data"]["success"] and answered["data"]["sent_chars"] == len(frames["shown"])


async def test_two_specialists_in_parallel(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.DAMAGE_DEFECT))
    message = "门板有划痕，而且尾款还多收了"

    answer, recorded = await _chat(main, log, message)
    assert answer["supporting_agents"] == ["billing"]
    routed = next(e for e in recorded if e["type"] == "routed")
    assert routed["data"]["path"] == "parallel" and routed["data"]["domain_scores"]["technical"] > 0
    assert sorted(k for k in _kinds(recorded) if k.startswith("agent_")) == [
        "agent_answered:billing", "agent_answered:technical", "agent_started:billing", "agent_started:technical"]
    _assert_projects_to(recorded, meta=answer, result=answer)

    frames, streamed = await _stream(main, log, message)
    _assert_projects_to(streamed, meta=frames["meta"], result=frames["result"], routing=frames["routing"])


async def test_clarification(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.OTHER, confidence=0.2, entities={}))
    message = "那个东西怎么弄啊"

    answer, recorded = await _chat(main, log, message)
    assert answer["needs_clarification"]
    routed = next(e for e in recorded if e["type"] == "routed")
    assert routed["data"]["path"] == "clarify"
    assert _kinds(recorded)[-2:] == ["routed", "turn_completed"]
    assert not any(e["type"].startswith("agent_") for e in recorded)
    completed = recorded[-1]["data"]
    assert completed["outcome"] == "clarification"
    _assert_projects_to(recorded, meta=answer, result=answer)

    frames, streamed = await _stream(main, log, message)
    _assert_projects_to(streamed, meta=frames["meta"], result=frames["result"], routing=frames["routing"])


@pytest.mark.parametrize("intent", [
    _Intent(IntentCategory.HUMAN_HANDOFF),
    _Intent(IntentCategory.DAMAGE_DEFECT, urgency=UrgencyLevel.CRITICAL),
])
async def test_a_direct_handoff(monkeypatch, tmp_path, intent):
    tickets = TicketStore(db_path=str(tmp_path / "tickets.db"))
    main, log = await _desk(monkeypatch, intent, tickets=tickets)

    answer, recorded = await _chat(main, log)
    assert answer["handoff"] and answer["ticket_status"] == "open"
    assert _kinds(recorded)[-4:] == ["routed", "handoff", "ticket_created", "turn_completed"]
    handoff = next(e for e in recorded if e["type"] == "handoff")["data"]
    assert handoff["path"] == "direct" and handoff["executed_agent"] is None
    ticket = next(e for e in recorded if e["type"] == "ticket_created")["data"]
    assert ticket == {"ticket_id": answer["ticket_id"], "status": "open", "notification": "not_configured"}
    _assert_projects_to(recorded, meta=answer, result=answer)
    _assert_no_canary_in_structure(recorded)

    frames, streamed = await _stream(main, log)
    _assert_projects_to(streamed, meta=frames["meta"], result=frames["result"], routing=frames["routing"])
    await tickets.close()


async def test_a_handoff_because_a_signal_fell_short(monkeypatch):
    """意图置信度低于门槛：专家照常作答，答完按信号转人工。工单没建成（没接工单库）也照实记。"""
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART, confidence=0.3))

    answer, recorded = await _chat(main, log)
    assert answer["handoff"] and answer["ticket_status"] == "not_created"
    assert _kinds(recorded)[-6:] == ["routed", "agent_started:order", "agent_answered:order",
                                     "handoff", "ticket_created", "turn_completed"]
    handoff = next(e for e in recorded if e["type"] == "handoff")["data"]
    assert handoff["path"] == "signals" and handoff["executed_agent"] == "order"
    assert "；".join(handoff["reasons"]) == answer["handoff_reason"]
    _assert_projects_to(recorded, meta=answer, result=answer)

    frames, streamed = await _stream(main, log)
    _assert_projects_to(streamed, meta=frames["meta"], result=frames["result"], routing=frames["routing"])


async def test_the_model_is_known_to_be_unavailable_before_answering(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART), outage="quota_exhausted")

    answer, recorded = await _chat(main, log)
    assert answer["service_unavailable"] and answer["unavailable_reason"] == "quota_exhausted"
    assert _kinds(recorded) == [*PREP, "routed", "turn_completed"]
    planned = next(e for e in recorded if e["type"] == "prefetch_planned")["data"]
    assert planned["skipped"] == "model_unavailable:quota_exhausted"
    routed = next(e for e in recorded if e["type"] == "routed")["data"]
    assert (routed["path"], routed["primary"], routed["confidence"]) == ("unavailable", "system", 0.0)
    assert recorded[-1]["data"]["outcome"] == "unavailable"
    _assert_projects_to(recorded, meta=answer, result=answer)

    frames, streamed = await _stream(main, log)
    _assert_projects_to(streamed, meta=frames["meta"], result=frames["result"], routing=frames["routing"])


async def test_the_stream_breaks_half_way_through(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART),
                            models={AgentType.ORDER: _BreaksMidStream()})

    frames, recorded = await _stream(main, log)
    assert frames["result"]["service_unavailable"]
    assert frames["shown"].startswith("第一句已经发出去了。")
    assert _kinds(recorded)[-4:] == ["routed", "agent_started:order", "agent_answered:order", "turn_completed"]
    answered = next(e for e in recorded if e["type"] == "agent_answered")["data"]
    assert not answered["success"] and answered["sent_chars"] == len("第一句已经发出去了。")
    completed = recorded[-1]
    assert completed["data"]["partial"] is True and completed["data"]["outcome"] == "unavailable"
    assert completed["body"] == {"response": frames["result"]["response"], "shown": frames["shown"]}
    _assert_projects_to(recorded, meta=frames["meta"], result=frames["result"], routing=frames["routing"])


async def test_a_failing_specialist_falls_back_to_general(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART), models={
        AgentType.ORDER: ScriptedModel(fail_at=(1, RuntimeError("订单专家出错"))),
    })

    answer, recorded = await _chat(main, log)
    assert answer["agent_type"] == "general" and not answer["service_unavailable"]
    assert _kinds(recorded)[-7:] == [
        "routed", "agent_started:order", "agent_answered:order", "agent_fallback:order",
        "agent_started:general", "agent_answered:general", "turn_completed"]
    fallback = next(e for e in recorded if e["type"] == "agent_fallback")["data"]
    assert fallback["reason"] == "failed" and fallback["to"].startswith("general_")
    _assert_projects_to(recorded, meta=answer, result=answer)


# ── 守卫替换、知识片段、业务数据 ──────────────────────────────────────────────

async def test_guard_replacements_are_recorded_and_projected(monkeypatch):
    """没有出处的价格承诺被守卫换掉：句子原文在 body，结构里只有条数和规则。"""
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART), data=False, models={
        AgentType.ORDER: _answer("补件运费是 680 元，三个工作日内送到。"),
    })
    answer, recorded = await _chat(main, log)
    assert answer["policy_violations"]
    replaced = next(e for e in recorded if e["type"] == "guard_replaced")
    assert replaced["data"]["count"] == len(answer["policy_violations"])
    assert replaced["body"]["violations"] == answer["policy_violations"]
    assert "680" not in json.dumps(replaced["data"], ensure_ascii=False)
    _assert_projects_to(recorded, meta=answer, result=answer)


async def test_the_rule_prefetch_is_recorded_with_its_latency_even_with_the_loop_off(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    answer, recorded = await _chat(main, log)

    assert answer["trajectory"] == []                       # 响应里的轨迹照旧只在循环打开时有
    calls = {e["data"]["tool"]: e for e in recorded if e["type"] == "tool_call"}
    assert set(calls) == {"knowledge_search", "order_data_query"}
    for tool, event in calls.items():
        assert event["data"]["actor"] == "rule" and event["step"] == 0 and event["call_id"] == ""
        assert event["data"]["latency_ms"] is not None
        assert event["data"]["idem_key"] == f"{answer['turn_id']}:rule:{tool}"
        assert event["body"]["args"] == {"query": CANARY["message"]}
    assert calls["order_data_query"]["body"]["raw"]["answer"] == ORDER_DATA["answer"]
    assert calls["order_data_query"]["data"]["trace"]["databases"] == ["wms_db"]
    excerpt = calls["knowledge_search"]["body"]["excerpts"][0]
    assert CANARY["knowledge"] in excerpt["title"]
    started = [e["data"]["decision"] for e in recorded if e["type"] == "tool_started"]
    assert started == ["execute", "execute"]


async def test_purged_text_still_projects_the_structure(monkeypatch):
    """原文按保留期清空之后：结构照常投影，要原文的字段是空的，redacted 为真。"""
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    answer, recorded = await _chat(main, log)
    purged = [{**e, "body": None} for e in recorded]

    projected = project_turn(purged)
    assert projected["complete"] and projected["redacted"]
    meta, result = projected["meta"], projected["result"]
    assert (meta["intent"], meta["knowledge_used"], meta["entities"]) == ("missing_part", True, {})
    assert meta["knowledge_sources"] == [{k: v for k, v in s.items() if k not in ("title", "doc_title", "excerpt")}
                                         for s in answer["knowledge_sources"]]
    assert meta["business_data"]["outcome"] == answer["business_data"]["outcome"] == "evidence"
    assert result["response"] == "" and result["agent_type"] == answer["agent_type"]
    assert result["routing_reason"] == answer["routing_reason"]


async def test_the_not_searched_shape_matches_the_prefetch():
    trace: dict = {}
    await Prefetch(tool_manager=None).knowledge_context("随便问问", trace=trace)
    assert trace == KNOWLEDGE_NOT_SEARCHED


def test_an_empty_or_unfinished_turn_is_reported_as_incomplete():
    assert project_turn([])["complete"] is False
    started = {"turn_id": "t", "seq": 0, "conv_id": "c", "request_id": "r", "type": "turn_started",
               "agent": "", "step": None, "call_id": "", "data": {}, "body": {}}
    assert project_turn([started])["complete"] is False                         # 没有终结事件
    completed = {**started, "seq": 2, "type": "turn_completed"}
    assert project_turn([started, completed])["complete"] is False              # seq 有缺口
    assert project_turn([started, {**completed, "seq": 1}])["complete"] is True


async def test_the_shadow_check_notices_when_the_response_and_the_events_drift(monkeypatch):
    """有人改了响应、忘了事件：这一轮照常返回，只记一条日志（只有字段名）和一次计数。"""
    main, _ = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    real = main.project_turn

    def drifted(events):
        projected = real(events)
        projected["result"]["agent_type"] = "billing"
        return projected

    warned: list[str] = []

    class _Log:
        def warning(self, msg, *args, **kwargs):
            warned.append(msg % args)

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    monkeypatch.setattr(main, "project_turn", drifted)
    monkeypatch.setattr(main, "logger", _Log())
    before = _sample("turn_projection_mismatch_total")
    answer = await main.chat(_request(main), None)

    assert answer.agent_type == "order"
    assert _sample("turn_projection_mismatch_total") == before + 1
    assert any("result.agent_type" in w and CANARY["answer"] not in w for w in warned)
