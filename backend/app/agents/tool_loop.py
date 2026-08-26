"""
Answer 阶段的工具循环：专家读完规则预取的资料后，还可以自己调用只读工具补资料。

形状照 pi-agent-core：一步 = 一次模型调用 + 这次调用要的工具；模型不再调工具就结束。
循环只管"调模型、执行工具、把结果交回去"，不碰路由、答案守卫和转人工——那些在
BaseAgent 和编排器里照旧做。具体是哪些工具、怎么查、算不算出处，由 LoopTool 的
实现决定（见 tools/turn_tools.py）。

边界（LoopBudget）：

  - 每轮最多 `max_model_calls` 次模型调用，最后一次传 `tool_choice=none`，强制作答；
  - 工具调用总数、慢工具（委派查库）的次数、墙钟时间各有上限，超了就拒绝这次调用，
    把原因作为结果告诉模型，而不是悄悄不执行；墙钟上限没配时按能用的慢工具推算
    （deadline_for），保证查库在这套超时下调得动；
  - 这一轮已经知道查不成的工具（比如规则预取刚委派过、下游超时）不再调，原因同样交还模型；
  - 工具的任何失败都变成一条 is_error 的结果交还模型，不往上抛；
  - 同一轮里参数相同的调用只执行一次（并行的两个专家也共用），规则预取过的那次也算。

只依赖客户端 `messages.stream` 的两个入口：`text_stream` 和 `get_final_message()`。
anthropic SDK 原生就有，OpenAI 兼容端点由 core/openai_compat 翻成同样的形状。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.telemetry import span
from app.events.recorder import model_call_tag

logger = logging.getLogger(__name__)

_ON = {"1", "true", "yes", "on"}

# 每类专家默认能用的工具。general 只查知识库；technical 也能查业务数据——轨迹 A/B
# 里，交给它的破损、缺件问题常带着柜号或订单号（"W3012 开裂了，这批哪天出厂的"），只查知识库
# 就只能让客户等人工。账款那边的报价意图另有策略门（见 turn_tools.OrderDataQueryTool）。
DEFAULT_ALLOW: dict[str, tuple[str, ...]] = {
    "general": ("knowledge_search",),
    "technical": ("knowledge_search", "order_data_query"),
    "billing": ("knowledge_search", "order_data_query"),
    "order": ("knowledge_search", "order_data_query"),
}

# 慢工具开跑前，剩余时间除了它自己的超时还得留出这么多给最后一步作答。
ANSWER_RESERVE_S = 15.0

# 墙钟上限没有显式配置时，按最慢的慢工具推算：它的超时 + 作答预留 + 这么多。这一段留给
# 调它之前的模型调用和快工具——通常是一两步，每步几秒。
LEAD_IN_S = 20.0

# 没有慢工具时的墙钟上限，也是推算值的下限。
DEFAULT_DEADLINE_S = 75.0


@dataclass(frozen=True)
class LoopBudget:
    max_model_calls: int = 3     # 含最后一次作答
    max_tool_calls: int = 4      # 整轮；去重命中的不算
    max_parallel: int = 2        # 同一步里并发执行的工具数
    max_delegations: int = 1     # 慢工具（委派查库）整轮最多几次
    # 循环部分的墙钟上限，不含准备阶段。None = 按这个专家能用的慢工具推算（deadline_for）
    deadline_s: float | None = None


def _parse_allow(raw: str) -> dict[str, tuple[str, ...]]:
    """`order=knowledge_search,order_data_query;general=` → 在默认白名单上逐个专家覆盖。"""
    allow = dict(DEFAULT_ALLOW)
    for entry in raw.split(";"):
        agent, sep, names = entry.partition("=")
        if not sep or not agent.strip():
            continue
        allow[agent.strip()] = tuple(n.strip() for n in names.split(",") if n.strip())
    return allow


@dataclass(frozen=True)
class LoopConfig:
    """工具循环的边界和每类专家的白名单。"""

    budget: LoopBudget = field(default_factory=LoopBudget)
    allow: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_ALLOW))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LoopConfig | None:
        """`AI_HELPDESK_TOOL_LOOP` 没打开时返回 None——默认关闭，专家照旧只做一次生成。"""
        env = os.environ if env is None else env
        if env.get("AI_HELPDESK_TOOL_LOOP", "").strip().lower() not in _ON:
            return None
        deadline = env.get("AI_HELPDESK_TOOL_LOOP_DEADLINE_S", "").strip()
        budget = LoopBudget(
            max_model_calls=max(1, int(env.get("AI_HELPDESK_TOOL_LOOP_MAX_CALLS", "3"))),
            max_tool_calls=max(0, int(env.get("AI_HELPDESK_TOOL_LOOP_MAX_TOOL_CALLS", "4"))),
            max_delegations=max(0, int(env.get("AI_HELPDESK_TOOL_LOOP_MAX_DELEGATIONS", "1"))),
            deadline_s=float(deadline) if deadline else None,
        )
        return cls(budget=budget, allow=_parse_allow(env.get("AI_HELPDESK_TOOL_LOOP_TOOLS", "")))


@dataclass
class ToolOutcome:
    """一次工具调用交还给模型的结果。"""

    content: str                 # 交给模型读的文本
    is_error: bool = False
    evidence: str = ""           # 能当出处的文本：非空才进答案守卫的 sources
    trace: dict[str, Any] = field(default_factory=dict)   # 给工作台和评测看的经过
    outcome: str = ""            # 一个词的结局，进轨迹：found / empty / evidence / failed ...
    # 业务数据的归一化结果：只进事件日志（重放和导出用例时当桩），不进轨迹、不交给模型
    raw: dict[str, Any] | None = None


def _normalize(value: Any) -> Any:
    return " ".join(value.split()).casefold() if isinstance(value, str) else value


class LoopTool:
    """
    循环里模型能调的工具。子类给出名字、说明、参数模型和执行。

    `params` 只放模型该填的东西。身份之类的可信参数从 TurnContext 取，不出现在
    模型看得到的 schema 里，提示注入也就改不了它们。

    `slow` 的工具一次可能要几十秒（委派查库）：计入 `max_delegations`，剩余时间不够
    `timeout_s` 再加一步作答时不让调。
    """

    name: str = ""
    description: str = ""
    params: type[BaseModel] = BaseModel
    slow: bool = False
    timeout_s: float = 0.0

    def spec(self) -> dict[str, Any]:
        """给模型看的工具定义（anthropic 的形状；OpenAI 侧由兼容层翻译）。"""
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}

    def memo_key(self, args: BaseModel) -> tuple:
        """同一轮里"同一次查询"的判据：工具名加规范化后的参数。"""
        return (self.name, *(_normalize(v) for v in args.model_dump().values()))

    def refuse(self, turn: TurnContext) -> str:
        """策略门：这一轮不许调时返回交给模型的原因；可以调就返回空串。"""
        return ""

    def skip(self, turn: TurnContext) -> str:
        """
        这一轮已经知道查不成时（比如下游刚超时）返回交给模型的原因；照常调就返回空串。

        和 refuse 分开：refuse 是"不许查"，这里是"查了也没用"，轨迹里记 skipped，
        评测和工作台不会把它当成越权。
        """
        return ""

    async def run(self, args: BaseModel, turn: TurnContext) -> ToolOutcome:
        raise NotImplementedError


# ── 墙钟上限 ──────────────────────────────────────────────────────────────────

def slow_tool_deadline_s(tools: Iterable[LoopTool]) -> float:
    """最慢的慢工具在前面留出 LEAD_IN_S 之后还调得动所需的墙钟上限；没有慢工具时为 0。"""
    timeouts = [t.timeout_s for t in tools if t.slow]
    return max(timeouts) + ANSWER_RESERVE_S + LEAD_IN_S if timeouts else 0.0


def deadline_for(budget: LoopBudget, tools: Iterable[LoopTool]) -> float:
    """
    一轮循环的墙钟上限：显式配了就用配的，没配就按这个专家能用的慢工具推算。

    慢工具的准入条件是剩余时间 ≥ 它的超时 + ANSWER_RESERVE_S（见 _plan_call）。原来固定
    75 秒，在 SQL_AGENT_TIMEOUT_S=90（工具超时 92 秒）时这个条件永远满足不了，模型发起的
    查库一次也跑不成——轨迹试跑就是这样。只能查知识库的专家没有慢工具，
    仍是 75 秒。
    """
    if budget.deadline_s is not None:
        return budget.deadline_s
    return max(DEFAULT_DEADLINE_S, slow_tool_deadline_s(tools))


def deadline_warnings(config: LoopConfig, tools: Iterable[LoopTool]) -> list[str]:
    """
    显式配的墙钟上限让慢工具调不动、或者只在前面几步很快时才调得动，启动时的提醒。

    不拦启动：循环照样能用，只是模型发起的那类查询会被拒。没配上限时按工具推算，
    没有这个问题。
    """
    deadline = config.budget.deadline_s
    if deadline is None:
        return []
    warnings: list[str] = []
    for tool in tools:
        if not tool.slow:
            continue
        need = tool.timeout_s + ANSWER_RESERVE_S
        derived = max(DEFAULT_DEADLINE_S, need + LEAD_IN_S)
        if deadline < need:
            warnings.append(
                f"AI_HELPDESK_TOOL_LOOP_DEADLINE_S={deadline:g} 小于 {tool.name} 的超时 {tool.timeout_s:g} 秒"
                f"加作答预留 {ANSWER_RESERVE_S:g} 秒：模型发起的 {tool.name} 每次都会被拒。"
                f"去掉这个变量就按工具超时推算（{derived:g} 秒）。"
            )
        elif deadline < need + LEAD_IN_S:
            warnings.append(
                f"AI_HELPDESK_TOOL_LOOP_DEADLINE_S={deadline:g} 只给调 {tool.name} 之前的步骤留了 "
                f"{deadline - need:g} 秒（推算值留 {LEAD_IN_S:g} 秒）：前面的模型调用慢一点，"
                f"这次查询就会被拒。去掉这个变量就按工具超时推算（{derived:g} 秒）。"
            )
    return warnings


@dataclass(frozen=True)
class LoopSetup:
    """工具循环打开时，一轮对话能用的工具和边界。准备阶段据此建 TurnContext。"""

    tools: dict[str, LoopTool]
    config: LoopConfig


@dataclass
class TurnContext:
    """
    一轮对话里工具循环共用的东西。准备阶段建好挂在编排请求上（`Request.turn`），
    并行的两个专家拿到的是同一份：去重表和轨迹共享，预算各算各的。

    `user_id` / `conv_id` 是服务端推导出的可信身份：工具需要身份时从这里取。
    """

    user_id: str
    conv_id: str
    tools: dict[str, LoopTool]
    config: LoopConfig
    intent: str | None = None
    progress_cb: Callable[[dict[str, Any]], Any] | None = None
    memo: dict[tuple, asyncio.Future] = field(default_factory=dict)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # 学习模式要说清楚的两件事：有专家被要求直接作答（第一次的原因：model_calls / tool_calls /
    # deadline），或者模型连接不支持工具、这一轮退回了单次生成（tools_unsupported）
    forced: str = ""
    fallback: str = ""

    def tools_for(self, agent: str) -> list[LoopTool]:
        """这个专家这一轮能用的工具：白名单和这个实例有的工具取交集，按名字排序。"""
        allowed = set(self.config.allow.get(agent, ()))
        return [self.tools[name] for name in sorted(allowed) if name in self.tools]

    def remember(self, key: tuple, outcome: ToolOutcome) -> None:
        """记下一次已经有结果的查询（规则预取用），之后同样的调用直接复用。"""
        future = asyncio.get_running_loop().create_future()
        future.set_result(outcome)
        self.memo[key] = future

    def count_usage(self, input_tokens: int, output_tokens: int) -> None:
        """作答阶段的一次模型调用。"""
        self.model_calls += 1
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0

    def usage(self) -> dict[str, int | str]:
        usage: dict[str, int | str] = {"model_calls": self.model_calls, "input_tokens": self.input_tokens,
                                       "output_tokens": self.output_tokens}
        if self.forced:
            usage["forced"] = self.forced
        if self.fallback:
            usage["fallback"] = self.fallback
        return usage


# ── 循环产出的事件 ────────────────────────────────────────────────────────────

@dataclass
class TextDelta:
    step: int
    text: str


@dataclass
class ToolStart:
    step: int
    call_id: str
    name: str
    args: dict[str, Any]
    # 这次调用的去向（_plan_call 定的）：execute / dedup，或者没执行的原因 refused / skipped /
    # budget / invalid_args / unavailable_tool。事件日志据此分清"执行了"和"被预算拒了"
    decision: str = "execute"


@dataclass
class ToolEnd:
    step: int
    call_id: str
    name: str
    outcome: ToolOutcome
    record: dict[str, Any]       # 这次调用进轨迹的那一条


@dataclass
class StepEnd:
    step: int
    stop_reason: str
    called_tools: bool           # 这一步的工具调用会被执行（不是最后一步）
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LoopEnd:
    model_calls: int
    tool_calls: int
    stop_reason: str             # 最后一步的 stop_reason：max_tokens 时调用方要报"预算吃光"
    forced: str = ""             # 被强制作答的原因：model_calls / tool_calls / deadline；正常结束为空


# ── 循环 ──────────────────────────────────────────────────────────────────────

def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


@dataclass
class _Usage:
    tool_calls: int = 0
    delegations: int = 0


@dataclass
class _Call:
    """一次工具调用的去向：拒绝、复用、或者真的执行。"""

    call_id: str
    name: str
    args: dict[str, Any]
    tool: LoopTool | None = None
    parsed: BaseModel | None = None
    refusal: ToolOutcome | None = None
    future: asyncio.Future | None = None     # 要等的结果：自己执行的，或去重复用的
    owner: bool = False                       # 这个调用负责执行、把结果写进 future
    dedup: bool = False
    record: dict[str, Any] = field(default_factory=dict)   # 进轨迹的那一条


def _error(content: str, outcome: str) -> ToolOutcome:
    return ToolOutcome(content=content, is_error=True, outcome=outcome)


def _decision(call: _Call) -> str:
    if call.refusal is not None:
        return call.refusal.outcome
    return "dedup" if call.dedup else "execute"


def _forced_reason(step: int, budget: LoopBudget, usage: _Usage, deadline: float, now: float) -> str:
    if step >= budget.max_model_calls:
        return "model_calls"
    if usage.tool_calls >= budget.max_tool_calls:
        return "tool_calls"
    if now >= deadline:
        return "deadline"
    return ""


def _plan_call(
    block: Any,
    tools: dict[str, LoopTool],
    turn: TurnContext,
    usage: _Usage,
    budget: LoopBudget,
    remaining_s: float,
) -> _Call:
    """
    按模型发出的顺序逐个决定每次调用的去向，预算因此记得确定。

    顺序：工具在不在 → 参数解析和校验 → 策略门 → 去重（复用不花预算）→ 这一轮已知查不成
    → 次数和时间预算。策略门排在去重前面：不许查的东西，即使本轮别处查过，也不交给这个专家。
    "已知查不成"排在去重后面：和先前那次一模一样的查询照样复用那次的结果（它本身就说明了
    没查成），换了说法的才在这里拦下；拦下的不占预算。
    """
    raw_input = _get(block, "input")
    call = _Call(
        call_id=str(_get(block, "id") or ""),
        name=str(_get(block, "name") or ""),
        args=dict(raw_input) if isinstance(raw_input, dict) else {},
    )
    tool = tools.get(call.name)
    if tool is None:
        call.refusal = _error(f"工具 {call.name or '（未命名）'} 在本轮不可用。", "unavailable_tool")
        return call
    call.tool = tool
    parse_error = _get(block, "parse_error")
    if parse_error:
        call.refusal = _error(f"参数没有被执行：{parse_error}", "invalid_args")
        return call
    try:
        call.parsed = tool.params.model_validate(call.args)
    except ValidationError as ex:
        problems = "；".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in ex.errors())
        call.refusal = _error(f"参数不对，没有执行：{problems}", "invalid_args")
        return call
    reason = tool.refuse(turn)
    if reason:
        call.refusal = _error(reason, "refused")
        return call

    key = tool.memo_key(call.parsed)
    if key in turn.memo:
        call.future, call.dedup = turn.memo[key], True
        return call
    reason = tool.skip(turn)
    if reason:
        call.refusal = _error(reason, "skipped")
        return call
    if usage.tool_calls >= budget.max_tool_calls:
        call.refusal = _error("本轮的查询次数已经用完，请根据已有的资料作答。", "budget")
        return call
    if tool.slow and (usage.delegations >= budget.max_delegations
                      or remaining_s < tool.timeout_s + ANSWER_RESERVE_S):
        call.refusal = _error("本轮不能再查业务系统了，请根据已有的资料作答，缺的信息如实告诉客户。", "budget")
        return call

    usage.tool_calls += 1
    if tool.slow:
        usage.delegations += 1
    call.future = asyncio.get_running_loop().create_future()
    call.owner = True
    turn.memo[key] = call.future
    return call


async def _execute(call: _Call, turn: TurnContext, agent: str, limit: asyncio.Semaphore) -> ToolOutcome:
    """执行或等待一次调用，返回交给模型的结果。任何异常都变成 is_error 的结果。"""
    if call.refusal is not None:
        return call.refusal
    if not call.owner:
        original: ToolOutcome = await asyncio.shield(call.future)
        return ToolOutcome(
            content=f"（与本轮之前的查询相同，结果复用）\n{original.content}",
            is_error=original.is_error, evidence=original.evidence,
            trace=original.trace, outcome=original.outcome,
        )
    try:
        async with limit:
            with span("agent.loop.tool", agent=agent, tool=call.name):
                outcome = await call.tool.run(call.parsed, turn)
    except asyncio.CancelledError:
        call.future.cancel()      # 别让复用这次结果的调用一直等下去
        raise
    except Exception as ex:
        logger.warning("工具 %s 执行出错: %s", call.name, ex)
        outcome = _error(f"工具执行出错：{str(ex) or type(ex).__name__}", "error")
    call.future.set_result(outcome)
    return outcome


async def run_tool_loop(
    client: Any,
    *,
    model: str,
    system: Any,
    messages: list[dict[str, Any]],
    tools: list[LoopTool],
    turn: TurnContext,
    agent: str,
    max_tokens: int,
    options: dict[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    call_tag: dict[str, Any] | None = None,
) -> AsyncIterator[TextDelta | ToolStart | ToolEnd | StepEnd | LoopEnd]:
    """
    跑一次有边界的工具循环，边跑边产出事件。

    文字增量原样往外交（TextDelta），守卫、转人工标记由调用方处理。调用方传进来的
    `messages` 不会被改动。模型服务不可用之类的模型调用异常照常往上抛，由调用方按
    现在的"没答完"处理；工具的异常不会抛出来。

    循环自己不发事件日志：调用方把这些产出转成日志事件。`call_tag` 是事件日志给这些
    模型调用打的标签（用途、专家实例），每一步再加上 step。
    """
    budget = turn.config.budget
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]
    messages = list(messages)
    deadline = clock() + deadline_for(budget, tools)
    usage = _Usage()
    limit = asyncio.Semaphore(max(1, budget.max_parallel))
    model_calls = 0

    for step in range(1, budget.max_model_calls + 1):
        forced = _forced_reason(step, budget, usage, deadline, clock())
        with model_call_tag(**(call_tag or {}), step=step):
            request = client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=specs,
                tool_choice={"type": "none"} if forced else {"type": "auto"},
                **(options or {}),
            )
        async with request as stream:
            async for text in stream.text_stream:
                yield TextDelta(step, text)
            final = await stream.get_final_message()

        model_calls += 1
        reply_usage = getattr(final, "usage", None)
        input_tokens = getattr(reply_usage, "input_tokens", 0) or 0
        output_tokens = getattr(reply_usage, "output_tokens", 0) or 0
        turn.count_usage(input_tokens, output_tokens)
        stop_reason = str(getattr(final, "stop_reason", "") or "")
        blocks = list(getattr(final, "content", None) or [])
        calls = [b for b in blocks if _get(b, "type") == "tool_use"]

        yield StepEnd(step, stop_reason, called_tools=bool(calls) and not forced,
                      input_tokens=input_tokens, output_tokens=output_tokens)
        if not calls or forced:
            if calls:
                logger.warning("%s 在必须作答的一步仍然调了工具（%s），不执行，取已有正文",
                               agent, ", ".join(str(_get(c, "name")) for c in calls))
            yield LoopEnd(model_calls, usage.tool_calls, stop_reason, forced)
            return

        remaining = deadline - clock()
        planned = [_plan_call(b, by_name, turn, usage, budget, remaining) for b in calls]
        for call in planned:
            yield ToolStart(step, call.call_id, call.name, call.args, decision=_decision(call))

        started = clock()

        async def _run(call: _Call) -> tuple[_Call, ToolOutcome]:
            return call, await _execute(call, turn, agent, limit)

        outcomes: dict[int, ToolOutcome] = {}
        for done in asyncio.as_completed([_run(call) for call in planned]):
            call, outcome = await done
            outcomes[id(call)] = outcome
            call.record = {
                "step": step, "call_id": call.call_id, "actor": "model", "agent": agent,
                "tool": call.name, "args": call.args, "outcome": outcome.outcome,
                "is_error": outcome.is_error, "evidence": bool(outcome.evidence),
                "dedup": call.dedup, "latency_ms": round((clock() - started) * 1000, 1),
                "trace": outcome.trace,
            }
            yield ToolEnd(step, call.call_id, call.name, outcome, call.record)

        # 轨迹按模型发出的顺序记，结果也按这个顺序交回——OpenAI 要求 tool 消息和调用一一对上
        turn.trajectory.extend(c.record for c in planned)
        messages.append({"role": "assistant", "content": blocks})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.call_id,
             "content": outcomes[id(c)].content or "（没有结果）",
             **({"is_error": True} if outcomes[id(c)].is_error else {})}
            for c in planned
        ]})
