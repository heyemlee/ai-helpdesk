"""
亮点：端到端意图识别

把工单分到 18 个细粒度意图（IntentCategory 去掉兜底的 OTHER 后共 18 个），
用三路信号融合决定最终标签：

  1. 规则（关键词模式匹配，权重 10%）—— 同步执行，零网络往返
  2. Embedding 向量相似度（权重 20%）—— 匹配换了说法但语义相同的表达
  3. LLM 语义理解（权重 70%）—— 主力，理解复杂语义和上下文

Embedding 和 LLM 并行调用，不串行等待；三路结果加权投票合并，
置信度低于阈值时降级为 OTHER，交给上层做澄清追问。

零延迟规则通道（fast path）：
  高精度锚点短语（"转人工"、"补件"、"报价多少" 等）单独命中且不存在
  跨意图冲突时，直接返回结果，**完全跳过 Embedding 和 LLM 两路网络调用**。
  这条通道让高频高确定性工单的意图识别耗时从数百毫秒降到微秒级，
  同时让上层可以跳过查询改写和结果重排（见 api/main.py 的 RAG 分支）。
"""
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.embedder import Embedder, EmbeddingUnavailable
from app.core.llm_guard import log_model_failure, unavailable_reason
from app.core.llm_utils import (
    DEFAULT_ANTHROPIC_MODEL,
    STRUCTURED_MAX_TOKENS,
    create_llm_client,
    structured_parse,
)
from app.core.telemetry import span

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    """
    橱柜工厂 + 销售展厅的客服意图。

    5 个宽泛大类（QUERY / BILLING / TECHNICAL / ORDER / ESCALATION）对应"看得出
    是哪个域、但还不知道具体诉求"；9 个细粒度意图挂在大类下面，是真正驱动路由和
    SOP 的那一层；剩下 4 个是会话类意图（问候/投诉/请求/反馈）。
    """
    QUERY      = "query"       # 查询信息
    COMPLAINT  = "complaint"   # 投诉不满
    REQUEST    = "request"     # 请求操作
    GREETING   = "greeting"    # 问候
    ESCALATION = "escalation"  # 要求升级/转人工
    TECHNICAL  = "technical"   # 产品与安装技术
    BILLING    = "billing"     # 报价与账款
    ORDER      = "order"       # 订单与交付
    FEEDBACK   = "feedback"    # 正面反馈
    ORDER_STATUS  = "order_status"   # 订单/生产进度与交期
    DELIVERY      = "delivery"       # 送货、运输、到场排期
    ORDER_CHANGE  = "order_change"   # 改单：改尺寸/配置/饰面/取消
    MISSING_PART  = "missing_part"   # 补件：缺件、漏发、错发
    QUOTE_PRICING = "quote_pricing"  # 报价与价格咨询
    PAYMENT_ISSUE = "payment_issue"  # 定金/尾款/账款异常
    INSTALLATION  = "installation"   # 现场安装与五金调整
    DAMAGE_DEFECT = "damage_defect"  # 破损、瑕疵、返工
    HUMAN_HANDOFF = "human_handoff"  # 转人工
    OTHER      = "other"


class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


# 去掉兜底的 OTHER 之后的 18 个细粒度业务意图。
# 这个常量同时被 Evaluator 和测试引用，保证"18 类"是代码里可断言的事实，
# 而不是文档里的一句话。
FINE_GRAINED_INTENTS = tuple(c for c in IntentCategory if c is not IntentCategory.OTHER)
FINE_GRAINED_INTENT_COUNT = len(FINE_GRAINED_INTENTS)


# 类的 docstring 和字段的 description 会写进 JSON Schema、随请求发给模型，
# 所以那里只写模型需要知道的；给开发者看的来龙去脉放在注释里。
#
# LLM 那一路的返回形状，交给结构化输出在服务端强制。
#
# `intent` 写成 Literal 而不是直接用 IntentCategory：两者都能生成合法 schema，
# 但 Enum 会被 Pydantic 拆成 `$defs` + `$ref`，Literal 直接内联成一个 `enum`
# 数组。内联的那份更小、也不依赖服务端对 `$ref` 的支持。
#
# 这一层约束替掉了原来的 `except ValueError: intent = OTHER`——模型现在**不可能**
# 返回意图表以外的值，那个兜底分支从"经常走"变成了纯粹的防御。
class LLMIntentPayload(BaseModel):
    """对一条客户消息的意图判断。"""

    intent: Literal[tuple(c.value for c in IntentCategory)] = Field(  # type: ignore[valid-type]
        description="最贴切的意图；能对上细粒度业务意图时优先用细粒度的"
    )
    confidence: float = Field(description="对这个判断的把握，0 到 1 之间")
    reasoning: str = Field(description="一句话说明判断依据")


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    intent_group: str
    entities:   dict[str, list[str]]   # 从消息中提取的实体
    reasoning:  str
    latency_ms: float
    source_scores: dict[str, float] = field(default_factory=dict)
    # 本次识别走的通道：fast_path（纯规则，零网络调用）或 ensemble（三路融合）
    strategy:   str = "ensemble"
    # LLM 那一路这次没答上来（超时、5xx、凭证被拒……）：结果是另外两路凑出来的
    # 降级结果，不是这句话本来的意图。这种结果**不进缓存**，调用方也据此区分
    # "这句话确实含糊"和"模型没答上来"。
    llm_failed: bool = False
    # 没答上来的原因若是"模型服务不可用"（core/llm_guard），这里是原因，否则空串
    llm_unavailable: str = ""
    # 这次是从进程内缓存里取的：没有发起任何模型调用（事件日志据此不把"缺一次意图的
    # 模型调用"当成漏记）。缓存里存的那一份永远是 False，命中时返回的是一份副本
    cached: bool = False

    @property
    def fast_path(self) -> bool:
        return self.strategy == "fast_path"


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
#
# 宽泛大类（QUERY / REQUEST / TECHNICAL / BILLING / ORDER）的示例**必须避开细粒度
# 意图的典型说法**，否则会直接抵消 prompt 里"优先返回细粒度意图"那条指令——
# 模型看到的示例在教它反着做。这里给大类挑的都是"看得出是哪个域、但还判断不出
# 具体诉求"的句子，细粒度的典型说法留给对应的细粒度意图。
_TEMPLATES: dict[IntentCategory, list[str]] = {
    IntentCategory.QUERY:      ["这款门板有哪些颜色？", "展厅几点开门？", "你们做不做整装？"],
    IntentCategory.COMPLAINT:  ["等了好几个星期！", "服务太差了！", "一直没人处理！"],
    IntentCategory.REQUEST:    ["帮我预约量尺", "请把图纸发我一份", "帮我转给负责的销售"],
    IntentCategory.GREETING:   ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.ESCALATION: ["我要投诉！", "转人工客服", "找你们经理"],
    IntentCategory.TECHNICAL:  ["柜子这里做得不太对", "产品有点问题想问一下", "现场这块不太对劲"],
    IntentCategory.BILLING:    ["费用方面有个问题", "账目上想核对一下", "钱的事想问问"],
    IntentCategory.ORDER:      ["我这单有点问题", "关于订单的事想问一下", "这单想处理一下"],
    IntentCategory.FEEDBACK:   ["安装师傅很专业！", "非常满意", "给个好评"],
    IntentCategory.ORDER_STATUS: ["我的柜子生产到哪一步了？", "这批订单排产了吗？", "什么时候能做好？"],
    IntentCategory.DELIVERY: ["什么时候送货？", "能不能改送货日期？", "货车几点到工地？"],
    IntentCategory.ORDER_CHANGE: ["我想把柜体尺寸改一下", "能不能换个门板颜色？", "这单还能取消吗？"],
    IntentCategory.MISSING_PART: ["少发了一块门板", "缺一根抽屉滑轨", "补件什么时候到？"],
    IntentCategory.QUOTE_PRICING: ["这套橱柜报价多少？", "加一个岛台要加多少钱？", "能不能给个报价单？"],
    IntentCategory.PAYMENT_ISSUE: ["定金付了怎么还显示未付？", "尾款金额对不上", "多收了一笔费用"],
    IntentCategory.INSTALLATION: ["柜门关不严怎么调？", "铰链松了", "台面和墙之间有缝"],
    IntentCategory.DAMAGE_DEFECT: ["送来的门板有划痕", "台面裂了", "饰面起泡了"],
    IntentCategory.HUMAN_HANDOFF: ["转人工客服", "我要找人工", "请升级处理"],
}

_SPECIFIC_INTENTS = {
    IntentCategory.ORDER_STATUS,
    IntentCategory.DELIVERY,
    IntentCategory.ORDER_CHANGE,
    IntentCategory.MISSING_PART,
    IntentCategory.QUOTE_PRICING,
    IntentCategory.PAYMENT_ISSUE,
    IntentCategory.INSTALLATION,
    IntentCategory.DAMAGE_DEFECT,
    IntentCategory.HUMAN_HANDOFF,
}

# ── 零延迟规则通道的锚点短语 ──────────────────────────────────────────────────
# 与 _pattern_recognize 里的宽泛关键词不同，这里每条短语单独出现就足以定性，
# 不需要再花一次 LLM 往返去确认。命中即直接返回，不做任何网络调用。
# 英文锚点必须和中文一样密：快通道命中即返回，**不做任何网络调用**，
# 没有第二意见能纠正它。原来每个意图只挂一条英文锚点，其中 QUOTE_PRICING 那条
# 还是宽泛的 "how much"——于是
#   "My quartz countertop arrived with a chipped edge ... how much will it cost?"
# 只命中 how much 一条，快通道以 0.82 抢答成 quote_pricing 送去账款 Agent，
# 而它其实是破损投诉。中文侧不会这样：破损那一类有 5 条锚点，"有划痕/破损/开裂"
# 会同时命中，跨意图冲突让快通道整体让位给三路融合。
#
# 所以这里做两件事：把 "how much" 换成带宾语的完整问法，并把每个意图的英文锚点
# 补到和中文同等密度——锚点越密，复合问题越容易触发冲突回退，这是安全的方向。
_FAST_PATH_RULES: dict[IntentCategory, list[str]] = {
    IntentCategory.HUMAN_HANDOFF: [
        "转人工", "人工客服", "找人工", "转接人工", "要人工",
        "speak to a human", "talk to a person", "talk to someone", "real person",
        "escalate this", "transfer me", "your manager",
    ],
    IntentCategory.ORDER_STATUS: [
        "订单状态", "生产进度", "排产了吗", "订单进度",
        "order status", "production status", "where is my order",
        "status of my order", "status of order", "when will my order",
        "when will it be ready", "is it in production", "in production yet",
    ],
    IntentCategory.DELIVERY: [
        "什么时候送货", "送货日期", "送货时间", "改送货", "安排送货",
        "delivery date", "delivery time", "when will it ship", "when will it arrive",
        "reschedule delivery", "schedule delivery", "shipping date", "the truck",
    ],
    IntentCategory.ORDER_CHANGE: [
        "改尺寸", "改颜色", "改一下订单", "修改订单", "取消订单",
        "change my order", "cancel my order", "modify my order",
        "change the color", "change the colour", "change the size", "different finish",
    ],
    # "少了" 与 "少发了" 并存：前者覆盖"拆箱发现少了两个铰链"这类只说"少了"的表述。
    # 它比其他锚点宽，但宽在安全的方向——一旦同时命中别的意图锚点（如"铰链"），
    # 跨意图冲突会让快通道整体让位给三路融合，而不是抢答。
    IntentCategory.MISSING_PART: [
        "少发了", "少了", "漏发", "缺一块", "缺件", "补件",
        "missing part", "missing parts", "missing piece", "didn't receive",
        "did not receive", "never received", "short shipped", "not in the box",
        "wasn't included", "weren't included", "we're short", "we are short",
    ],
    IntentCategory.QUOTE_PRICING: [
        "报价单", "报价多少", "价格是多少", "要加多少钱", "能便宜点",
        # 用 "how much <助动词>" 而不是完整问法，也不是裸的 "how much"：
        # 完整问法挡不住 "how much does a quartz countertop cost"（宾语插在中间），
        # 裸的 "how much" 又会把 "how much longer will my order take" 判成询价。
        # 这几个前缀两边都覆盖到。
        "how much does", "how much is", "how much will", "how much would",
        "how much for", "what's the price", "what is the price",
        "send me a quote", "price quote", "quote for", "price list",
    ],
    IntentCategory.PAYMENT_ISSUE: [
        "定金", "尾款", "多收了", "重复扣款", "付款失败",
        "duplicate charge", "charged twice", "double charged", "overcharged",
        "payment failed", "payment didn't go through", "billed twice",
    ],
    IntentCategory.INSTALLATION: [
        "关不严", "铰链", "对不齐", "有条缝", "装不上",
        "adjust the hinge", "hinge is loose", "loose hinge", "won't close",
        "doesn't close", "doesn't line up", "don't line up", "not aligned", "gap between",
        "won't fit", "doesn't fit",
    ],
    IntentCategory.DAMAGE_DEFECT: [
        "有划痕", "破损", "开裂", "起泡", "色差",
        "damaged panel", "damaged", "chipped", "cracked", "scratch", "dented",
        "warped", "peeling", "arrived broken", "color mismatch", "colour mismatch",
    ],
}

# 命中锚点后的置信度：单条 0.82，每多命中一条 +0.05，上限 0.95。
_FAST_PATH_BASE_CONFIDENCE = 0.82
_FAST_PATH_STEP = 0.05
_FAST_PATH_MAX_CONFIDENCE = 0.95

_GENERIC_INTENTS = {
    IntentCategory.QUERY,
    IntentCategory.BILLING,
    IntentCategory.TECHNICAL,
    IntentCategory.ORDER,
    IntentCategory.ESCALATION,
}

_INTENT_GROUPS: dict[IntentCategory, IntentCategory] = {
    IntentCategory.ORDER_STATUS: IntentCategory.QUERY,
    IntentCategory.DELIVERY: IntentCategory.QUERY,
    IntentCategory.ORDER_CHANGE: IntentCategory.ORDER,
    IntentCategory.MISSING_PART: IntentCategory.ORDER,
    IntentCategory.QUOTE_PRICING: IntentCategory.BILLING,
    IntentCategory.PAYMENT_ISSUE: IntentCategory.BILLING,
    IntentCategory.INSTALLATION: IntentCategory.TECHNICAL,
    IntentCategory.DAMAGE_DEFECT: IntentCategory.TECHNICAL,
    IntentCategory.HUMAN_HANDOFF: IntentCategory.ESCALATION,
}

# 柜体/工单编号识别：橱柜行业的柜型号是"字母+尺寸数字"（B24 底柜、W3012 吊柜、
# SB36 水槽柜、DB24-3 三抽柜），另外还有 SKU/PO/JOB 前缀的工单号。
# 关键约束是必须带数字：早期版本用 [A-Z][A-Z0-9_-]{2,16} 会把任意大写单词当成
# 编号——"I NEED HELP" 会命中 NEED/HELP，进而给产品域错误加分导致误路由。
# 柜型号这一支刻意不开 re.I：行业写法一律大写，放开大小写会把 "and24" 这类
# 普通词当成柜号。
_ITEM_CODE_RE = re.compile(
    r"(?<![0-9A-Za-z¥￥$])("
    r"[A-Z]{1,3}\d{2,4}(?:-[A-Z0-9]{1,3})?"
    r"|(?i:SKU|PO|JOB|LOT|ITEM)[-_# ]?\d{3,8}"
    r")(?![0-9A-Za-z])"
    r"(?!\s*(?:元|块钱|块|美元|美金|usd|cny|rmb|dollar))"
)

# 紧急关键词
_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: ["紧急", "emergency", "urgent", "asap", "立刻"],
    UrgencyLevel.HIGH:     ["今天", "马上", "尽快", "hurry", "now"],
    UrgencyLevel.MEDIUM:   ["这周", "soon", "快点"],
}


def _cosine(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentRecognizer:
    """
    端到端意图识别器。

    初始化时不加载任何本地模型，所有 AI 能力通过远端 LLM 调用
    （Anthropic 或 OpenAI 兼容协议，见 core/llm_utils.create_llm_client）。
    模板 Embedding 在首次请求时懒加载并缓存，后续复用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        confidence_threshold: float = 0.5,
        embedding_base_url: str | None = None,
        embedding_api_key: str | None = None,
        embedding_model: str = "text-embedding-3-small",
        enable_fast_path: bool = True,
        embedder: Embedder | None = None,
    ):
        self.client    = create_llm_client(api_key, base_url)
        self.model     = model
        self.threshold = confidence_threshold
        self.enable_fast_path = enable_fast_path

        # Embedding 这一路始终参与投票。配置了 OpenAI 兼容的 embeddings 端点时
        # 走远端真向量；没配置（例如走 Anthropic 协议，它不提供 embeddings 资源）
        # 时退化为本地字符 n-gram 哈希向量。
        # 换供应商是改环境变量的事，不需要改代码，三路融合链路始终成立。
        #
        # 服务里传进来的是和知识库共用的那个 Embedder；单独构造（测试、评测脚本）
        # 时按 embedding_* 参数自己建一个。
        self._embedding_enabled = True
        dim_raw = os.getenv("EMBEDDING_DIM", "").strip()
        self._embedder = embedder or Embedder(
            base_url=embedding_base_url, api_key=embedding_api_key, model=embedding_model,
            dim=int(dim_raw) if dim_raw else None,
        )

        # 模板向量，以及它们是哪条路算出来的（remote / local）。消息向量必须和模板
        # 在同一个空间里比：远端失败时这一路整体改用本地向量现算，模板也跟着重算，
        # 不拿本地的消息向量去和远端的模板向量算余弦。
        self._tpl_embeddings: dict[IntentCategory, list[list[float]]] = {}
        self._tpl_space = ""
        self._cache: dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0
        self.fast_path_hits = 0
        self.ensemble_hits  = 0
        # LLM 这一路的失败次数。三路融合的容错设计让它挂掉也不报错，
        # 所以必须计数并暴露出去，否则"主力信号长期掉线"只有日志里看得见。
        self.llm_failures   = 0

    def reconfigure_llm(self, client: Any, model: str) -> None:
        """
        换用另一份对话模型连接（控制台保存时，见 core/model_runtime.py）。

        缓存一起清掉：里面是旧模型的判断，换了模型还拿它们回答，就等于没换。
        """
        self.client = client
        self.model = model
        self._cache.clear()

    @property
    def embedding_backend(self) -> str:
        """当前 Embedding 这一路实际使用的后端，供 /health 和排障使用。"""
        return "remote" if self._remote_embedding_usable else "local_ngram"

    @property
    def _remote_embedding_usable(self) -> bool:
        return self._embedder.backend == "remote" and self._embedder.available

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        with span("intent.recognize") as sp:
            result = await self._recognize_inner(message, history)
            sp.set_attribute("intent", result.intent.value)
            sp.set_attribute("strategy", result.strategy)
            sp.set_attribute("confidence", result.confidence)
            return result

    async def _recognize_inner(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> IntentResult:
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return dataclasses.replace(self._cache[key], cached=True)
        self.cache_misses += 1

        t0 = time.monotonic()

        # 0. 零延迟规则通道：高置信度锚点命中即返回，不发起任何网络调用。
        fast = self._fast_path(message) if self.enable_fast_path else None
        if fast is not None:
            self.fast_path_hits += 1
            result = IntentResult(
                intent=fast["intent"],
                confidence=fast["confidence"],
                urgency=self._urgency(message, fast["intent"]),
                intent_group=self._intent_group(fast["intent"]),
                entities=self._extract_entities(message),
                reasoning=f"规则通道命中锚点短语: {', '.join(fast['anchors'])}",
                latency_ms=(time.monotonic() - t0) * 1000,
                source_scores={"rules": fast["confidence"], "embedding": 0.0, "llm": 0.0},
                strategy="fast_path",
            )
            self._remember(key, result)
            return result

        self.ensemble_hits += 1

        # 1. 三路融合：规则同步执行，Embedding 和 LLM 并行发起。
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message))
        pat      = self._pattern_recognize(message)
        llm, emb = await asyncio.gather(llm_task, emb_task)

        if llm.get("failed"):
            self.llm_failures += 1

        intent, confidence, source_scores = self._vote(llm, emb, pat)
        entities = self._extract_entities(message)
        urgency  = self._urgency(message, intent)

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            urgency=urgency,
            intent_group=self._intent_group(intent),
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=source_scores,
            strategy="ensemble",
            llm_failed=bool(llm.get("failed")),
            llm_unavailable=str(llm.get("unavailable") or ""),
        )
        # 基础设施故障凑出来的降级结果不进缓存。缓存没有过期时间：模型停机时记下的
        # "other / 0.15"会在恢复之后继续被同一句话命中，直到进程重启。
        if not result.llm_failed:
            self._remember(key, result)
        return result

    def _remember(self, key: str, result: IntentResult) -> None:
        """写入 LRU 缓存，超出容量时淘汰最早的一半。"""
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """在线学习：将纠正样本加入模板，清除对应 Embedding 缓存。"""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._tpl_embeddings.pop(correct, None)  # 下次重新计算
            logger.info(f"学习新样本 → {correct.value}: {message[:40]}")

    # ── 零延迟规则通道 ────────────────────────────────────────────────────────

    def _fast_path(self, message: str) -> dict[str, Any] | None:
        """
        纯规则、零网络调用的快速通道。

        只有满足以下全部条件才返回结果，否则交给三路融合：
          1. 命中 _FAST_PATH_RULES 中的高精度锚点短语；
          2. 命中的锚点全部指向同一个意图（跨意图冲突说明这是复合问题，
             例如"登录报错还被重复扣款"，必须交给 LLM 判断主意图）；
          3. 消息不太长——超长消息通常包含多个诉求，锚点不足以定性。
        """
        text = (message or "").strip()
        if not text or len(text) > 120:
            return None

        lowered = text.lower()
        hits: dict[IntentCategory, list[str]] = {}
        for intent, anchors in _FAST_PATH_RULES.items():
            matched = [a for a in anchors if a.lower() in lowered]
            if matched:
                hits[intent] = matched

        if len(hits) != 1:
            return None  # 0 个：规则无法定性；>1 个：跨意图冲突，走完整融合

        intent, anchors = next(iter(hits.items()))
        confidence = min(
            _FAST_PATH_MAX_CONFIDENCE,
            _FAST_PATH_BASE_CONFIDENCE + _FAST_PATH_STEP * (len(anchors) - 1),
        )
        return {"intent": intent, "confidence": round(confidence, 3), "anchors": anchors}

    @property
    def fast_path_stats(self) -> dict[str, Any]:
        """规则通道命中率——用来量化"跳过了多少次 LLM 往返"。"""
        total = self.fast_path_hits + self.ensemble_hits
        return {
            "fast_path_hits": self.fast_path_hits,
            "ensemble_hits": self.ensemble_hits,
            "fast_path_rate": round(self.fast_path_hits / total, 4) if total else 0.0,
            "llm_calls_saved": self.fast_path_hits,
            # 融合路里 LLM 这一路挂掉的次数。持续 > 0 说明主力信号掉线，
            # 意图识别正在靠 Embedding + 规则两路硬撑。
            "llm_failures": self.llm_failures,
            "llm_failure_rate": (
                round(self.llm_failures / self.ensemble_hits, 4) if self.ensemble_hits else 0.0
            ),
        }

    # ── 三路识别策略 ──────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        """策略 1：LLM 语义理解（Few-shot + 上下文）。"""
        message = self._clean_text(message)
        # 构建 Few-shot 示例
        examples = "\n".join(
            f'  消息: "{t}" → 意图: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # 每类取 1 条，控制 prompt 长度
        )
        # 最近 3 轮对话上下文
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""你是橱柜工厂与销售展厅的客服意图分析专家。根据示例判断用户意图。
如果用户问题能匹配细粒度业务意图，请优先返回细粒度意图，而不是宽泛大类。
例如缺件漏发优先返回 missing_part，问价优先返回 quote_pricing，
改尺寸改配置优先返回 order_change，柜门/铰链调整优先返回 installation。

示例:
{examples}

{ctx}
用户消息: "{message}"

confidence 表示你对该判断的把握，0 到 1 之间。

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            payload = await structured_parse(
                self.client,
                output_format=LLMIntentPayload,
                model=self.model,
                max_tokens=STRUCTURED_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            return {
                # schema 已经把取值限死在意图表里，这里的转换不会再抛。
                "intent": IntentCategory(payload.intent),
                # 置信度进三路投票要参与加权，越界值会污染融合分，钳一下。
                "confidence": min(1.0, max(0.0, payload.confidence)),
                "reasoning": payload.reasoning,
            }
        except Exception as ex:
            log_model_failure(logger, "LLM 识别失败", ex)
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败",
                    "failed": True, "unavailable": unavailable_reason(ex)}

    async def _embedding_recognize(self, message: str) -> dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            space, (msg_vec,) = await self._embed_texts([message])
            if await self._load_template_embeddings(space) != space:
                # 模板加载到一半远端挂了，模板已经改用本地向量：消息也得换过去
                msg_vec = self._local_embedding(message)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning(f"Embedding 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> dict[str, Any]:
        """策略 3：关键词模式匹配（同步，零延迟兜底）。"""
        msg = message.lower()
        specific_patterns = {
            IntentCategory.HUMAN_HANDOFF: ["转人工", "人工客服", "找人工"],
            IntentCategory.ORDER_STATUS: ["订单状态", "生产进度", "排产", "做好", "order status"],
            IntentCategory.DELIVERY: ["送货", "运输", "到场", "卸货", "delivery", "shipping"],
            IntentCategory.ORDER_CHANGE: ["改单", "改尺寸", "改颜色", "改一下", "换成", "取消", "change order"],
            IntentCategory.MISSING_PART: ["少发", "漏发", "缺件", "补件", "missing", "short shipped"],
            IntentCategory.QUOTE_PRICING: ["报价", "多少钱", "价格", "quote", "how much"],
            IntentCategory.PAYMENT_ISSUE: ["定金", "尾款", "多收", "重复扣款", "付款失败", "payment failed"],
            IntentCategory.INSTALLATION: ["安装", "铰链", "关不严", "对不齐", "装不上", "install"],
            IntentCategory.DAMAGE_DEFECT: ["划痕", "破损", "开裂", "色差", "起泡", "damaged"],
        }
        generic_patterns = {
            IntentCategory.ESCALATION: ["投诉", "经理", "supervisor"],
            IntentCategory.COMPLAINT:  ["太差", "糟糕", "horrible", "等了很久"],
            IntentCategory.QUERY:      ["?", "？", "怎么", "什么", "status"],
            IntentCategory.REQUEST:    ["帮我", "需要", "please", "help"],
            IntentCategory.GREETING:   ["你好", "嗨", "hello", "hi"],
            IntentCategory.BILLING:    ["报价", "账单", "发票", "quote"],
            IntentCategory.TECHNICAL:  ["台面", "门板", "五金", "工艺"],
            IntentCategory.ORDER:      ["订单", "这单", "工单", "order"],
        }

        best_cat, best_score = self._best_pattern_match(msg, specific_patterns)
        if best_cat != IntentCategory.OTHER:
            return {"intent": best_cat, "confidence": best_score}

        best_cat, best_score = self._best_pattern_match(msg, generic_patterns)
        return {"intent": best_cat, "confidence": best_score}

    # ── 投票合并 ──────────────────────────────────────────────────────────────

    # 三路权重：LLM 主导语义，Embedding 补换句表达，规则兜底高确定性关键词。
    _VOTE_WEIGHTS = {"llm": 0.7, "embedding": 0.2, "rules": 0.1}

    def _vote(self, llm: dict, emb: dict, pat: dict) -> tuple[IntentCategory, float, dict[str, float]]:
        """加权投票。返回最终意图、融合置信度和各路来源得分。"""
        source_scores = {
            "llm": float(llm.get("confidence", 0.0) or 0.0),
            "embedding": float(emb.get("confidence", 0.0) or 0.0),
            "rules": float(pat.get("confidence", 0.0) or 0.0),
        }
        if llm.get("failed"):
            # LLM 这一路挂了，剩下两路继续给结果——这是三路融合的容错价值。
            # 但降级有两条硬约束：取**更强**的那一路，且必须过和正常路径同一道阈值。
            #
            # 之前这里无条件优先 Embedding、且完全跳过阈值判断，于是本地 n-gram
            # 向量对短句给出的噪声匹配被当成"已定性的具体意图"送进路由：
            # "hello" 判成 payment_issue（0.11）→ 路由到账款 Agent → 0.11 又低于
            # 转人工阈值 → 立刻开一张工单。噪声不该冒充证据，够不到阈值就老实
            # 落到 OTHER，交给上层澄清追问。
            fallback_intent, fallback_conf = max(
                (
                    (emb.get("intent", IntentCategory.OTHER), source_scores["embedding"]),
                    (pat.get("intent", IntentCategory.OTHER), source_scores["rules"]),
                ),
                key=lambda item: item[1],
            )
            if fallback_intent is IntentCategory.OTHER or fallback_conf < self.threshold:
                return IntentCategory.OTHER, fallback_conf, source_scores
            return fallback_intent, fallback_conf, source_scores

        weights = [
            (llm, self._VOTE_WEIGHTS["llm"]),
            (emb, self._VOTE_WEIGHTS["embedding"]),
            (pat, self._VOTE_WEIGHTS["rules"]),
        ]
        scores: dict[IntentCategory, float] = {}
        for result, w in weights:
            cat  = result.get("intent", IntentCategory.OTHER)
            conf = result.get("confidence", 0.0)
            scores[cat] = scores.get(cat, 0.0) + w * conf

        best = max(scores, key=scores.get)  # type: ignore
        best_score = scores[best]
        pat_intent = pat.get("intent", IntentCategory.OTHER)
        pat_conf = float(pat.get("confidence", 0.0) or 0.0)
        if best in _GENERIC_INTENTS and pat_intent in _SPECIFIC_INTENTS and pat_conf >= 0.5 and best_score < 0.8:
            source_scores["refined_by_pattern"] = pat_conf
            return pat_intent, max(best_score, pat_conf), source_scores
        if best_score < self.threshold:
            return IntentCategory.OTHER, best_score, source_scores
        return best, best_score, source_scores

    # ── 实体提取 ──────────────────────────────────────────────────────────────

    def _extract_entities(self, message: str) -> dict[str, list[str]]:
        """用规则提取高价值实体，避免每次识别都额外调用 LLM。"""
        message = self._clean_text(message)
        return {
            "order_id": self._unique(re.findall(r"(?:订单号?|order(?:_id)?|#)\s*[:：#]?\s*([A-Za-z0-9_-]{4,32})", message, re.I)),
            "product": [],
            "date": self._unique(re.findall(r"(今天|明天|昨天|本周|这周|下周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", message)),
            "amount": self._unique(re.findall(r"((?:[¥￥$])\s*\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:\.\d{1,2})?\s*(?:元|块|rmb|cny|usd|美元))", message, re.I)),
            "item_code": self._unique(_ITEM_CODE_RE.findall(message)),
        }

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self, space: str) -> str:
        """
        保证模板向量和 space 在同一个空间里，缺的补算。返回模板最终所在的空间。

        只有三种情况真的去算：首次调用、某个类别被纠正过（见 learn）、空间换了。
        换空间有两种——远端挂了改用本地，和远端恢复后换回来——都要把模板整批重算：
        留着另一个空间的旧向量，比出来的分数没有意义。
        远端一次请求带一批模板（Embedder.embed_many），不是一条一个请求。
        """
        key = self._space_key(space)
        if self._tpl_space != key:
            self._tpl_embeddings.clear()
            self._tpl_space = key
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return space

        texts = [t for cat in missing for t in _TEMPLATES[cat]]
        got, vecs = await self._embed_texts(texts, prefer=space)
        if got != space:
            # 要的是远端，中途挂了：已有的远端模板作废，全部改用本地向量
            self._tpl_embeddings.clear()
            self._tpl_space = self._space_key(got)
            missing = list(_TEMPLATES)
            vecs = [self._local_embedding(t) for cat in missing for t in _TEMPLATES[cat]]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n
        return got

    def _space_key(self, space: str) -> str:
        """
        模板向量是在哪个空间里算的。远端要带上模型——控制台换了 Embedding 模型之后，
        同样是"remote"，旧模板也已经不能用了。
        """
        return f"remote:{self._embedder.signature}" if space == "remote" else "local"

    async def _embed_texts(
        self, texts: list[str], prefer: str = "remote"
    ) -> tuple[str, list[list[float]]]:
        """
        生成一批文本向量，返回 (向量所在的空间, 向量)。

        优先调用配置好的 OpenAI 兼容 embeddings 端点（EMBEDDING_BASE_URL /
        EMBEDDING_API_KEY）；没配置、还在失败后的冷却期、或这次调用失败时改用
        字符 n-gram 哈希向量。无论走哪条路，Embedding 都会作为独立的一路参与投票
        ——这是"三路融合"在换供应商时依然成立的原因。

        返回值带着空间，是因为两种向量不能放在一起比：调用方要据此保证消息和
        模板用的是同一种。冷却由 Embedder 管，过了冷却期这里会自动换回远端。
        """
        if prefer == "remote" and self._remote_embedding_usable:
            try:
                return "remote", await self._embedder.embed_many(texts)
            except EmbeddingUnavailable as ex:
                logger.warning(f"远端 Embedding 不可用，意图识别这一路暂时改用本地向量: {ex}")
        return "local", [self._local_embedding(text) for text in texts]

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> list[float]:
        """稳定的字符 n-gram 哈希向量，用于无远端 Embedding 时的语义近似匹配。"""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent in (IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF):
            return UrgencyLevel.HIGH
        if intent == IntentCategory.COMPLAINT:
            return UrgencyLevel.MEDIUM
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: list[dict[str, str]] | None = None) -> str:
        payload = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _best_pattern_match(
        message: str,
        patterns: dict[IntentCategory, list[str]],
    ) -> tuple[IntentCategory, float]:
        best_cat, best_score = IntentCategory.OTHER, 0.0
        for cat, kws in patterns.items():
            hits = sum(1 for kw in kws if kw in message)
            if not hits:
                continue
            # 单个明确业务关键词就给可用置信度；多个关键词命中时提高置信度。
            score = min(1.0, 0.5 + 0.25 * (hits - 1))
            if score > best_score:
                best_score, best_cat = score, cat
        return best_cat, best_score

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, intent).value

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }

    @property
    def stats(self) -> dict[str, Any]:
        """意图识别器整体状态，供 /health 和 /monitor 暴露。"""
        return {
            "fine_grained_intents": FINE_GRAINED_INTENT_COUNT,
            "vote_weights": dict(self._VOTE_WEIGHTS),
            "embedding_backend": self.embedding_backend,
            "cache": self.cache_stats,
            **self.fast_path_stats,
        }
