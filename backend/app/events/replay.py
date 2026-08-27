"""
从事件重放一轮。四种方式，前三种不调模型：

| 方式 | 做什么 |
|---|---|
| route | 用记下的意图、实体和原话，拿当前代码重算两道门控和路由，和记录比 |
| guard | 按记录的顺序，把每一步的原始输出喂给当前的答案守卫：中途按工具结果加出处，每步结束作废半句，和记录的替换比 |
| export | 导出成 data/eval/trajectory_cases.json 的用例骨架（needs 和期望要人来填） |
| answer | 从事件重建请求，用当前代码和模型重新作答——要调模型，由 scripts/replay_turns.py 在 --yes 之后做 |

**重建要能自证。** agent_started 记了初始 messages 和 system 的哈希。重建出来的哈希对不上
（脚手架改了、SOP 改了）时，结果里标出来：那是"用当前代码重放旧输入"，不是复现。两种都有用，
但不能混在一起报。原文按保留期清空之后，重建不了（ReplayError）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.agents.tool_loop import _normalize
from app.agents.turn_prep import build_request
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events.projection import _decoded
from app.events.shapes import sha256

_SKILLS_MARK = "\n\n[动态 Skills]\n"


class ReplayError(ValueError):
    """这一轮重建不了：缺事件，或者原文已经按保留期清空。"""


def _first(evs: list[dict[str, Any]], kind: str, agent: str | None = None) -> dict[str, Any] | None:
    return next((e for e in evs if e["type"] == kind and (agent is None or e["agent"] == agent)), None)


def _needs_body(event: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    if event is None:
        raise ReplayError(f"这一轮没有 {kind} 事件，重建不了")
    if event["body"] is None:
        raise ReplayError(f"{kind} 的原文已按保留期清空（或当时只记结构），重建不了")
    return event["body"]


def _rule_calls(evs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["data"]["tool"]: e for e in evs if e["type"] == "tool_call" and e["data"].get("actor") == "rule"}


def _context_text(call: dict[str, Any] | None) -> str:
    """规则预取的一路放进专家上下文的那段。和交给模型读的不一样时另存在 context 里。"""
    if call is None:
        return ""
    body = _needs_body(call, "tool_call")
    return body["context"] if "context" in body else body.get("content", "")


def _intent_of(voted: dict[str, Any]) -> SimpleNamespace:
    data, body = voted["data"], _needs_body(voted, "intent_voted")
    urgency = str(data.get("urgency") or "")
    return SimpleNamespace(
        intent=IntentCategory(data.get("intent") or "other"),
        intent_group=data.get("group", ""),
        urgency=UrgencyLevel[urgency.upper()] if urgency else None,
        confidence=data.get("confidence", 0.0),
        entities=body.get("entities") or {},
        llm_failed=bool(data.get("llm_failed")),
    )


@dataclass
class RebuiltTurn:
    """从事件重建出来的编排请求，加上它能不能自证。"""

    turn_id: str
    request: Any
    # 作答的专家（第一个 agent_started 的实例键）；这一轮没走到专家（澄清、直接转人工）为空
    agent: str = ""
    # 重建出来的初始 messages 的哈希和当时记的一样不一样；没有专家作答时为 None
    verified: bool | None = None
    notes: list[str] = field(default_factory=list)


def rebuild_request(events) -> RebuiltTurn:
    """
    事件 → 编排请求：原话、历史、记忆上下文和预取内容拼成的上下文、实体、意图、出处。

    拼法和线上是同一个函数（agents/turn_prep.build_request）。工具循环的 TurnContext 不在这里
    重建：要按哪一组跑（规则 / 循环）由调用方决定。
    """
    from app.agents.agent_orchestrator import BaseAgent

    evs = _decoded(events)
    if not evs:
        raise ReplayError("没有事件")
    head = evs[0]
    started = _needs_body(_first(evs, "turn_started"), "turn_started")
    memory = _needs_body(_first(evs, "memory_loaded"), "memory_loaded")
    voted = _first(evs, "intent_voted")
    if voted is None:
        raise ReplayError("这一轮没有 intent_voted 事件，重建不了")
    rule = _rule_calls(evs)
    order = rule.get("order_data_query")
    request = build_request(
        started.get("message", ""),
        user_id=started.get("user_id", ""),
        conv_id=head["conv_id"],
        request_id=head["request_id"],
        memory_text=memory.get("text", ""),
        history=memory.get("recent") or None,
        intent_result=_intent_of(voted),
        knowledge_text=_context_text(rule.get("knowledge_search")),
        order_text=_context_text(order),
        order_is_evidence=bool(order and order["data"].get("evidence")),
    )
    rebuilt = RebuiltTurn(turn_id=head["turn_id"], request=request)
    first_agent = _first(evs, "agent_started")
    if first_agent is not None:
        rebuilt.agent = first_agent["agent"]
        rebuilt.verified = sha256(BaseAgent._build_messages(request)) == first_agent["data"].get("messages_sha256")
        if not rebuilt.verified:
            rebuilt.notes.append("重建出来的 messages 和当时的哈希对不上：这是用当前代码重放旧输入，不是复现")
    return rebuilt


# ── 循环状态（第 3 步续跑要的检查点，先给 answer 重放用）─────────────────────────

@dataclass
class LoopState:
    """一个专家的工具循环走到哪了：下一步交给模型的 messages、预算用了多少、查过什么。"""

    agent: str
    steps: int                                   # 已经有结果的模型调用
    messages: list[dict[str, Any]]               # 下一步交给模型的 messages
    tool_calls: int                              # 计入预算的工具调用（真的执行了的）
    delegations: int                             # 其中委派查库的
    memo: dict[tuple, str]                       # 本轮查过的（工具 + 规范化参数）→ 交还模型的内容
    sources: list[str]                           # 答案守卫的出处：预取的 + 工具查回来能当出处的
    in_flight: list[str]                         # 发起了、没有结果的调用：进程挂的时候它们正在跑
    sent_chars: int                              # 已经发给客户的字数


def _memo_key(tool: str, args: dict[str, Any]) -> tuple:
    from app.tools.turn_tools import KnowledgeSearchArgs, OrderDataQueryArgs

    params = {"knowledge_search": KnowledgeSearchArgs, "order_data_query": OrderDataQueryArgs}.get(tool)
    values = params.model_construct(**args).model_dump().values() if params else args.values()
    return (tool, *(_normalize(v) for v in values))


def rebuild_loop_state(events, agent: str) -> LoopState:
    """
    纯函数地从事件重建一个专家（实例键，如 order_0）的循环状态。

    messages 从重建的请求开始，每一步以调工具结束的，接上那一步的原始输出（tool_use 块）和
    交还模型的 tool_result——和 tool_loop.run_tool_loop 里拼的一样。
    """
    from app.agents.agent_orchestrator import BaseAgent

    evs = _decoded(events)
    rebuilt = rebuild_request(evs)
    messages: list[dict[str, Any]] = list(BaseAgent._build_messages(rebuilt.request))
    results = {e["call_id"]: e for e in evs if e["type"] == "tool_call" and e["agent"] == agent}
    steps = sorted((e for e in evs if e["type"] == "model_call" and e["agent"] == agent
                    and e["data"].get("purpose") == "answer" and e["data"].get("status") == "ok"
                    and e["body"] is not None), key=lambda e: e["step"] or 0)
    in_flight: list[str] = []
    for step in steps:
        blocks = step["body"].get("output") or []
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            continue
        messages.append({"role": "assistant", "content": blocks})
        replies = []
        for use in uses:
            call = results.get(use.get("id"))
            if call is None:
                in_flight.append(use.get("id"))
                continue
            body = call["body"] or {}
            replies.append({"type": "tool_result", "tool_use_id": use.get("id"),
                            "content": body.get("content") or "（没有结果）",
                            **({"is_error": True} if call["data"].get("is_error") else {})})
        if replies:
            messages.append({"role": "user", "content": replies})

    started = [e for e in evs if e["type"] == "tool_started" and e["agent"] == agent]
    executed = [e for e in started if e["data"].get("decision") == "execute"]
    memo: dict[tuple, str] = {}
    for call in [*_rule_calls(evs).values(), *(results.get(e["call_id"]) for e in executed)]:
        if call is not None and call["body"] is not None:
            memo[_memo_key(call["data"]["tool"], call["body"].get("args") or {})] = call["body"].get("content", "")
    sources = list(rebuilt.request.grounding_sources)
    for call in results.values():
        if call["data"].get("evidence") and call["body"] is not None:
            sources.append(call["body"].get("evidence") or call["body"].get("content", ""))
    answered = next((e for e in evs if e["type"] == "agent_answered" and e["agent"] == agent), None)
    return LoopState(
        agent=agent, steps=len(steps), messages=messages, tool_calls=len(executed),
        delegations=sum(1 for e in executed if e["data"].get("tool") == "order_data_query"),
        memo=memo, sources=sources, in_flight=in_flight,
        sent_chars=(answered or {}).get("data", {}).get("sent_chars", 0),
    )


# ── 重放守卫 ──────────────────────────────────────────────────────────────────

def _violation_key(v: dict[str, Any]) -> tuple[str, str, str]:
    return (v.get("rule", ""), v.get("evidence", ""), v.get("sentence", ""))


@dataclass
class GuardReplay:
    agent: str
    recorded: list[dict[str, Any]]
    replayed: list[dict[str, Any]]

    @property
    def same(self) -> bool:
        return [_violation_key(v) for v in self.recorded] == [_violation_key(v) for v in self.replayed]


def replay_guard(events, policy: AnswerPolicy | None = None) -> list[GuardReplay]:
    """
    每个作答成功的专家一份：记录里守卫换掉了哪些句子，当前代码会换掉哪些。

    出处照当时的：预取的知识和业务数据（重建的请求），加上当时注入的 SOP（从 agent_started
    记下的 system 里取，不读现在的 Skills 目录），循环里每一步之后再加这一步工具查回来的。
    """
    from app.agents.agent_orchestrator import BaseAgent

    policy = policy or AnswerPolicy()
    evs = _decoded(events)
    rebuilt = rebuild_request(evs)
    out: list[GuardReplay] = []
    for started in (e for e in evs if e["type"] == "agent_started"):
        agent = started["agent"]
        answered = next((e for e in evs if e["type"] == "agent_answered" and e["agent"] == agent
                         and e["seq"] > started["seq"]), None)
        if answered is None or not answered["data"].get("success"):
            continue
        system = _needs_body(started, "agent_started").get("system", "")
        skill_prompt = system.split(_SKILLS_MARK, 1)[1] if _SKILLS_MARK in system else ""
        sources = [*rebuilt.request.grounding_sources, *([skill_prompt] if skill_prompt else [])]
        steps = sorted((e for e in evs if e["type"] == "model_call" and e["agent"] == agent
                        and e["data"].get("purpose") == "answer" and e["data"].get("status") == "ok"
                        and started["seq"] < e["seq"] < answered["seq"]), key=lambda e: e["step"] or 0)
        texts = ["".join(b.get("text", "") for b in (_needs_body(s, "model_call").get("output") or [])
                         if b.get("type") == "text") for s in steps]
        if started["data"].get("mode") == "single" and not started["data"].get("stream"):
            # 非流式单次生成是整段 enforce（先摘掉转人工标记），和线上 _handle_inner 一样
            content, _ = BaseAgent._split_handoff_trailer("".join(texts))
            replayed = policy.enforce(content, sources).violations
        else:
            guard = PolicyStreamGuard(policy, sources)
            for step, text in zip(steps, texts):
                guard.feed(text)
                if not step["data"].get("tool_uses"):
                    continue
                guard.discard_pending()
                for call in (e for e in evs if e["type"] == "tool_call" and e["agent"] == agent
                             and e["step"] == step["step"] and e["data"].get("evidence")):
                    body = _needs_body(call, "tool_call")
                    guard.add_sources([body.get("evidence") or body.get("content", "")])
            guard.flush()
            replayed = guard.violations
        recorded_event = next((e for e in evs if e["type"] == "guard_replaced" and e["agent"] == agent
                               and started["seq"] < e["seq"] < answered["seq"]), None)
        recorded = _needs_body(recorded_event, "guard_replaced").get("violations", []) if recorded_event else []
        out.append(GuardReplay(agent=agent, recorded=recorded, replayed=[v.to_dict() for v in replayed]))
    return out


# ── 重放路由 ──────────────────────────────────────────────────────────────────

@dataclass
class RouteReplay:
    recorded: dict[str, Any]
    replayed: dict[str, Any]

    @property
    def differences(self) -> list[str]:
        return sorted(k for k in self.recorded if self.replayed.get(k) != self.recorded[k])


def replay_route(events, orchestrator) -> RouteReplay | None:
    """
    用当前代码重算两道门控（查不查知识库、查不查业务数据）和路由，和记录比。

    作答之前就知道模型不可用的那几轮跳过（返回 None）：那一轮的路由不是按意图定的。
    """
    from app.agents.agent_orchestrator import AgentType
    from app.tools.turn_tools import Prefetch

    evs = _decoded(events)
    planned = _first(evs, "prefetch_planned")
    routed = _first(evs, "routed")
    if planned is None or routed is None or routed["data"].get("path") == "unavailable":
        return None
    req = rebuild_request(evs).request
    gates = Prefetch(tool_manager=object(),
                     sql_agent=SimpleNamespace(enabled=bool(planned["data"].get("data_enabled")), currency=""))
    if orchestrator._needs_clarification(req):
        path, decision = "clarify", None
    else:
        decision = orchestrator._route_decision(req)
        path = ("handoff" if decision.primary_agent == AgentType.HANDOFF
                else "parallel" if decision.multi_agent else "single")
    replayed = {
        "knowledge": gates.will_search(req.message, intent=req.intent),
        "order_data": gates.will_query(req.message, intent=req.intent, entities=req.entities),
        "path": path,
        "primary": decision.primary_agent.value if decision else "general",
        "supporting": [a.value for a in decision.supporting_agents] if decision else [],
    }
    recorded = {
        "knowledge": planned["data"].get("knowledge"), "order_data": planned["data"].get("order_data"),
        "path": routed["data"].get("path"), "primary": routed["data"].get("primary"),
        "supporting": routed["data"].get("supporting", []),
    }
    if planned["data"].get("skipped"):
        # 那一轮门控没判（模型不可用时整段跳过），不比
        for key in ("knowledge", "order_data"):
            recorded.pop(key)
            replayed.pop(key)
    return RouteReplay(recorded=recorded, replayed=replayed)


# ── 导出成用例 ────────────────────────────────────────────────────────────────

def export_case(events, *, case_id: str, category: str = "B") -> dict[str, Any]:
    """
    一轮 → data/eval/trajectory_cases.json 里一条用例的骨架：历史、原话、意图标注、业务数据的桩
    （payload 用当时下游返回的归一化结果）。needs 和期望要人来填——导出的只是"当时发生了什么"。
    """
    evs = _decoded(events)
    started = _needs_body(_first(evs, "turn_started"), "turn_started")
    memory = _needs_body(_first(evs, "memory_loaded"), "memory_loaded")
    voted = _first(evs, "intent_voted")
    raws = [(call["body"].get("args") or {}, call["body"]["raw"]) for call in evs
            if call["type"] == "tool_call" and call["data"].get("tool") == "order_data_query"
            and call["body"] is not None and call["body"].get("raw")]
    if len(raws) == 1:
        stubs = [{"match": ".*", "payload": raws[0][1], "answer": raws[0][1].get("answer", "")}]
    else:
        stubs = [{"match": re.escape(str(args.get("query", ""))[:60]) or ".*", "payload": raw,
                  "answer": raw.get("answer", "")} for args, raw in raws]
    return {
        "id": case_id,
        "category": category,
        "intent_label": (voted or {}).get("data", {}).get("intent") or "other",
        "message": started.get("message", ""),
        "history": [{"role": m.get("role"), "content": m.get("content")} for m in memory.get("recent") or []],
        "stubs": stubs,
        "needs": [],
        "tags": ["exported"],
        "note": f"从 turn {evs[0]['turn_id']} 导出：needs 和期望还要人来填",
    }
