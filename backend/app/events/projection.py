"""
从一轮的事件投影出 {meta, routing, result}：和前端 ChatMessage 里的三个对象一模一样
（/chat/stream 的 meta、routing、result 三个事件的形状）。

读接口（刷新之后恢复检查器和学习模式）、影子校验（每轮结束拿内存里的事件和响应比）、
测试都用这一份，投影只有这一份代码。

| 响应字段 | 来自哪些事件 |
|---|---|
| intent、intent_*、entities | intent_voted |
| knowledge、knowledge_sources、knowledge_used、business_data | prefetch_planned、tool_call(actor=rule) |
| routing、primary_agent、supporting_agents、routing_reason、routing_confidence | routed、turn_completed |
| response、agent_type(s)、escalated、answer_confidence、handoff、needs_clarification、service_unavailable … | turn_completed、handoff |
| policy_violations | guard_replaced |
| ticket_* | ticket_created |
| trajectory | tool_call |
| answer_usage | model_call(purpose=answer)、loop_ended、loop_fallback |

原文已经按保留期清空时（body 为 None），结构照常投影，要原文的字段是空的：`redacted` 为真。
"""
from __future__ import annotations

from typing import Any

from app.events.shapes import agent_type_of, record_from_event
from app.events.types import TERMINAL_TYPES, Event

# 这一轮没检索知识库时，响应里 knowledge 报的样子（tools/turn_tools.Prefetch.knowledge_context
# 开头那一句）。有一条测试守着两边一致
KNOWLEDGE_NOT_SEARCHED: dict[str, Any] = {
    "attempted": False, "mode": "", "sources": [], "reranked": False,
    "corrected": False, "evidence_ok": None, "degraded": False,
}

# 规则预取的两条在轨迹里的顺序：两路是并发跑的，事件按谁先完成排，轨迹按这个固定顺序
_RULE_ORDER = {"knowledge_search": 0, "order_data_query": 1}


def _decoded(events) -> list[dict[str, Any]]:
    out = [e.to_dict() if isinstance(e, Event) else e for e in events]
    return sorted(out, key=lambda e: e["seq"])


def _first(events: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((e for e in events if e["type"] == kind), None)


def project_turn(events) -> dict[str, Any]:
    """
    一轮的事件 → {turn_id, complete, redacted, meta, routing, result}。

    complete 为假有两种可能：seq 有缺口（中间丢过），或者没有终结事件（没写完，或者进程在
    这一轮中间挂了）。读的一方照实显示"不完整"，不假装完整。
    """
    evs = _decoded(events)
    if not evs:
        return {"turn_id": "", "complete": False, "redacted": False, "meta": {}, "routing": {}, "result": {}}
    head = evs[0]
    turn_id, conv_id = head["turn_id"], head["conv_id"]
    started = _first(evs, "turn_started")
    terminal = next((e for e in evs if e["type"] in TERMINAL_TYPES), None)
    complete = (started is not None and terminal is not None
                and [e["seq"] for e in evs] == list(range(len(evs))))
    redacted = started is not None and started["body"] is None

    meta = _meta(evs, turn_id, conv_id)
    routing = _routing(evs)
    result = _result(evs, turn_id, conv_id, routing, started)
    # 排队模式（app/runs/）：这一轮是哪个 run 的第几次执行、按哪一档降级。turn_started 里记着，
    # meta 和 result 里各带一份，和当轮发出去的一样；inline 的轮次没有这两项
    started_data = (started or {}).get("data") or {}
    run_fields = {key: started_data[key] for key in ("run", "degrade") if key in started_data}
    meta.update(run_fields)
    if "response" in result:
        result.update(run_fields)
    return {"turn_id": turn_id, "complete": complete, "redacted": redacted,
            "meta": meta, "routing": routing, "result": result}


def _rule_calls(evs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["data"].get("tool"): e for e in evs
            if e["type"] == "tool_call" and e["data"].get("actor") == "rule"}


def _meta(evs: list[dict[str, Any]], turn_id: str, conv_id: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"conv_id": conv_id, "turn_id": turn_id}
    voted = _first(evs, "intent_voted")
    if voted is not None:
        data, body = voted["data"], voted["body"] or {}
        # 事件里记的是原值（重放要用），这里按响应的口径取整
        meta.update(
            intent=data.get("intent", "other"),
            intent_group=data.get("group", "other"),
            intent_confidence=round(data.get("confidence", 0.0), 4),
            intent_strategy=data.get("strategy", "ensemble"),
            intent_latency_ms=round(data.get("latency_ms", 0.0), 2),
            intent_source_scores=data.get("source_scores", {}),
            entities=body.get("entities", {}),
        )

    rule = _rule_calls(evs)
    knowledge_call = rule.get("knowledge_search")
    trace = (record_from_event(knowledge_call)["trace"] if knowledge_call is not None
             else dict(KNOWLEDGE_NOT_SEARCHED))
    meta["knowledge_used"] = bool(knowledge_call is not None and knowledge_call["data"].get("outcome") == "found")
    meta["knowledge_sources"] = trace.get("sources", [])
    meta["knowledge"] = {k: v for k, v in trace.items() if k != "sources"}

    order_call = rule.get("order_data_query")
    if order_call is not None:
        meta["business_data"] = record_from_event(order_call)["trace"]
    else:
        planned = _first(evs, "prefetch_planned")
        meta["business_data"] = dict((planned or {}).get("data", {}).get("not_queried") or {})
    return meta


def _routing(evs: list[dict[str, Any]]) -> dict[str, Any]:
    routed = _first(evs, "routed")
    if routed is None:
        return {}
    data = routed["data"]
    routing = {"primary_agent": data.get("primary", ""), "supporting_agents": data.get("supporting", []),
               "reason": data.get("reason", ""), "confidence": data.get("confidence", 0.0)}
    if data.get("domain_scores"):
        routing["domain_scores"] = data["domain_scores"]
    return routing


def _violations(evs: list[dict[str, Any]], routing: dict[str, Any]) -> list[dict[str, Any]]:
    """
    守卫换掉的句子，按响应里的顺序：并行的几个专家按路由里的先后（主专家在前），
    同一个专家按发生顺序。只算作答成功的专家——没答上来的那一轮响应里不报替换。
    """
    answered = {e["agent"] for e in evs if e["type"] == "agent_answered" and e["data"].get("success")}
    slots = [routing.get("primary_agent", ""), *routing.get("supporting_agents", [])]
    # 专家失败降级到 general 时，general 的回答占的是那个专家的位置
    took_over = {agent_type_of(e["data"].get("to", "")): agent_type_of(e["data"].get("from", ""))
                 for e in evs if e["type"] == "agent_fallback"}

    def slot(agent: str) -> int:
        kind = agent_type_of(agent)
        kind = kind if kind in slots else took_over.get(kind, kind)
        return slots.index(kind) if kind in slots else len(slots)

    replaced = [e for e in evs if e["type"] == "guard_replaced" and e["agent"] in answered]
    out: list[dict[str, Any]] = []
    for event in sorted(replaced, key=lambda e: slot(e["agent"])):
        if event["body"] is not None:
            out.extend(event["body"].get("violations", []))
        else:
            out.extend({"rule": rule} for rule in event["data"].get("rules", []))
    return out


def _trajectory(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    响应 trajectory 的顺序：规则预取的两条在前（固定顺序），然后是模型发起的。

    循环里一步的几个工具并发跑，事件按谁先结束排；而轨迹是整步跑完之后按模型发出的顺序
    一组记进去的（tool_loop.run_tool_loop）。所以组（专家 + 第几步）按组里最后结束的那条排，
    组内按 tool_started 的先后排。
    """
    calls = [e for e in evs if e["type"] == "tool_call"]
    rule = sorted((e for e in calls if e["data"].get("actor") == "rule"),
                  key=lambda e: _RULE_ORDER.get(e["data"].get("tool"), len(_RULE_ORDER)))
    issued = {(e["agent"], e["call_id"]): e["seq"] for e in evs
              if e["type"] == "tool_started" and e["data"].get("actor") != "rule"}
    groups: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for event in calls:
        if event["data"].get("actor") != "rule":
            groups.setdefault((event["agent"], event["step"]), []).append(event)
    model = [
        event
        for group in sorted(groups.values(), key=lambda g: max(e["seq"] for e in g))
        for event in sorted(group, key=lambda e: issued.get((e["agent"], e["call_id"]), e["seq"]))
    ]
    return [record_from_event(e) for e in [*rule, *model]]


def _answer_usage(evs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    作答阶段的模型用量，口径同 TurnContext.usage()：拿到了用量的作答调用才算一次。
    被要求直接作答的原因取第一个；退回单次生成记 fallback。
    """
    usage: dict[str, Any] = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
    for event in evs:
        data = event["data"]
        if event["type"] != "model_call" or data.get("purpose") != "answer" or not data.get("usage"):
            continue
        usage["model_calls"] += 1
        usage["input_tokens"] += data["usage"].get("input_tokens") or 0
        usage["output_tokens"] += data["usage"].get("output_tokens") or 0
    forced = next((e["data"]["forced"] for e in evs if e["type"] == "loop_ended" and e["data"].get("forced")), "")
    if forced:
        usage["forced"] = forced
    fallback = next((e["data"]["reason"] for e in evs if e["type"] == "loop_fallback"), "")
    if fallback:
        usage["fallback"] = fallback
    return usage


def _result(evs, turn_id, conv_id, routing, started) -> dict[str, Any]:
    result: dict[str, Any] = {"conv_id": conv_id, "request_id": evs[0]["request_id"], "turn_id": turn_id}
    completed = _first(evs, "turn_completed")
    if completed is None:
        return result
    data, body = completed["data"], completed["body"] or {}
    handoff = next((e for e in reversed(evs) if e["type"] == "handoff"), None)
    ticket = _first(evs, "ticket_created")
    loop = bool(((started or {}).get("data") or {}).get("loop", {}).get("enabled"))
    result.update(
        response=body.get("response", ""),
        agent_type=data.get("agent_type", ""),
        agent_types=data.get("agent_types", []),
        primary_agent=data.get("primary", ""),
        supporting_agents=data.get("supporting", []),
        routing_reason=routing.get("reason", ""),
        routing_confidence=routing.get("confidence", 0.0),
        escalated=data.get("escalated", False),
        handoff=data.get("handoff", False),
        handoff_reason="；".join(handoff["data"].get("reasons", [])) if handoff else "",
        needs_clarification=data.get("needs_clarification", False),
        answer_confidence=data.get("answer_confidence", 1.0),
        policy_violations=_violations(evs, routing),
        service_unavailable=data.get("service_unavailable", False),
        unavailable_reason=data.get("unavailable_reason", ""),
        latency_ms=data.get("latency_ms", 0.0),
        ticket_id=ticket["data"].get("ticket_id") if ticket else None,
        ticket_status=ticket["data"].get("status", "") if ticket else "",
        ticket_notification=ticket["data"].get("notification", "") if ticket else "",
        trajectory=_trajectory(evs) if loop else [],
        answer_usage=_answer_usage(evs) if loop else {},
    )
    return result


def summarize_turn(events) -> dict[str, Any]:
    """
    一轮的摘要：列一个会话的各轮用（GET /conversations/{conv_id}/turns）。

    outcome 取终结事件：turn_completed 里记的结局（answered / handoff / clarification /
    unavailable），turn_failed 记 failed，turn_aborted 记 aborted，没有终结事件记 incomplete。
    """
    evs = _decoded(events)
    projected = project_turn(evs)
    started = _first(evs, "turn_started")
    terminal = next((e for e in evs if e["type"] in TERMINAL_TYPES), None)
    completed = (terminal or {}).get("data", {}) if (terminal or {}).get("type") == "turn_completed" else {}
    outcome = (completed.get("outcome") if completed
               else {"turn_failed": "failed", "turn_aborted": "aborted"}.get((terminal or {}).get("type"), "incomplete"))
    return {
        "turn_id": projected["turn_id"],
        "request_id": evs[0]["request_id"] if evs else "",
        "started_at": (started or (evs[0] if evs else {})).get("ts"),
        "ended_at": terminal["ts"] if terminal else None,
        "outcome": outcome,
        "agent_type": completed.get("agent_type", ""),
        "intent": projected["meta"].get("intent", ""),
        "handoff": bool(completed.get("handoff")),
        "ticket_id": projected["result"].get("ticket_id"),
        "latency_ms": completed.get("latency_ms"),
        "model_calls": sum(1 for e in evs if e["type"] == "model_call"),
        "complete": projected["complete"],
        "redacted": projected["redacted"],
    }


def mismatches(projected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """影子校验：投影里有、响应里也有、两边不一样的字段名。只报名字，不报值（值里有原文）。"""
    return sorted(k for k, v in projected.items() if k in actual and actual[k] != v)
