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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from app.agents.agent_orchestrator import Request
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
    from app.agents.agent_orchestrator import Request as OrcReq

    prefetch = deps.prefetch
    report_stage(progress_cb, STAGE_UNDERSTANDING)

    # 1. 读取记忆上下文
    mem_ctx = await deps.load_memory(user_id, conv_id, query=message)

    # 2. 对话历史，用于意图识别上下文
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    intent_result = await deps.recognize_intent(message, history=history)

    knowledge_trace: dict[str, Any] = {}
    data_trace: dict[str, Any] = {}
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
    else:
        # 接下来要查什么，动手之前就定得下来：两个门控都是同步的纯判断，和下面两个
        # 构建函数里用的是同一对函数。两路是并发跑的，所以合成一条进度报出去，
        # 不拆成两条——拆开的话先报的那条会被界面当成"已经做完了"。
        searching = prefetch.will_search(message, intent=intent_result.intent)
        querying = prefetch.will_query(
            message, intent=intent_result.intent, entities=intent_result.entities)
        if searching or querying:
            report_stage(progress_cb, STAGE_GATHERING, knowledge=searching, order_data=querying)

        # 规则通道命中说明意图已经确定，检索这边也跟着走短链路：
        # 直接检索，跳过"查询改写 + LLM 重排"两次额外的 LLM 往返。
        # 知识检索和业务数据查询互不依赖，并发跑。串起来的话查库那 17.7s（契约 §2）
        # 会整段叠在检索之后，用户等待时间白白多出一截。
        (knowledge_text, knowledge_used), (order_text, order_is_evidence) = await asyncio.gather(
            prefetch.knowledge_context(
                message,
                intent=intent_result.intent,
                fast_path=intent_result.fast_path,
                trace=knowledge_trace,
            ),
            prefetch.order_data_context(
                message,
                intent=intent_result.intent,
                entities=intent_result.entities,
                user_id=user_id,
                conv_id=conv_id,
                progress_cb=progress_cb,
                trace=data_trace,
            ),
        )
    context_parts = [mem_ctx.to_prompt_text()]
    if knowledge_text:
        context_parts.append(knowledge_text)
    if order_text:
        context_parts.append(order_text)
    full_context = "\n\n".join(part for part in context_parts if part)

    orch_req = OrcReq(
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
    evidence_trace = {"knowledge": knowledge_trace, "business_data": data_trace}
    return PreparedTurn(orch_req, intent_result, knowledge_used, evidence_trace)
