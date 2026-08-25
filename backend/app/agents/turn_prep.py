"""
准备阶段：读记忆 → 意图识别 → 规则预取（知识库 ∥ 业务数据）→ 组装编排请求。

/chat 和 /chat/stream 共用这一个函数。评测要复刻 /chat 的准备阶段时也该调它，
而不是自己再写一遍——两份实现迟早会漂开，评测量的就不再是线上那条路。

依赖全部由调用方传进来（`TurnDeps`），这里不读任何模块级单例：api/main.py 按当前
进程的单例装配一份，别处按自己的工具和桩装配一份。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from app.events import events
from app.events.recorder import TurnRecorder
from app.events.shapes import tool_call_parts

if TYPE_CHECKING:
    from app.agents.agent_orchestrator import Request
    from app.agents.tool_loop import LoopSetup, TurnContext
    from app.tools.turn_tools import Prefetch

logger = logging.getLogger(__name__)


# ── 准备阶段的进度 ────────────────────────────────────────────────────────────
#
# 意图识别、检索改写、重排各是一次模型往返，加起来十几秒到一分钟。这段时间流里
# 以前什么都不发——只有委派查库走 A2A 时，下游的阶段才会透传上来。于是只问知识的
# 问题（最常见的一类）在出第一个字之前，页面上只有一句不动的"正在处理"，
# 看不出是在干活还是卡住了。
#
# 阶段名带 `helpdesk.` 前缀，和下游透传的阶段名（契约 §4.5）分开，两边各改各的
# 不会撞。`label` 是给不认识这个阶段的旧前端看的；新前端按 stage 自己出文案，
# 跟着界面语言走。
STAGE_UNDERSTANDING = "helpdesk.understanding"
STAGE_GATHERING = "helpdesk.gathering"
_STAGE_LABEL = {
    STAGE_UNDERSTANDING: "正在理解问题",
    STAGE_GATHERING: "正在查找回答需要的资料",
}


def report_stage(progress_cb, stage: str, **detail: Any) -> None:
    """把"现在在做哪一步"放进进度队列。非流式没有队列（progress_cb 为 None），什么都不做。"""
    if progress_cb is None:
        return
    try:
        progress_cb({"stage": stage, "label": _STAGE_LABEL.get(stage, ""), **detail})
    except Exception:
        # 进度是增强项：报不出去不能连累这一轮回答
        logger.debug("进度上报失败: %s", stage, exc_info=True)


@dataclass(frozen=True)
class TurnDeps:
    """准备阶段用到的外部依赖。"""

    # (user_id, conv_id, query=...) → MemoryContext：工作记忆、会话摘要、情景记忆、画像
    load_memory: Callable[..., Awaitable[Any]]
    # (message, history=...) → 意图识别结果
    recognize_intent: Callable[..., Awaitable[Any]]
    # 模型服务此刻被保护挡着（不出网）时返回原因，可用时返回空串
    outage_reason: Callable[[], str]
    # 规则预取那两路查询，绑定在这一份工具上
    prefetch: Prefetch
    # 工具循环：打开时是这一轮能用的工具和边界，编排请求上会挂一份 TurnContext；
    # 关着（默认）为 None，专家照旧只做一次生成
    loop: LoopSetup | None = None


class PreparedTurn(NamedTuple):
    """准备阶段的产出。也可以按元组解包：(编排请求, 意图结果, 是否用到知识库, 依据记录)。"""

    request: Request
    intent: Any
    knowledge_used: bool
    evidence: dict[str, Any]


async def prepare_turn(
    message: str,
    *,
    user_id: str,
    conv_id: str,
    request_id: str,
    deps: TurnDeps,
    progress_cb=None,
) -> PreparedTurn:
    """
    /chat 和 /chat/stream 共用的准备阶段：
      读取三级记忆 → 意图识别 → 按需 RAG → 组装编排请求

    抽出来是为了让流式和非流式共用同一套上下文构造——两条链路一旦漂移，
    就会出现"流式和非流式对同一个问题给出不同答案"的问题。

    依据记录是 `{"knowledge": {...}, "business_data": {...}}`：这一轮检索到了
    哪几段知识、业务数据委派有没有发起、结果算不算数。它们原样进响应，
    工作台显示的"回答依据"和学习模式的讲解都只来自这里——前端自己推断不出
    这一轮有没有查库，推断出来的也不能当事实显示。
    """
    prefetch = deps.prefetch
    recorder = events()
    report_stage(progress_cb, STAGE_UNDERSTANDING)

    # 1. 读取记忆上下文
    mem_ctx = await deps.load_memory(user_id, conv_id, query=message)

    # 2. 对话历史，用于意图识别上下文
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None
    memory_text = mem_ctx.to_prompt_text()
    _record_memory(recorder, mem_ctx, memory_text, history)

    intent_result = await deps.recognize_intent(message, history=history)
    _record_intent(recorder, intent_result)

    knowledge_trace: dict[str, Any] = {}
    data_trace: dict[str, Any] = {}
    latency_ms: dict[str, float] = {}
    outage = deps.outage_reason()
    if outage:
        # 模型服务不可用：这一轮作答不了（编排器会直接返回"不可用"，或按规则转人工），
        # 检索和委派查库拿回来的东西没有人读。尤其是委派——它会让下游为一个注定
        # 用不上的答案把自己的模型跑一遍。
        knowledge_text, knowledge_used, order_text, order_is_evidence = "", False, "", False
        knowledge_trace.update(attempted=False, mode="", sources=[], reranked=False,
                               corrected=False, evidence_ok=None, degraded=False)
        data_trace.update(enabled=prefetch.data_enabled,
                          attempted=False, outcome="model_unavailable", reason=outage)
        recorder.emit("prefetch_planned", {
            "knowledge": False, "order_data": False, "mode": "", "data_enabled": prefetch.data_enabled,
            "skipped": f"model_unavailable:{outage}", "not_queried": dict(data_trace),
        })
    else:
        # 接下来要查什么，动手之前就定得下来：两个门控都是同步的纯判断，和下面两个
        # 构建函数里用的是同一对函数。两路是并发跑的，所以合成一条进度报出去，
        # 不拆成两条——拆开的话先报的那条会被界面当成"已经做完了"。
        searching = prefetch.will_search(message, intent=intent_result.intent)
        querying = prefetch.will_query(
            message, intent=intent_result.intent, entities=intent_result.entities)
        if searching or querying:
            report_stage(progress_cb, STAGE_GATHERING, knowledge=searching, order_data=querying)
        recorder.emit("prefetch_planned", {
            "knowledge": searching, "order_data": querying,
            "mode": ("direct" if intent_result.fast_path else "rewrite_rerank") if searching else "",
            "data_enabled": prefetch.data_enabled, "skipped": "",
            **({} if querying else {"not_queried": prefetch.not_queried()}),
        })

        # 规则通道命中说明意图已经确定，检索这边也跟着走短链路：
        # 直接检索，跳过"查询改写 + LLM 重排"两次额外的 LLM 往返。
        # 知识检索和业务数据查询互不依赖，并发跑。串起来的话查库那 17.7s（契约 §2）
        # 会整段叠在检索之后，用户等待时间白白多出一截。
        raw: dict[str, Any] = {}
        (knowledge_text, knowledge_used), (order_text, order_is_evidence) = await asyncio.gather(
            _rule_call(recorder, "knowledge_search", searching, message, latency_ms, prefetch.knowledge_context(
                message,
                intent=intent_result.intent,
                fast_path=intent_result.fast_path,
                trace=knowledge_trace,
            ), trace=knowledge_trace),
            _rule_call(recorder, "order_data_query", querying, message, latency_ms, prefetch.order_data_context(
                message,
                intent=intent_result.intent,
                entities=intent_result.entities,
                user_id=user_id,
                conv_id=conv_id,
                progress_cb=progress_cb,
                trace=data_trace,
                raw=raw,
            ), trace=data_trace, raw=raw),
        )
    orch_req = build_request(
        message, user_id=user_id, conv_id=conv_id, request_id=request_id, memory_text=memory_text,
        history=history, intent_result=intent_result, knowledge_text=knowledge_text,
        order_text=order_text, order_is_evidence=order_is_evidence,
    )
    if deps.loop is not None:
        orch_req.turn = _turn_context(
            message, user_id, conv_id, intent_result, progress_cb, deps.loop,
            knowledge=(knowledge_text, knowledge_used, knowledge_trace),
            order=(order_text, order_is_evidence, data_trace),
            latency_ms=latency_ms,
        )
    evidence_trace = {"knowledge": knowledge_trace, "business_data": data_trace}
    return PreparedTurn(orch_req, intent_result, knowledge_used, evidence_trace)


def build_request(
    message: str,
    *,
    user_id: str,
    conv_id: str,
    request_id: str,
    memory_text: str,
    history: list[dict[str, str]] | None,
    intent_result: Any,
    knowledge_text: str,
    order_text: str,
    order_is_evidence: bool,
) -> Request:
    """
    准备阶段的产物 → 编排请求。上下文是记忆、知识、业务数据三段拼起来的；出处只算检索到的
    知识和真的查回来的业务数据。

    抽出来是为了让重放（app/events/replay.py）用同一个函数从事件重建请求：两份拼法迟早会
    漂开，而重放靠 agent_started 里记的哈希自证"重建出来的就是当时那一份"。
    """
    from app.agents.agent_orchestrator import Request as OrcReq

    context_parts = [memory_text]
    if knowledge_text:
        context_parts.append(knowledge_text)
    if order_text:
        context_parts.append(order_text)
    full_context = "\n\n".join(part for part in context_parts if part)

    return OrcReq(
        message=message,
        user_id=user_id,
        conv_id=conv_id,
        request_id=request_id,
        context=full_context,
        history=history,
        entities=intent_result.entities,
        intent=intent_result.intent,
        intent_group=intent_result.intent_group,
        urgency=intent_result.urgency,
        intent_confidence=intent_result.confidence,
        # 意图那一路模型没答上来时，这个意图是降级凑出来的——编排器据此不去追问
        intent_degraded=bool(getattr(intent_result, "llm_failed", False)),
        # 只有检索到的知识和**真的查回来的**业务数据才算"出处"，记忆和历史不能
        # 作为价格/时效的依据。业务数据必须在这里——否则真实交期会被答案策略
        # 当成无依据承诺替换掉。反过来，委派失败时那段降级提示进的是上下文而
        # 不是这里：它是指令不是证据，`order_is_evidence` 就是这个开关。
        # 见 tools/turn_tools.delegate_order_query 的说明。
        grounding_sources=[
            t for t in (knowledge_text, order_text if order_is_evidence else "") if t
        ],
    )


def _turn_context(
    message: str,
    user_id: str,
    conv_id: str,
    intent_result: Any,
    progress_cb: Callable[[dict[str, Any]], Any] | None,
    loop: LoopSetup,
    *,
    knowledge: tuple[str, bool, dict[str, Any]],
    order: tuple[str, bool, dict[str, Any]],
    latency_ms: dict[str, float] | None = None,
) -> TurnContext:
    """工具循环这一轮共用的上下文：可信身份、可用工具，轨迹和去重表从规则预取开始。"""
    from app.agents.tool_loop import TurnContext
    from app.tools.turn_tools import seed_from_prefetch

    intent = getattr(intent_result, "intent", None)
    turn = TurnContext(
        user_id=user_id,
        conv_id=conv_id,
        tools=dict(loop.tools),
        config=loop.config,
        intent=getattr(intent, "value", intent),
        progress_cb=progress_cb,
    )
    seed_from_prefetch(turn, message, knowledge=knowledge, order=order, latency_ms=latency_ms)
    return turn


# ── 事件日志（app/events/）────────────────────────────────────────────────────

def _record_memory(recorder: TurnRecorder, mem_ctx: Any, text: str, history: list[dict] | None) -> None:
    """memory_loaded：结构里只有条数和字数，原文（记忆上下文、给意图识别的最近 5 条）放 body。"""
    recorder.emit("memory_loaded", {
        "recent": len(getattr(mem_ctx, "recent_messages", None) or []),
        "summary": bool(getattr(mem_ctx, "summary", "")),
        "history": len(getattr(mem_ctx, "relevant_history", None) or []),
        "profile_keys": len(getattr(mem_ctx, "user_profile", None) or {}),
        "chars": len(text),
    }, {"text": text, "recent": history or []})


def _record_intent(recorder: TurnRecorder, result: Any) -> None:
    """
    intent_voted。意图命中进程内缓存时没有模型调用（cached=true），读的一方别把"缺一次
    intent 的 model_call"当成漏记。实体值和 reasoning 是外来文字，放 body；结构里只有各类
    实体的个数。置信度和耗时记原值：重放要用原值重算路由和门槛，取整由投影按响应的口径做。
    """
    intent = getattr(result, "intent", None)
    urgency = getattr(result, "urgency", None)
    entities = getattr(result, "entities", None) or {}
    recorder.emit("intent_voted", {
        "intent": getattr(intent, "value", intent) or "other",
        "group": getattr(result, "intent_group", ""),
        "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
        "urgency": getattr(urgency, "name", "").lower() if urgency is not None else "",
        "strategy": getattr(result, "strategy", "ensemble"),
        "source_scores": dict(getattr(result, "source_scores", None) or {}),
        "llm_failed": bool(getattr(result, "llm_failed", False)),
        "llm_unavailable": getattr(result, "llm_unavailable", "") or "",
        "latency_ms": float(getattr(result, "latency_ms", 0.0) or 0.0),
        "cached": bool(getattr(result, "cached", False)),
        "entity_kinds": {kind: len(values) for kind, values in entities.items() if values},
    }, {"entities": entities, "reasoning": getattr(result, "reasoning", "") or ""})


async def _rule_call(
    recorder: TurnRecorder,
    tool: str,
    planned: bool,
    message: str,
    latency_ms: dict[str, float],
    call: Awaitable[tuple[str, bool]],
    *,
    trace: dict[str, Any],
    raw: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """
    规则预取的一路：查之前发 tool_started，查完发 tool_call，量下实际耗时。

    没轮到查的那一路照旧调一次（它只是把 trace 填成"没查"），不发事件。tool_call 和轨迹
    里的那一条由同一个函数生成（turn_tools.rule_record），循环关着也记：日志记的是发生了
    什么，不看开关。
    """
    from app.tools.turn_tools import rule_outcome, rule_record

    if not planned:
        return await call
    idem_key = f"{recorder.turn_id}:rule:{tool}"
    recorder.emit("tool_started", {"actor": "rule", "tool": tool, "decision": "execute", "idem_key": idem_key},
                  {"args": {"query": message}}, step=0)
    started = time.monotonic()
    text, flag = await call
    latency_ms[tool] = round((time.monotonic() - started) * 1000, 1)
    outcome = rule_outcome(tool, text, flag, trace)
    data, body = tool_call_parts(rule_record(tool, message, outcome, latency_ms[tool]), idem_key=idem_key,
                                 content=outcome.content, raw=raw or None, evidence=outcome.evidence)
    if text != outcome.content:
        # 放进专家上下文的那段和"交给模型读的结果"不总是同一段（没查到时前者是空的）：
        # 重放要重建出一模一样的上下文，两段都留
        body["context"] = text
    recorder.emit("tool_call", data, body, step=0)
    return text, flag
