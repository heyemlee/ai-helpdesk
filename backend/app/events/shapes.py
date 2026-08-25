"""
几类事件的形状：哪些字段算结构（data），哪些算原文（body）。

发事件的一方（准备阶段、专家）和投影的一方（projection.py）共用这一份，免得两边各写各的、
慢慢漂开。分法的原则见 recorder.py 开头：任何来自客户、模型、知识库、业务系统或异常信息
的文字都放 body。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 工具调用的经过（trace）里属于外来文字的字段：下游给的失败原因、可信校验的理由
TRACE_TEXT_KEYS = ("reason", "reasons")
# 知识片段里属于外来文字的字段：标题和摘录。doc_id、chunk_id、分数这些留在结构里
SOURCE_TEXT_KEYS = ("title", "doc_title", "excerpt")


def sha256(value: Any) -> str:
    """文本或可以序列化的结构的哈希。重放时拿它判断重建出来的输入和当时的是不是同一份。"""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def agent_type_of(key: str) -> str:
    """专家实例键 → 类型：order_1 → order。事件里记实例，轨迹和响应里报类型。"""
    base, sep, suffix = (key or "").rpartition("_")
    return base if sep and suffix.isdigit() else key


def split_trace(trace: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """工具调用的经过 → (结构, 原文)。merge_trace 是它的逆。"""
    structure = {k: v for k, v in trace.items() if k not in TRACE_TEXT_KEYS and k != "sources"}
    text = {k: trace[k] for k in TRACE_TEXT_KEYS if k in trace}
    if "sources" in trace:
        sources = [s for s in trace.get("sources") or [] if isinstance(s, dict)]
        structure["sources"] = [{k: v for k, v in s.items() if k not in SOURCE_TEXT_KEYS} for s in sources]
        text["excerpts"] = [{k: s[k] for k in SOURCE_TEXT_KEYS if k in s} for s in sources]
    return structure, text


def merge_trace(structure: dict[str, Any], text: dict[str, Any] | None) -> dict[str, Any]:
    """split_trace 的逆。原文已经清空时（text 为 None）只剩结构：片段没有标题和摘录。"""
    trace = dict(structure)
    text = text or {}
    for key in TRACE_TEXT_KEYS:
        if key in text:
            trace[key] = text[key]
    if "sources" in structure:
        excerpts = text.get("excerpts") or []
        trace["sources"] = [
            {**source, **(excerpts[i] if i < len(excerpts) else {})}
            for i, source in enumerate(structure["sources"])
        ]
    return trace


def tool_call_parts(
    record: dict[str, Any], *, idem_key: str, content: str = "", raw: dict[str, Any] | None = None,
    evidence: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    轨迹里的一条 → tool_call 事件的 (data, body)。

    轨迹条目和事件由同一个 dict 生成：信封里的 step、call_id、agent，加上 data，再加上
    body 里的 args 和 trace 的文字字段，就是响应里那一条，一个字段不差（record_from_event）。
    content 是交给模型读的文本，raw 是业务数据的归一化结果：都只在日志里，不进响应。
    evidence 是加进答案守卫出处的那段，和 content 不一样时（复用的结果前面多一句说明）才另存，
    重放守卫要用它。
    """
    structure, text = split_trace(record.get("trace") or {})
    data = {
        "actor": record["actor"], "tool": record["tool"], "outcome": record["outcome"],
        "is_error": record["is_error"], "evidence": record["evidence"], "dedup": record["dedup"],
        "latency_ms": record["latency_ms"], "idem_key": idem_key, "trace": structure,
    }
    body: dict[str, Any] = {"args": record.get("args") or {}, "content": content, **text}
    if raw is not None:
        body["raw"] = raw
    if evidence and evidence != content:
        body["evidence"] = evidence
    return data, body


def record_from_event(event: dict[str, Any]) -> dict[str, Any]:
    """tool_call 事件（解开之后的样子）→ 响应 trajectory 里的那一条。"""
    data, body = event["data"], event.get("body")
    agent = event.get("agent") or ""
    return {
        "step": event["step"], "call_id": event["call_id"], "actor": data["actor"],
        "agent": agent_type_of(agent) if agent else None, "tool": data["tool"],
        "args": (body or {}).get("args", {}), "outcome": data["outcome"],
        "is_error": data["is_error"], "evidence": data["evidence"], "dedup": data["dedup"],
        "latency_ms": data["latency_ms"], "trace": merge_trace(data.get("trace") or {}, body),
    }
