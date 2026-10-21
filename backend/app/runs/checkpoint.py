"""
提交点里的编排结果：OrchestratorResult ↔ JSON。

提交点要存的是"这一轮回答成了什么样"：作答的 Agent、回答原文、转没转人工、为什么、
置信度、拦下的违规表述……接手的那一遍照着它落副作用、拼响应，所以字段要全，和直接
跑完的那一遍用的是同一个结果对象。枚举存值，违规表述存四个字段。
"""
from __future__ import annotations

from typing import Any


def encode_result(result) -> dict[str, Any]:
    return {
        "request_id": result.request_id,
        "response": result.response,
        "agent_type": result.agent_type.value,
        "intent": result.intent.value if result.intent is not None else None,
        "escalated": result.escalated,
        "latency_ms": result.latency_ms,
        "agent_types": [a.value for a in result.agent_types],
        "primary_agent": result.primary_agent.value if result.primary_agent is not None else None,
        "supporting_agents": [a.value for a in result.supporting_agents],
        "routing_reason": result.routing_reason,
        "routing_confidence": result.routing_confidence,
        "handoff": result.handoff,
        "handoff_reason": result.handoff_reason,
        "answer_confidence": result.answer_confidence,
        "needs_clarification": result.needs_clarification,
        "policy_violations": [
            {"rule": v.rule, "evidence": v.evidence, "sentence": v.sentence, "reason": v.reason}
            for v in result.policy_violations
        ],
        "service_unavailable": result.service_unavailable,
        "unavailable_reason": result.unavailable_reason,
        "trajectory": result.trajectory,
        "answer_usage": result.answer_usage,
    }


def decode_result(data: dict[str, Any]):
    from app.agents.agent_orchestrator import AgentType, OrchestratorResult
    from app.core.answer_policy import PolicyViolation
    from app.core.intent_recognizer import IntentCategory

    def agent(value):
        return AgentType(value) if value is not None else None

    return OrchestratorResult(
        request_id=data.get("request_id", ""),
        response=data.get("response", ""),
        agent_type=AgentType(data["agent_type"]),
        intent=IntentCategory(data["intent"]) if data.get("intent") is not None else None,
        escalated=bool(data.get("escalated")),
        latency_ms=float(data.get("latency_ms") or 0.0),
        agent_types=[AgentType(a) for a in data.get("agent_types") or []],
        primary_agent=agent(data.get("primary_agent")),
        supporting_agents=[AgentType(a) for a in data.get("supporting_agents") or []],
        routing_reason=data.get("routing_reason", ""),
        routing_confidence=float(data.get("routing_confidence") or 0.0),
        handoff=bool(data.get("handoff")),
        handoff_reason=data.get("handoff_reason", ""),
        answer_confidence=float(data.get("answer_confidence", 1.0)),
        needs_clarification=bool(data.get("needs_clarification")),
        policy_violations=[PolicyViolation(**v) for v in data.get("policy_violations") or []],
        service_unavailable=bool(data.get("service_unavailable")),
        unavailable_reason=data.get("unavailable_reason", ""),
        trajectory=list(data.get("trajectory") or []),
        answer_usage=dict(data.get("answer_usage") or {}),
    )
