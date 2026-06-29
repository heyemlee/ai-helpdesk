"""
亮点：多 Agent 路由与编排

核心问题：多 Agent 情况下如何做 Routing？

三个专家 Agent + 一个兜底 Agent：
  - TechnicalAgent —— 柜体台面规格、饰面色差、五金调整、安装偏差与破损判定
  - BillingAgent   —— 报价构成、变更加价、定金尾款、账单核对与发票
  - OrderAgent     —— 订单与生产进度、交期、改单取消、缺件补件、送货排期
  - GeneralAgent   —— 不属于任何专家域，或专家执行失败时的兜底

路由策略：
  1. 领域打分（_domain_scores）—— 意图、关键词、结构化实体各自贡献分数，
     得分最高的专家做主 Agent（primary）。
  2. 主 + 辅协作（_route_decision）—— 跨域问题里，分数达到门槛的其他专家
     升为辅助 Agent（supporting），与主 Agent 并行执行后合并结果。
  3. 低置信度澄清 —— 意图落到 OTHER 且置信度不足时先追问，不猜着路由。
  4. 置信度转人工 —— 意图置信度、路由领域分、Agent 自评三者取最弱一环，
     低于阈值直接转人工，不硬答。
  5. 兜底降级 —— 专家 Agent 执行失败时自动降级到 GeneralAgent 重试。

在线表现反馈：
  Monitor 把各 Agent 的成功率/延迟写回 stats.monitor_penalty，
  同类型有多个实例时 _best_agent() 会绕开表现差的实例。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.answer_policy import AnswerPolicy, PolicyStreamGuard, PolicyViolation
from core.text_lang import detect_language
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import (
    ANSWER_MAX_TOKENS,
    LLMClient,
    create_llm_client,
    extract_structured_text,
)
from core.metrics import record_agent_latency, record_llm_tokens
from core.telemetry import span

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL   = "general"    # 通用客服（兜底）
    TECHNICAL = "technical"  # 专家：产品规格与现场安装
    BILLING   = "billing"    # 专家：报价与账款
    ORDER     = "order"      # 专家：订单、改单、补件与交付
    HANDOFF   = "handoff"    # 转人工（不是 LLM Agent，由编排器直接产出）


# 三个专家 Agent —— 领域打分只在这三者之间比较，GENERAL 只做兜底。
SPECIALIST_AGENTS = (AgentType.TECHNICAL, AgentType.BILLING, AgentType.ORDER)

# 对外展示的角色名。内部类名按技术职责命名，对外按业务职责命名，
# 这样产品文档和 UI 里出现的是"收入与合规 Agent"而不是 BillingAgent。
AGENT_DISPLAY_NAMES: dict[AgentType, str] = {
    AgentType.GENERAL:   "展厅接待 Agent",
    AgentType.TECHNICAL: "产品与安装 Agent",
    AgentType.BILLING:   "报价与账款 Agent",
    AgentType.ORDER:     "订单履约 Agent",
    AgentType.HANDOFF:   "运营升级通道",
}


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 和路由决策使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    def record(self, ok: bool, ms: float) -> None:
        """
        一次请求结束时记账。total / success / total_ms 必须在同一时刻更新。

        之前 total 在请求开始时 +1、success 在 LLM 返回后才 +1，中间隔着一整次
        LLM 调用。演示夹具毫秒返回时这个窗口采不中；真模型 8~20s，Monitor 每
        10s 采一次，几乎每次都落在里面——首个请求的窗口里 success_rate 是
        0/1 = 0.000，Monitor 按 < 0.9 报 ERROR，_routing_penalty 再给这个
        一次都没失败过的实例记 0.5 的惩罚，把流量倒给隔壁那个同样在途的实例。
        """
        self.total += 1
        if ok:
            self.success += 1
        self.total_ms += ms

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        """路由评分：成功率高、延迟低的 Agent 得分高。"""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


# 回复语言。此前系统提示词和 SKILL.md 里一条语言指令都没有——中文回复只是模型
# 顺着中文 SOP、中文知识库和 _build_messages 注入的中文脚手架走出来的结果，
# 是巧合不是契约：换个模型、把 SOP 写成双语，它就会漂。
#
# 只约束给客户看的正文。routing_reason / handoff_reason / 策略拦截理由是给
# 控制台操作员看的诊断串，仍然保持中文——接英文客户的操作员多半说中文，
# 这两拨人不是同一批，不该共用一个语言开关。
# 脚手架消息的两套措辞，见 _build_messages
_SCAFFOLD = {
    "zh": {
        "context_label":  "[背景信息]",
        "context_ack":    "好的，我已了解背景信息。",
        "entities_label": "[结构化实体]",
        "entities_ack":   "好的，我会结合这些结构化实体处理。",
    },
    "en": {
        "context_label":  "[Context]",
        "context_ack":    "Understood — I've read the context.",
        "entities_label": "[Extracted entities]",
        "entities_ack":   "Understood — I'll take these entities into account.",
    },
}

_LANGUAGE_RULE = (
    "\n\n[回复语言]\n"
    "始终使用客户提问所用的语言作答：客户用中文问就用中文答，用英文问就用英文答。"
    "知识库、SOP 和上文的背景信息都是中文的，但这只是资料的语言，不是回复的语言——"
    "给英文客户作答时把依据翻译成英文表述，不要因为资料是中文就改用中文回复。"
)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # 是否需要升级
    policy_violations: list[PolicyViolation] = field(default_factory=list)


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     list[dict[str, str]] | None = None  # 对话历史，传给意图识别
    entities:    dict[str, list[str]] = field(default_factory=dict)
    intent:      IntentCategory | None = None
    intent_group: str | None = None
    urgency:     UrgencyLevel | None   = None
    intent_confidence: float = 1.0
    # 本轮回答允许引用的出处（RAG 检索结果等）。AnswerPolicy 用它判断
    # 回复里的价格/时效是否"有依据"。
    grounding_sources: list[str] = field(default_factory=list)
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      IntentCategory | None
    escalated:   bool  = False
    latency_ms:  float = 0.0
    agent_types: list[AgentType] = field(default_factory=list)
    primary_agent: AgentType | None = None
    supporting_agents: list[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    # 转人工：由置信度不足或明确的转人工意图触发
    handoff:     bool = False
    handoff_reason: str = ""
    answer_confidence: float = 1.0
    needs_clarification: bool = False
    policy_violations: list[PolicyViolation] = field(default_factory=list)


@dataclass
class RoutingDecision:
    """一次请求的结构化路由决策。"""
    primary_agent: AgentType
    supporting_agents: list[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    domain_scores: dict[AgentType, float] = field(default_factory=dict)

    @property
    def agent_types(self) -> list[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 调用、Skill 注入、出站策略校验和统计。"""

    agent_type: AgentType
    system_prompt: str

    def __init__(
        self,
        client: LLMClient,
        model: str,
        skill_manager: Any | None = None,
        policy: AnswerPolicy | None = None,
        replica: int = 0,
    ):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        self._policy = policy or AnswerPolicy()
        self.replica = replica          # 同类型内的实例序号，对应 stats key 的后缀
        self.stats   = AgentStats()

    @property
    def key(self) -> str:
        """Monitor 用来回写路由惩罚的键，例如 technical_1。"""
        return f"{self.agent_type.value}_{self.replica}"

    @property
    def model(self) -> str:
        return self._model

    def _record(self, ok: bool, ms: float) -> None:
        """
        一次请求结束时同时记本地统计和 Prometheus 延迟。

        两者必须在同一处：分开写过一次就会有人只补其中一个，然后 /monitor 的
        avg_ms 和 Grafana 的 p95 各说各话，而且没有任何测试会失败。
        """
        self.stats.record(ok=ok, ms=ms)
        record_agent_latency(self.key, ms)

    async def handle(self, req: Request) -> AgentResponse:
        with span("agent.handle", agent=self.agent_type.value):
            return await self._handle_inner(req)

    async def _handle_inner(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        # 统计只在请求结束时记（stats.record），在途期间不动——见 AgentStats.record
        # Skill SOP 既要注入 prompt，也要作为 AnswerPolicy 判断"有无出处"的依据，
        # 所以这里取一次，两处复用。
        skill_prompt = self._skill_prompt(req)
        try:
            content = await self._call_llm(req, skill_prompt)
            ms = (time.monotonic() - t0) * 1000
            self._record(ok=True, ms=ms)

            # 出站策略校验：拦截无依据的价格/时效承诺
            sources = list(req.grounding_sources)
            if skill_prompt:
                sources.append(skill_prompt)
            policy_result = self._policy.enforce(content, sources)
            if policy_result.blocked:
                logger.warning(
                    "%s 回复触发答案策略拦截 %d 处: %s",
                    self.agent_type.value,
                    policy_result.blocked_count,
                    "; ".join(v.evidence for v in policy_result.violations),
                )

            escalate = self._needs_escalation(policy_result.text)
            return AgentResponse(
                agent_type=self.agent_type,
                content=policy_result.text,
                success=True,
                confidence=self._self_confidence(policy_result.violations, escalate),
                latency_ms=ms,
                escalate=escalate,
                policy_violations=policy_result.violations,
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self._record(ok=False, ms=ms)
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content=_customer_text(req, "agent_error"),
                success=False,
                confidence=0.0,
                latency_ms=ms,
            )

    async def stream(self, req: Request) -> AsyncIterator[dict[str, Any]]:
        """
        流式生成。产出 {"type": "delta"|"end", ...} 事件。

        与 handle() 走同一套策略守卫：文本按句缓冲，整句过一遍 AnswerPolicy
        之后才发出去，保证流式和非流式的答案口径一致。
        统计也和 handle() 同口径，这样 Monitor 看到的成功率不会因为
        用户走了流式接口而失真。
        """
        t0 = time.monotonic()
        skill_prompt = self._skill_prompt(req)

        sources = list(req.grounding_sources)
        if skill_prompt:
            sources.append(skill_prompt)
        guard = PolicyStreamGuard(self._policy, sources)

        full: list[str] = []
        try:
            async for chunk in self._stream_llm(req, skill_prompt):
                safe = guard.feed(chunk)
                if safe:
                    full.append(safe)
                    yield {"type": "delta", "text": safe}

            tail = guard.flush()
            if tail:
                full.append(tail)
                yield {"type": "delta", "text": tail}

            ms = (time.monotonic() - t0) * 1000
            self._record(ok=True, ms=ms)

            content = "".join(full)
            escalate = self._needs_escalation(content)
            if guard.violations:
                logger.warning(
                    "%s 流式回复触发答案策略拦截 %d 处: %s",
                    self.agent_type.value, len(guard.violations),
                    "; ".join(v.evidence for v in guard.violations),
                )
            yield {
                "type": "end",
                "response": AgentResponse(
                    agent_type=self.agent_type,
                    content=content,
                    success=True,
                    confidence=self._self_confidence(guard.violations, escalate),
                    latency_ms=ms,
                    escalate=escalate,
                    policy_violations=guard.violations,
                ),
            }
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self._record(ok=False, ms=ms)
            logger.error(f"{self.agent_type.value} 流式处理失败: {ex}")
            yield {
                "type": "end",
                "response": AgentResponse(
                    agent_type=self.agent_type,
                    content=_customer_text(req, "agent_error"),
                    success=False,
                    confidence=0.0,
                    latency_ms=ms,
                ),
            }

    async def _stream_llm(self, req: Request, skill_prompt: str = "") -> AsyncIterator[str]:
        """调用 LLM 的流式接口，逐段产出文本增量。"""
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=ANSWER_MAX_TOKENS,
            system=self._build_system_prompt(skill_prompt),
            messages=self._build_messages(req),
        ) as stream:
            async for text in stream.text_stream:
                yield text
            # 流结束后取最终消息的 usage 计量 token；失败不影响回复
            try:
                final = await stream.get_final_message()
                usage = getattr(final, "usage", None)
                if usage is not None:
                    record_llm_tokens(
                        self.agent_type.value,
                        getattr(usage, "input_tokens", 0) or 0,
                        getattr(usage, "output_tokens", 0) or 0,
                    )
            except Exception:
                pass

    async def _call_llm(self, req: Request, skill_prompt: str = "") -> str:
        with span("agent.llm_call", agent=self.agent_type.value, model=self._model):
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=ANSWER_MAX_TOKENS,
                system=self._build_system_prompt(skill_prompt),
                messages=self._build_messages(req),
            )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            record_llm_tokens(
                self.agent_type.value,
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
            )
        return extract_structured_text(resp, budget_env="AI_HELPDESK_ANSWER_MAX_TOKENS")

    @staticmethod
    def _build_messages(req: Request) -> list[dict[str, str]]:
        """
        流式和非流式共用同一套消息构造，避免两条链路的上下文出现差异。

        脚手架那两轮的语言跟客户走。它们是**伪造的助手发言**——写死中文等于让
        模型看见自己刚用中文说过两句话，然后再用系统提示词要求它用英文回答英文
        客户，是拿指令去对抗上下文。语言规则压得住（实测没漂），但没必要先制造
        这个对抗再去压。注入的资料本身（req.context 来自中文知识库）保持原样：
        那是依据的语言，不是回复的语言。
        """
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        scaffold = _SCAFFOLD[detect_language(req.message)]

        messages = []
        if req.context:
            messages.append({
                "role": "user",
                "content": f"{scaffold['context_label']}\n{_clean(req.context)}",
            })
            messages.append({"role": "assistant", "content": scaffold["context_ack"]})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({
                "role": "user",
                "content": f"{scaffold['entities_label']}\n{_clean(entities_text)}",
            })
            messages.append({"role": "assistant", "content": scaffold["entities_ack"]})
        messages.append({"role": "user", "content": _clean(req.message)})
        return messages

    def _skill_prompt(self, req: Request) -> str:
        """按 Agent 类型取出本轮命中的 Skill SOP 正文（热加载后立即生效）。"""
        if self._skill_manager is None:
            return ""
        return self._skill_manager.prompt_for(req.message, self.agent_type.value) or ""

    def _build_system_prompt(self, skill_prompt: str = "") -> str:
        """把动态加载的 Skills 拼入 system prompt，让业务规则随请求生效。"""
        prompt = self.system_prompt + _LANGUAGE_RULE
        if not skill_prompt:
            return prompt
        return f"{prompt}\n\n[动态 Skills]\n{skill_prompt}"

    @staticmethod
    def _self_confidence(violations: list[PolicyViolation], escalate: bool) -> float:
        """
        Agent 对本次回答的自评置信度。

        不问模型"你有多确信"（那是不可校准的自述），而是用可观测信号折算：
        触发一次策略拦截说明回答里有编造成分，明说处理不了就是明确的低置信。
        """
        confidence = 1.0
        confidence -= min(0.6, 0.25 * len(violations))
        if escalate:
            confidence -= 0.35
        return round(max(0.0, confidence), 3)

    # 英文那几条是补的：回复语言跟客户走之后，英文回复里说"我处理不了、要转人工"
    # 用的是 hand this over / connect you with 这类说法，一个中文关键词都不含，
    # 于是 escalate 恒为 False——自评置信度不扣分、不建工单，明明说了转人工却没转。
    _ESCALATION_KEYWORDS = (
        "转人工", "人工客服", "无法处理", "处理不了",
        "escalate", "specialist", "hand this over", "hand it over",
        "hand you over", "connect you with", "transfer you", "refer you to",
        "a human agent", "our team will", "cannot resolve", "can't resolve",
        "unable to resolve", "beyond what i can",
    )

    def _needs_escalation(self, content: str) -> bool:
        """检测 Agent 是否建议升级（简单关键词检测，中英双语）。"""
        lowered = content.lower()
        return any(kw.lower() in lowered for kw in self._ESCALATION_KEYWORDS)


class GeneralAgent(BaseAgent):
    agent_type    = AgentType.GENERAL
    system_prompt = (
        "你是橱柜厂销售展厅的接待客服。友好、简洁地回答客户和经销商的问题。"
        "如果问题超出你的能力范围，明确说明并建议转接对应的专业客服。"
    )


class TechnicalAgent(BaseAgent):
    agent_type    = AgentType.TECHNICAL
    system_prompt = (
        "你是橱柜产品与现场安装专家。专注于：柜体和台面的规格、门板饰面与色差、"
        "五金件（铰链、滑轨）调整、现场安装对不齐/关不严、破损瑕疵的判定与返工路径。"
        "提供清晰的步骤化解决方案。需要排返工或到场服务的，说明需要升级处理。"
    )


class BillingAgent(BaseAgent):
    agent_type    = AgentType.BILLING
    system_prompt = (
        "你是报价与账款专家。专注于：橱柜报价构成、变更加价、定金与尾款、账单核对、发票。"
        "对财务问题保持准确和专业。没有拿到正式报价单或系统账目时，不要自己算价、"
        "也不要给出任何具体金额，说明需要销售出具报价单。涉及实际退补款时，说明需要人工审核。"
    )


class OrderAgent(BaseAgent):
    agent_type    = AgentType.ORDER
    system_prompt = (
        "你是订单与交付专家。专注于：订单和生产进度、交期、改单（改尺寸/配置/饰面/取消）、"
        "缺件漏发的补件流程、送货与到场排期。没有查到系统记录时，先说明需要订单号或柜号，"
        "不要凭空描述订单状态。交期以工厂排产实际情况为准，不要给出确定的完工或到货日期。"
    )


# ── 转人工文案 ────────────────────────────────────────────────────────────────
#
# 转人工时会把已经生成的回答保留在前面，避免用户白等一轮。但草稿里那些
# **向用户索取信息的句子**不能一起留下来：Agent 问"具体想咨询什么内容呢？
# 请提供订单号"，是为了它自己接着往下答；这一轮既然已经建单转人工，
# 接着往下办的人换成了同事，用户照着补充也没人接。两段拼在一起就是
# 一条自相矛盾的回复——前半段让用户补资料，后半段说已经转交人工。
#
# 所以转人工时先把索取信息的表述过滤掉，只留有信息量的部分，
# 由固定文案给出唯一一条行动指引：等同事联系，同时把资料准备好。

# 所有会进到 response 正文、被客户读到的固定文案。集中在这里分语种，
# 是因为它们散落时全是中文硬编码：回复语言跟客户走之后，一段英文回答的结尾
# 会突然接上一整段中文转人工通知（实测就是这样）。这类文案和安全话术同性质——
# 客户读的，就跟客户的语言。
#
# 诊断串（routing_reason / handoff_reason / 日志）不在这里：那是给控制台
# 操作员看的，保持中文。
_CUSTOMER_TEXT = {
    "zh": {
        "handoff_notice": (
            "这个问题我已经转交人工客服跟进，稍后会有同事联系您。"
            "为了加快处理，请准备好订单号或柜号、以及现场照片等相关资料。"
        ),
        # 流式下草稿已经逐句发出去了，收不回来——只能明确作废掉刚才的追问，
        # 而不是当它没发生过。
        "handoff_after_ask": "上面需要您补充的信息就不用回复我了——",
        "agent_error": "抱歉，处理您的请求时出现问题，请稍后重试。",
        "all_failed": "抱歉，所有 Agent 均处理失败。",
        "unavailable": "服务暂时不可用，请稍后重试。",
        "clarify": (
            "我还不能确定您要处理的是哪类问题。请补充一下是订单交付、报价账款，"
            "还是产品安装方面的问题？"
        ),
        "role_primary": "主处理",
        "role_supporting": "辅助处理",
    },
    "en": {
        "handoff_notice": (
            "I've passed this to a human colleague, who will follow up with you shortly. "
            "To speed things up, please have your order or cabinet number and any "
            "on-site photos ready."
        ),
        "handoff_after_ask": "You don't need to reply with the details I asked for above — ",
        "agent_error": "Sorry, something went wrong handling your request. Please try again shortly.",
        "all_failed": "Sorry, none of the agents were able to handle this request.",
        "unavailable": "The service is temporarily unavailable. Please try again shortly.",
        "clarify": (
            "I'm not yet sure which kind of issue this is. Could you tell me whether it's "
            "about order delivery, quoting and billing, or product and installation?"
        ),
        "role_primary": "primary",
        "role_supporting": "supporting",
    },
}


# 中文转人工文案的模块级别名：既有用例按它断言，保留住不让这次重构波及无关测试。
_HANDOFF_NOTICE = _CUSTOMER_TEXT["zh"]["handoff_notice"]


def _customer_text(req_or_message, key: str) -> str:
    """取面向客户的文案，语种跟客户提问走。"""
    message = getattr(req_or_message, "message", req_or_message) or ""
    return _CUSTOMER_TEXT[detect_language(message)][key]

# "向用户要东西"的表述：疑问句，或让用户提供/告知/补充信息的祈使句。
# 动词前允许插一个副词——"请**先**提供订单号""烦请**先**补一下照片"都漏网过。
# 英文那几条是补的：转人工时要先摘掉"让客户补资料"的表述，否则同一条回复会
# 一边让客户补充信息、一边说已经转交人工。中文侧早就这么做了，英文侧一条规则
# 都没有——于是英文回复里 "Could you send me photos?" 原样留着，后面紧跟
# "I've passed this to a human colleague"，正是这段注释说已经修掉的自相矛盾。
_ASK_RE = re.compile(
    r"[？?]"
    r"|(?:please|kindly)\s+(?:also\s+|first\s+)?"
    r"(?:provide|send|share|confirm|upload|attach|reply|forward|let\s+me\s+know)"
    r"|(?:could|can|would)\s+you\s+(?:please\s+)?"
    r"(?:provide|send|share|confirm|upload|attach|forward|let\s+me\s+know)"
    r"|(?:we|i)(?:'?ll|\s+will)?\s+need\s+(?:you\s+to\s+)?"
    r"(?:provide|send|share|confirm|the\b|a\b|your\b)"
    r"|send\s+(?:me|us|over)\b"
    r"|let\s+me\s+know\b"
    r"|(?:provide|share|confirm)\s+the\s+following"
    r"|(?:请|麻烦|烦请|还请)(?:您)?(?:先|再|帮我|把|尽快)?(?:提供|告知|发|回复|补充|补|传|说明|确认)"
    r"|方便(?:的话)?(?:再|先)?(?:提供|告知|发)"
    r"|需要(?:您)?(?:先|再)?(?:提供|补充|告知|确认)"
    r"|告诉我"
    r"|发(?:我|给我)"
    r"|(?:提供|补充|确认|补)一下",
    re.I,
)

# 列表项。`-`/`*` 必须带空格才算——行首的 `**加粗**` 会撞上 `*`，
# 把"**如果核实确实是重复扣款**，……必须转人工财务审核"这种整句误删。
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]\s+|[•·]\s*|\d+[.、)]\s*|[（(]\d+[）)]\s*)")

# 资料名词。清单里出现这些，说明它是"要你交上来的东西"，不是解释。
# 刻意不收"时间/金额/尺寸"这类两边都会出现的词——宁可漏判，不可错杀说明性内容。
_DOC_NOUN_RE = re.compile(
    r"订单号|合同号|柜号|单号|编号|截图|照片|图片|凭证|发票|验收单|签收单"
    r"|流水|账号|后四位|联系方式|收货地址"
    r"|order\s+number|po\s+number|cabinet\s+number|job\s+number|reference\s+number"
    r"|photo|picture|screenshot|invoice|receipt|packing\s+slip|delivery\s+note"
    r"|proof\s+of\s+(?:delivery|purchase)|contact\s+(?:number|details)|shipping\s+address",
    re.I,
)

# 冒号引导句 / 小标题。清单被摘掉后它就成了没有下文的标题，要跟着走。
_LEAD_IN_RE = re.compile(r"[：:]\s*$")
_HEADING_RE = re.compile(r"^\s*(?:\*\*[^*]{1,24}\*\*|#{1,6}\s+.{1,24}|.{1,24}[：:])\s*$")

# 行内断句，用于只摘掉一行里索取信息的那半句，保住同一行里有信息量的部分。
_LINE_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])")


def _last_content_line(lines: list[str]) -> str:
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ""


def _is_request_list(block: list[str], kept_lines: list[str]) -> bool:
    """
    判断一个清单是"待补资料"还是"说明内容"。

    两条证据任取其一即可判为待补资料：
      1. 条目里出现资料名词——真实回复里有过完全没有索取动词的
         "- **订单号或合同号**（必须，用来定位账目）"，只看动词一定漏；
      2. 引导句本身就在要东西——"**需要您补充的材料**"、"欢迎告诉我："。

    两条都不成立就当说明内容留下。破损判定那种
    "- 出厂前已存在裂纹 / - 运输途中磕碰"是回答的主体，删了会让后面
    "这三种情况责任方不同"变成没有指代的悬空句。
    """
    if any(_DOC_NOUN_RE.search(item) for item in block):
        return True
    lead_in = _last_content_line(kept_lines)
    return bool(lead_in and _ASK_RE.search(lead_in))


def _drop_dangling_lead_in(kept_lines: list[str], dropped: list[str]) -> None:
    """清单被丢掉后，回收它的引导句，不留没有下文的标题。"""
    while kept_lines and not kept_lines[-1].strip():
        kept_lines.pop()
    if not kept_lines:
        return
    tail = kept_lines[-1]
    if _LEAD_IN_RE.search(tail.rstrip("*　 ")) or _ASK_RE.search(tail) or _HEADING_RE.match(tail):
        dropped.append(kept_lines.pop().strip())


def _strip_customer_asks(text: str) -> tuple[str, list[str]]:
    """
    从待发出的回答里摘掉"要用户补充信息"的表述。

    返回（清理后的正文, 被摘掉的句子）。被摘掉的部分不是丢弃——它正是
    人工同事接手时还缺的资料，调用方会记进日志随工单一起留痕。
    """
    lines = text.splitlines()
    kept_lines: list[str] = []
    dropped: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # 清单整块判定：整块留或整块走，不逐条挑——挑剩半截清单比全留还难读
        if _LIST_ITEM_RE.match(line):
            j = i
            while j < len(lines) and _LIST_ITEM_RE.match(lines[j]):
                j += 1
            block = lines[i:j]
            if _is_request_list([b.strip() for b in block], kept_lines):
                dropped.extend(b.strip() for b in block)
                _drop_dangling_lead_in(kept_lines, dropped)
            else:
                kept_lines.extend(block)
            i = j
            continue

        i += 1
        stripped = line.strip()
        if not stripped or not _ASK_RE.search(stripped):
            kept_lines.append(line)
            continue

        # 一行里可能只有半句是追问，另外半句仍然有信息量，逐句筛而不是整行扔。
        kept_parts, dropped_parts = [], []
        for part in _LINE_SENTENCE_RE.split(line):
            (dropped_parts if _ASK_RE.search(part) else kept_parts).append(part)
        dropped.extend(p.strip() for p in dropped_parts if p.strip())

        remainder = "".join(kept_parts).rstrip()
        if remainder.strip():
            kept_lines.append(remainder)

    # 收尾：末尾剩下的"**后续流程**"这种小标题，下面已经空了，一并回收
    while True:
        while kept_lines and not kept_lines[-1].strip():
            kept_lines.pop()
        if not (kept_lines and _HEADING_RE.match(kept_lines[-1])):
            break
        dropped.append(kept_lines.pop().strip())

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return cleaned, dropped


# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排器。

    一次请求的完整链路：
      意图识别 → 低置信度澄清 → 领域打分选主/辅 Agent → 执行（失败降级）
      → 出站策略校验 → 置信度转人工判定 → 返回结果
    """

    # 意图 → 领域的映射。领域打分以此为基础，再叠加关键词和实体信号。
    _INTENT_DOMAIN: dict[IntentCategory, AgentType] = {
        IntentCategory.TECHNICAL:        AgentType.TECHNICAL,
        IntentCategory.INSTALLATION:     AgentType.TECHNICAL,
        IntentCategory.DAMAGE_DEFECT:    AgentType.TECHNICAL,
        IntentCategory.BILLING:          AgentType.BILLING,
        IntentCategory.QUOTE_PRICING:    AgentType.BILLING,
        IntentCategory.PAYMENT_ISSUE:    AgentType.BILLING,
        IntentCategory.ORDER:            AgentType.ORDER,
        IntentCategory.ORDER_STATUS:     AgentType.ORDER,
        IntentCategory.DELIVERY:         AgentType.ORDER,
        IntentCategory.ORDER_CHANGE:     AgentType.ORDER,
        IntentCategory.MISSING_PART:     AgentType.ORDER,
        IntentCategory.ESCALATION:       AgentType.HANDOFF,
        IntentCategory.HUMAN_HANDOFF:    AgentType.HANDOFF,
        # 其余意图（QUERY / REQUEST / COMPLAINT / GREETING / FEEDBACK /
        # OTHER）没有专家域，落到 GENERAL。
    }

    # 领域锚点：单独出现就足以说明"这句话里有这个域的诉求"。
    # 跨域工单（"门板有划痕，而且还少发了一块"）靠它把第二个域顶到辅助 Agent
    # 门槛之上——划痕是产品域，少发是订单域，两个域都要出人。
    _DOMAIN_ANCHORS: dict[AgentType, list[str]] = {
        AgentType.TECHNICAL: [
            "关不严", "对不齐", "铰链松", "装不上", "有划痕", "开裂", "起泡",
            "色差", "台面裂", "门板变形", "五金坏了", "需要返工",
        ],
        AgentType.BILLING: [
            "报价单", "报个价", "多少钱", "要加多少钱", "定金", "尾款",
            "多收了", "重复扣款", "付款失败", "开发票", "账单对不上",
        ],
        AgentType.ORDER: [
            "订单状态", "生产进度", "排产了吗", "什么时候送货", "少发了", "漏发",
            "缺件", "补件", "改尺寸", "取消订单", "订单进度",
        ],
    }

    # 领域弱关键词：单独出现证据不足，用来在锚点之上做微调。
    _DOMAIN_KEYWORDS: dict[AgentType, list[str]] = {
        AgentType.TECHNICAL: [
            "门板", "台面", "柜门", "铰链", "滑轨", "五金", "饰面", "安装",
            "划痕", "工艺", "变形", "install",
        ],
        AgentType.BILLING: [
            "报价", "价格", "费用", "账单", "发票", "定金", "尾款", "付款",
            "收费", "quote", "invoice", "payment",
        ],
        AgentType.ORDER: [
            "订单", "这单", "工单", "送货", "补件", "改单", "order",
            "delivery", "shipping",
        ],
    }

    # 打分权重
    _ANCHOR_FIRST = 0.5      # 命中第一个领域锚点
    _ANCHOR_EXTRA = 0.1      # 每多命中一个锚点
    _ANCHOR_CAP   = 0.65
    _KEYWORD_STEP = 0.12
    _KEYWORD_CAP  = 0.3

    # 转人工阈值。三路信号量纲不同，各自对齐自己的阈值分别判断：
    #   - 意图置信度是三路加权投票的融合分，量纲被权重压缩，天然偏低；
    #   - 路由领域分是 [0,1] 的领域打分，命中专家域时接近 1；
    #   - Agent 自评从 1.0 起，按策略违规和"我处理不了"的自述扣分。
    # 拿这三个数取 min 再跟同一个阈值比，会让正常流量被大量误转人工。
    INTENT_MIN_CONFIDENCE  = 0.45
    ROUTING_MIN_CONFIDENCE = 0.35
    AGENT_MIN_CONFIDENCE   = 0.70
    # 低于该值且意图落到 OTHER 时先澄清
    CLARIFY_CONFIDENCE_THRESHOLD = 0.5
    # 辅助 Agent 门槛：绝对分 + 相对主 Agent 的比例，两个条件都要满足
    SUPPORTING_MIN_SCORE = 0.45
    SUPPORTING_MIN_RATIO = 0.55

    def __init__(
        self,
        api_key:  str,
        base_url: str | None = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Any | None = None,
        policy: AnswerPolicy | None = None,
        recognizer: IntentRecognizer | None = None,
        replicas: int = 1,
        replica_models: list[str] | None = None,
    ):
        client = create_llm_client(api_key, base_url)
        self._model = model

        self._intent_recognizer = recognizer or IntentRecognizer(
            api_key=api_key, base_url=base_url, model=model
        )
        self._skill_manager = skill_manager
        self.policy = policy or AnswerPolicy()

        # 每种类型的实例数。>1 时 _best_agent() 的性能路由才真正产生选择差异：
        # Monitor 把某个实例的成功率/延迟写回 monitor_penalty 后，
        # 路由会自动把流量倒给同类型里表现更好的实例。
        self.replicas = max(1, int(replicas))
        # 可选：给每个实例指定不同模型（例如 0 号用强模型、1 号用便宜模型），
        # 让"按在线表现选实例"有真实的差异来源。长度不足时回落到主模型。
        self._replica_models = list(replica_models or [])

        # HANDOFF 不在池里——转人工由编排器直接产出，不消耗一次 LLM 调用。
        agent_classes = {
            AgentType.GENERAL:   GeneralAgent,
            AgentType.TECHNICAL: TechnicalAgent,
            AgentType.BILLING:   BillingAgent,
            AgentType.ORDER:     OrderAgent,
        }
        self._pool: dict[AgentType, list[BaseAgent]] = {
            agent_type: [
                cls(client, self._model_for(i), skill_manager, self.policy, replica=i)
                for i in range(self.replicas)
            ]
            for agent_type, cls in agent_classes.items()
        }
        # 同分实例之间轮询的游标，避免流量永远压在 0 号实例上
        self._rr_cursor: dict[AgentType, int] = {t: 0 for t in self._pool}

    def _model_for(self, replica: int) -> str:
        if replica < len(self._replica_models) and self._replica_models[replica]:
            return self._replica_models[replica]
        return self._model

    def set_skill_manager(self, skill_manager: Any | None) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    async def recognize_intent(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ):
        """对外暴露意图识别，供 API 层先判断是否需要 RAG 等前置能力。"""
        return await self._intent_recognizer.recognize(message, history=history)

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别 → 澄清判定 → 路由选 Agent → 执行 → 策略校验 → 转人工判定
        """
        t0 = time.monotonic()

        # 1. 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence

        # 2. 低置信度澄清：意图没定性时先追问，不猜着路由
        if self._needs_clarification(req):
            return self._clarification_result(req, t0)

        decision = self._route_decision(req)

        # 3. 明确要求转人工 / CRITICAL 紧急度：直接产出转人工结果，不走 LLM
        if decision.primary_agent == AgentType.HANDOFF:
            return self._handoff_result(req, decision, decision.reason, t0)

        # 4. 跨域问题并行协作，例如同一句同时涉及登录故障和重复扣款
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 5. 执行主 Agent（失败时降级到 GeneralAgent）
        response = await self._execute(req, decision.primary_agent)

        # 6. 置信度转人工：三路信号各自对齐阈值
        answer_confidence = self._answer_confidence(req, decision, [response])
        handoff_reasons = self._handoff_signals(req, decision, [response])
        if handoff_reasons:
            return self._handoff_result(
                req, decision, "；".join(handoff_reasons), t0,
                answer=response.content if response.success else "",
                answer_confidence=answer_confidence,
                policy_violations=response.policy_violations,
                # 执行失败时文案完全由编排器产出，agent_type 就该是 handoff，
                # 不能挂在那个没能给出回答的 Agent 头上。
                executed_agent=response.agent_type if response.success else None,
            )

        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=response.escalate,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            answer_confidence=answer_confidence,
            policy_violations=response.policy_violations,
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        并行派发给主 Agent 和辅助 Agent，合并结果。
        适用于跨域问题（如同时涉及技术和账单）。
        """
        t0 = time.monotonic()
        agent_types = decision.agent_types
        tasks = [self._execute(req, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        ok = [r for r in responses if isinstance(r, AgentResponse) and r.success]

        # 合并：主 Agent 在前，辅助 Agent 在后。
        parts = []
        for r in ok:
            role = _customer_text(
                req, "role_primary" if r.agent_type == decision.primary_agent else "role_supporting"
            )
            parts.append(f"[{r.agent_type.value} - {role}]\n{r.content}")

        combined = "\n\n".join(parts) if parts else _customer_text(req, "all_failed")
        escalated = any(r.escalate for r in ok)
        violations = [v for r in ok for v in r.policy_violations]
        answer_confidence = self._answer_confidence(req, decision, ok)

        handoff_reasons = self._handoff_signals(req, decision, ok)
        if handoff_reasons:
            return self._handoff_result(
                req, decision, "；".join(handoff_reasons), t0,
                answer=combined if ok else "",
                answer_confidence=answer_confidence,
                policy_violations=violations,
            )

        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[r.agent_type for r in ok] or agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            answer_confidence=answer_confidence,
            policy_violations=violations,
        )

    # ── 流式入口 ──────────────────────────────────────────────────────────────

    async def run_stream(self, req: Request) -> AsyncIterator[dict[str, Any]]:
        """
        流式版 run()。事件序列：
          {"type": "routing", ...}  路由决策，第一时间发出，前端可立刻画徽章
          {"type": "delta", "text"} 逐句产出的回答（已过策略守卫）
          {"type": "result", ...}   最终结构化结果，与 run() 的字段一致

        澄清、转人工、跨域协作这三条路径不做逐字流式：它们的文本要么是固定文案，
        要么需要合并多个 Agent 的结果，流式没有意义。这些情况直接把整段文本
        作为一个 delta 发出，再发 result，前端不需要区分处理。
        """
        t0 = time.monotonic()

        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence

        if self._needs_clarification(req):
            result = self._clarification_result(req, t0)
            yield {"type": "routing", "primary_agent": AgentType.GENERAL.value,
                   "supporting_agents": [], "reason": result.routing_reason,
                   "confidence": result.routing_confidence}
            yield {"type": "delta", "text": result.response}
            yield {"type": "result", "result": result}
            return

        decision = self._route_decision(req)
        yield {
            "type": "routing",
            "primary_agent": decision.primary_agent.value,
            "supporting_agents": [a.value for a in decision.supporting_agents],
            "reason": decision.reason,
            "confidence": decision.confidence,
            "domain_scores": {a.value: v for a, v in decision.domain_scores.items()},
        }

        # 非逐字流式的三条路径
        if decision.primary_agent == AgentType.HANDOFF:
            result = self._handoff_result(req, decision, decision.reason, t0)
            yield {"type": "delta", "text": result.response}
            yield {"type": "result", "result": result}
            return

        if decision.multi_agent:
            result = await self.run_parallel(req, decision)
            yield {"type": "delta", "text": result.response}
            yield {"type": "result", "result": result}
            return

        # 单 Agent：逐句流式
        agent = self._best_agent(decision.primary_agent) or self._best_agent(AgentType.GENERAL)
        if agent is None:
            result = self._handoff_result(req, decision, "没有可用的 Agent 实例", t0)
            yield {"type": "delta", "text": result.response}
            yield {"type": "result", "result": result}
            return

        response: AgentResponse | None = None
        async for event in agent.stream(req):
            if event["type"] == "delta":
                yield event
            else:
                response = event["response"]

        # 流式失败无法重放已发出的内容，因此这里不做"降级到 General 重跑"，
        # 而是直接转人工——重跑会让用户看到两段互相矛盾的回答。
        if response is None or not response.success:
            result = self._handoff_result(
                req, decision, "Agent 流式执行失败", t0,
                answer_confidence=0.0, executed_agent=decision.primary_agent,
            )
            yield {"type": "delta", "text": "\n\n" + result.response}
            yield {"type": "result", "result": result}
            return

        # 与 run() 用同一套转人工判定，避免流式和非流式对同一次回答给出不同结论
        answer_confidence = self._answer_confidence(req, decision, [response])
        handoff_reasons = self._handoff_signals(req, decision, [response])
        if handoff_reasons:
            result = self._handoff_result(
                req, decision, "；".join(handoff_reasons), t0,
                answer=response.content,
                answer_confidence=answer_confidence,
                policy_violations=response.policy_violations,
                executed_agent=response.agent_type,
                answer_already_sent=True,
            )
            # 回答本体已经流出去了，这里只补发转人工提示的增量部分
            yield {"type": "delta", "text": result.response[len(response.content):]}
            yield {"type": "result", "result": result}
            return

        yield {
            "type": "result",
            "result": OrchestratorResult(
                request_id=req.request_id,
                response=response.content,
                agent_type=response.agent_type,
                intent=req.intent,
                escalated=response.escalate,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[response.agent_type],
                primary_agent=decision.primary_agent,
                routing_reason=decision.reason,
                routing_confidence=decision.confidence,
                answer_confidence=answer_confidence,
                policy_violations=response.policy_violations,
            ),
        }

    # ── 转人工 ────────────────────────────────────────────────────────────────

    def _clarification_result(self, req: Request, t0: float) -> OrchestratorResult:
        """意图没定性时的澄清追问。run() 和 run_stream() 共用。"""
        return OrchestratorResult(
            request_id=req.request_id,
            response=_customer_text(req, "clarify"),
            agent_type=AgentType.GENERAL,
            intent=req.intent,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[AgentType.GENERAL],
            primary_agent=AgentType.GENERAL,
            routing_reason="低置信度 OTHER 意图，先澄清用户需求",
            routing_confidence=req.intent_confidence,
            answer_confidence=req.intent_confidence,
            needs_clarification=True,
        )

    def _handoff_result(
        self,
        req: Request,
        decision: "RoutingDecision",
        reason: str,
        t0: float,
        answer: str = "",
        answer_confidence: float = 0.0,
        policy_violations: list[PolicyViolation] | None = None,
        executed_agent: AgentType | None = None,
        answer_already_sent: bool = False,
    ) -> OrchestratorResult:
        """
        产出转人工结果。

        转人工不是"再调一次 LLM"，而是编排器直接给出确定文案并打上标记，
        由上层创建工单。已经生成的回答会保留在前面，避免用户白等一轮——
        但只保留有信息量的部分：让用户补资料的追问要摘掉，否则这一条回复
        会一边让用户补充信息、一边说已经转交人工，自相矛盾。

        answer_already_sent=True 用于流式：正文已经逐句发出去了，一个字都改不了，
        只能在结尾明确作废掉刚才的追问。
        """
        if answer_already_sent:
            # 已发出的正文必须原样保留，否则流式的增量切片会对不上。
            body = answer if answer.strip() else ""
            asks = _strip_customer_asks(answer)[1]
        else:
            body, asks = _strip_customer_asks(answer)

        notice = _customer_text(req, "handoff_notice")
        if asks and answer_already_sent:
            notice = _customer_text(req, "handoff_after_ask") + notice
        content = f"{body}\n\n{notice}" if body.strip() else notice

        logger.warning("请求 %s 转人工: %s", req.request_id, reason)
        if asks:
            # 摘掉的正是"人工接手时还缺的资料"，留痕给值班同事，不是丢弃。
            logger.info(
                "请求 %s 转人工，移除 %d 处向用户索取信息的表述: %s",
                req.request_id, len(asks), " | ".join(asks)[:300],
            )
        # 生产环境：此处创建工单、通知人工客服坐席
        return OrchestratorResult(
            request_id=req.request_id,
            response=content,
            agent_type=executed_agent or AgentType.HANDOFF,
            intent=req.intent,
            escalated=True,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[executed_agent] if executed_agent else [AgentType.HANDOFF],
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            handoff=True,
            handoff_reason=reason,
            answer_confidence=answer_confidence,
            policy_violations=list(policy_violations or []),
        )

    def _answer_confidence(
        self,
        req: Request,
        decision: "RoutingDecision",
        responses: list[AgentResponse],
    ) -> float:
        """
        对外展示的综合置信度 = 三路信号里最弱的一环。

        注意这个数只用于展示和排障，**不直接拿去和阈值比较**——三路量纲不同，
        真正的转人工判定在 _handoff_signals 里逐路对齐各自的阈值。
        """
        agent_confidence = min((r.confidence for r in responses), default=0.0)
        return round(min(req.intent_confidence, decision.confidence, agent_confidence), 3)

    def _handoff_signals(
        self,
        req: Request,
        decision: "RoutingDecision",
        responses: list[AgentResponse],
    ) -> list[str]:
        """逐路检查是否触发转人工，返回所有不达标的原因。"""
        reasons: list[str] = []

        if not responses or not all(r.success for r in responses):
            reasons.append("专家 Agent 与通用 Agent 均执行失败")
            return reasons  # 已经没有可用回答，其余信号无意义

        if req.intent_confidence < self.INTENT_MIN_CONFIDENCE:
            reasons.append(
                f"意图置信度 {req.intent_confidence:.2f} 低于 {self.INTENT_MIN_CONFIDENCE}"
            )
        if decision.confidence < self.ROUTING_MIN_CONFIDENCE:
            reasons.append(
                f"路由领域分 {decision.confidence:.2f} 低于 {self.ROUTING_MIN_CONFIDENCE}"
            )

        agent_confidence = min(r.confidence for r in responses)
        if agent_confidence < self.AGENT_MIN_CONFIDENCE:
            violations = sum(len(r.policy_violations) for r in responses)
            detail = (
                f"，其中答案策略拦截 {violations} 处无依据承诺" if violations else ""
            )
            reasons.append(
                f"Agent 自评置信度 {agent_confidence:.2f} 低于 "
                f"{self.AGENT_MIN_CONFIDENCE}{detail}"
            )
        return reasons

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

    def _route_decision(self, req: Request) -> RoutingDecision:
        """
        结构化路由决策。

        先处理紧急/转人工，再用领域分数在三个专家 Agent 之间决定主 Agent，
        并把分数达到门槛的其他专家升为辅助 Agent。这样可以表达"主处理 + 辅助诊断"，
        避免关键词命中后无主次地把答案拼在一起。
        """
        if req.urgency == UrgencyLevel.CRITICAL:
            return RoutingDecision(
                primary_agent=AgentType.HANDOFF,
                reason="紧急度为 CRITICAL，直接转人工",
                confidence=1.0,
            )

        if self._INTENT_DOMAIN.get(req.intent) == AgentType.HANDOFF:
            return RoutingDecision(
                primary_agent=AgentType.HANDOFF,
                reason=f"意图为 {req.intent.value if req.intent else 'unknown'}，用户明确要求人工",
                confidence=max(req.intent_confidence, 0.8),
            )

        scores = self._domain_scores(req)
        available_scores = {
            agent_type: score
            for agent_type, score in scores.items()
            if agent_type == AgentType.GENERAL or self._pool.get(agent_type)
        }
        if not available_scores:
            return RoutingDecision(
                primary_agent=AgentType.GENERAL,
                reason="无可用专家 Agent，降级到 GeneralAgent",
                confidence=0.1,
            )

        ordered = sorted(available_scores.items(), key=lambda item: item[1], reverse=True)
        primary_agent, primary_score = ordered[0]
        supporting_agents = [
            agent_type
            for agent_type, score in ordered[1:]
            if agent_type in SPECIALIST_AGENTS
            and score >= self.SUPPORTING_MIN_SCORE
            and score >= primary_score * self.SUPPORTING_MIN_RATIO
        ]

        reason = self._routing_reason(req, available_scores, primary_agent, supporting_agents)
        return RoutingDecision(
            primary_agent=primary_agent,
            supporting_agents=supporting_agents,
            reason=reason,
            confidence=round(min(primary_score, 1.0), 3),
            domain_scores=available_scores,
        )

    def _domain_scores(self, req: Request) -> dict[AgentType, float]:
        """
        按意图、领域锚点/关键词和结构化实体给三个专家 Agent 打分。

        GENERAL 只拿一个固定基准分，它不是专家域的竞争者——只有当三个专家
        都没拿到足够分数时才会胜出，这正是"没有专家域就走兜底"的表达。

        最终分数会截断到 [0, 1]。截断是必要的：辅助 Agent 的门槛之一是
        "达到主 Agent 分数的一定比例"，如果主 Agent 的分数可以无限累加，
        跨域工单里的第二个域永远够不到门槛，多 Agent 协作就成了死代码。
        """
        msg = (req.message or "").lower()
        scores: dict[AgentType, float] = {AgentType.GENERAL: 0.1}
        for agent_type in SPECIALIST_AGENTS:
            scores[agent_type] = 0.0

        # 信号 1：意图映射
        domain = self._INTENT_DOMAIN.get(req.intent)
        if domain in SPECIALIST_AGENTS:
            scores[domain] += 0.75
        elif req.intent is not None:
            # 没有专家域的意图（泛咨询、投诉、问候、正面反馈等）抬高兜底分
            scores[AgentType.GENERAL] += 0.55

        # 信号 2：领域锚点（强证据）+ 弱关键词（微调）
        for agent_type in SPECIALIST_AGENTS:
            anchor_hits = sum(1 for a in self._DOMAIN_ANCHORS[agent_type] if a in msg)
            if anchor_hits:
                scores[agent_type] += min(
                    self._ANCHOR_CAP,
                    self._ANCHOR_FIRST + self._ANCHOR_EXTRA * (anchor_hits - 1),
                )
            keyword_hits = sum(1 for kw in self._DOMAIN_KEYWORDS[agent_type] if kw in msg)
            scores[agent_type] += min(self._KEYWORD_CAP, keyword_hits * self._KEYWORD_STEP)

        # 信号 3：结构化实体
        entities = req.entities or {}
        if entities.get("item_code"):
            # 报得出具体柜号/型号（W3012、DB24-3）说明问的是某个柜子本身，
            # 不是泛泛咨询——这是产品与安装域的强信号。
            scores[AgentType.TECHNICAL] += 0.2
        if entities.get("amount"):
            scores[AgentType.BILLING] += 0.15
        if entities.get("order_id"):
            scores[AgentType.ORDER] += 0.2

        return {
            agent_type: round(min(1.0, max(0.0, score)), 3)
            for agent_type, score in scores.items()
        }

    @staticmethod
    def _routing_reason(
        req: Request,
        scores: dict[AgentType, float],
        primary_agent: AgentType,
        supporting_agents: list[AgentType],
    ) -> str:
        score_text = ", ".join(
            f"{agent_type.value}={score:.2f}"
            for agent_type, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        )
        support_text = ", ".join(agent.value for agent in supporting_agents) or "none"
        intent = req.intent.value if req.intent else "unknown"
        return (
            f"intent={intent}, group={req.intent_group or 'unknown'}, "
            f"primary={primary_agent.value}, supporting={support_text}, scores=[{score_text}]"
        )

    @classmethod
    def _needs_clarification(cls, req: Request) -> bool:
        """低置信度且无明确意图时，先追问，避免误路由。"""
        if req.intent != IntentCategory.OTHER:
            return False
        text = (req.message or "").strip()
        if len(text) <= 2:
            return False  # "在？"这类超短消息追问反而啰嗦，直接让通用 Agent 接
        return req.intent_confidence < cls.CLARIFY_CONFIDENCE_THRESHOLD

    # 分数差在这个范围内视为"打平"，打平的实例之间轮询分流
    _ROUTING_SCORE_EPSILON = 0.02

    def _best_agent(self, agent_type: AgentType) -> BaseAgent | None:
        """
        性能路由：从同类型实例中选 routing_score() 最高的。

        打平时轮询而不是固定取第一个——否则冷启动阶段所有实例分数都是 1.0，
        流量会永远压在 0 号实例上，其他实例的统计永远是空的，
        Monitor 的在线表现反馈也就永远不会产生任何路由差异。
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        if len(agents) == 1:
            return agents[0]

        scores = [(a, a.stats.routing_score()) for a in agents]
        best_score = max(score for _, score in scores)
        top = [a for a, score in scores if best_score - score <= self._ROUTING_SCORE_EPSILON]
        if len(top) == 1:
            return top[0]

        cursor = self._rr_cursor.get(agent_type, 0)
        self._rr_cursor[agent_type] = (cursor + 1) % len(top)
        return top[cursor % len(top)]

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """执行 Agent，专家失败时降级到 GeneralAgent 重试一次。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content=_customer_text(req, "unavailable"),
                success=False,
                confidence=0.0,
            )

        response = await agent.handle(req)

        # 专家 Agent 失败时降级到 GeneralAgent
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} 失败，降级到 GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(req)

        return response

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for agent in agents:
                result[agent.key] = {
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                    "specialist":   agent_type in SPECIALIST_AGENTS,
                    "display_name": AGENT_DISPLAY_NAMES.get(agent_type, agent_type.value),
                    "model":        agent.model,
                }
        return result

    def routing_config(self) -> dict[str, Any]:
        """路由配置快照，供 /health 展示当前专家阵容和各项阈值。"""
        return {
            "specialists": [a.value for a in SPECIALIST_AGENTS],
            "fallback_agent": AgentType.GENERAL.value,
            "display_names": {a.value: name for a, name in AGENT_DISPLAY_NAMES.items()},
            "replicas_per_type": self.replicas,
            "handoff_thresholds": {
                "intent": self.INTENT_MIN_CONFIDENCE,
                "routing": self.ROUTING_MIN_CONFIDENCE,
                "agent": self.AGENT_MIN_CONFIDENCE,
            },
            "clarify_confidence_threshold": self.CLARIFY_CONFIDENCE_THRESHOLD,
            "supporting_min_score": self.SUPPORTING_MIN_SCORE,
            "supporting_min_ratio": self.SUPPORTING_MIN_RATIO,
            "answer_policy": self.policy.stats(),
            "intent": self._intent_recognizer.stats,
        }

    def update_routing_penalties(self, penalties: dict[str, float]) -> None:
        """
        接收 Monitor 的在线表现反馈，动态调整路由惩罚项。

        penalties 的 key 使用 get_stats() 中的 agent key，例如 technical_0。
        """
        for agents in self._pool.values():
            for agent in agents:
                penalty = penalties.get(agent.key, 0.0)
                agent.stats.monitor_penalty = min(max(penalty, 0.0), 0.9)
