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
  高精度锚点短语（"转人工"、"重复扣款"、"开发票" 等）单独命中且不存在
  跨意图冲突时，直接返回结果，**完全跳过 Embedding 和 LLM 两路网络调用**。
  这条通道让高频高确定性工单的意图识别耗时从数百毫秒降到微秒级，
  同时让上层可以跳过查询改写和结果重排（见 api/main.py 的 RAG 分支）。
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    QUERY      = "query"       # 查询信息
    COMPLAINT  = "complaint"   # 投诉不满
    REQUEST    = "request"     # 请求操作
    GREETING   = "greeting"    # 问候
    ESCALATION = "escalation"  # 要求升级/转人工
    TECHNICAL  = "technical"   # 技术问题
    BILLING    = "billing"     # 账单/退款
    ACCOUNT    = "account"     # 账户管理
    FEEDBACK   = "feedback"    # 正面反馈
    ORDER_STATUS = "order_status"        # 订单状态
    LOGISTICS = "logistics"              # 物流配送
    REFUND = "refund"                    # 退款/退货
    INVOICE = "invoice"                  # 发票
    PAYMENT_ISSUE = "payment_issue"      # 支付/扣款异常
    ACCOUNT_SECURITY = "account_security" # 账户安全
    TECHNICAL_LOGIN = "technical_login"  # 登录认证故障
    TECHNICAL_CRASH = "technical_crash"  # 崩溃/错误码
    HUMAN_HANDOFF = "human_handoff"      # 转人工
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

    @property
    def fast_path(self) -> bool:
        return self.strategy == "fast_path"


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
_TEMPLATES: dict[IntentCategory, list[str]] = {
    IntentCategory.QUERY:      ["我的订单状态是什么？", "如何重置密码？", "快递什么时候到？"],
    IntentCategory.COMPLAINT:  ["等了好几个小时！", "服务太差了！", "一直没人处理！"],
    IntentCategory.REQUEST:    ["帮我取消订单", "我需要修改地址", "请协助退款"],
    IntentCategory.GREETING:   ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.ESCALATION: ["我要投诉！", "转人工客服", "找你们经理"],
    IntentCategory.TECHNICAL:  ["应用一直崩溃", "无法登录", "出现500错误"],
    IntentCategory.BILLING:    ["为什么扣了两次款？", "申请退款", "发票问题"],
    IntentCategory.ACCOUNT:    ["修改邮箱", "注销账户", "更新个人信息"],
    IntentCategory.FEEDBACK:   ["服务很棒！", "非常满意", "给个好评"],
    IntentCategory.ORDER_STATUS: ["我的订单现在是什么状态？", "订单有没有发货？", "订单处理到哪一步了？"],
    IntentCategory.LOGISTICS: ["快递什么时候到？", "物流一直不更新", "配送要多久？"],
    IntentCategory.REFUND: ["我要申请退款", "退货退款怎么处理？", "退款多久到账？"],
    IntentCategory.INVOICE: ["帮我开发票", "发票抬头怎么改？", "电子发票在哪里？"],
    IntentCategory.PAYMENT_ISSUE: ["为什么重复扣款？", "支付失败怎么办？", "这个月多扣了钱"],
    IntentCategory.ACCOUNT_SECURITY: ["账户被盗了", "发现异常登录", "我要重置密码"],
    IntentCategory.TECHNICAL_LOGIN: ["登录一直报401", "验证码收不到", "无法登录账号"],
    IntentCategory.TECHNICAL_CRASH: ["应用一直崩溃", "页面报500错误", "系统闪退"],
    IntentCategory.HUMAN_HANDOFF: ["转人工客服", "我要找人工", "请升级处理"],
}

_SPECIFIC_INTENTS = {
    IntentCategory.ORDER_STATUS,
    IntentCategory.LOGISTICS,
    IntentCategory.REFUND,
    IntentCategory.INVOICE,
    IntentCategory.PAYMENT_ISSUE,
    IntentCategory.ACCOUNT_SECURITY,
    IntentCategory.TECHNICAL_LOGIN,
    IntentCategory.TECHNICAL_CRASH,
    IntentCategory.HUMAN_HANDOFF,
}

# ── 零延迟规则通道的锚点短语 ──────────────────────────────────────────────────
# 与 _pattern_recognize 里的宽泛关键词不同，这里每条短语单独出现就足以定性，
# 不需要再花一次 LLM 往返去确认。命中即直接返回，不做任何网络调用。
_FAST_PATH_RULES: dict[IntentCategory, list[str]] = {
    IntentCategory.HUMAN_HANDOFF: ["转人工", "人工客服", "找人工", "转接人工", "要人工"],
    IntentCategory.ORDER_STATUS: ["订单状态", "发货了吗", "有没有发货", "订单进度", "order status"],
    IntentCategory.LOGISTICS: ["物流信息", "快递到哪", "物流不更新", "运单号", "配送进度", "tracking number"],
    IntentCategory.REFUND: ["申请退款", "我要退款", "退款进度", "退款多久", "退货退款", "request a refund"],
    IntentCategory.INVOICE: ["开发票", "开具发票", "发票抬头", "电子发票", "增值税发票", "发票重开"],
    IntentCategory.PAYMENT_ISSUE: ["重复扣款", "重复支付", "多扣了", "扣了两次", "支付失败", "duplicate charge"],
    IntentCategory.ACCOUNT_SECURITY: ["账号被盗", "账户被盗", "异常登录", "重置密码", "两步验证", "account compromised"],
    IntentCategory.TECHNICAL_LOGIN: ["无法登录", "登录失败", "登不上", "验证码收不到", "报401", "401 错误"],
    IntentCategory.TECHNICAL_CRASH: ["一直崩溃", "闪退", "白屏", "报500", "500 错误", "系统崩溃"],
}

# 命中锚点后的置信度：单条 0.82，每多命中一条 +0.05，上限 0.95。
_FAST_PATH_BASE_CONFIDENCE = 0.82
_FAST_PATH_STEP = 0.05
_FAST_PATH_MAX_CONFIDENCE = 0.95

_GENERIC_INTENTS = {
    IntentCategory.QUERY,
    IntentCategory.BILLING,
    IntentCategory.TECHNICAL,
    IntentCategory.ACCOUNT,
    IntentCategory.ESCALATION,
}

_INTENT_GROUPS: dict[IntentCategory, IntentCategory] = {
    IntentCategory.ORDER_STATUS: IntentCategory.QUERY,
    IntentCategory.LOGISTICS: IntentCategory.QUERY,
    IntentCategory.REFUND: IntentCategory.BILLING,
    IntentCategory.INVOICE: IntentCategory.BILLING,
    IntentCategory.PAYMENT_ISSUE: IntentCategory.BILLING,
    IntentCategory.ACCOUNT_SECURITY: IntentCategory.ACCOUNT,
    IntentCategory.TECHNICAL_LOGIN: IntentCategory.TECHNICAL,
    IntentCategory.TECHNICAL_CRASH: IntentCategory.TECHNICAL,
    IntentCategory.HUMAN_HANDOFF: IntentCategory.ESCALATION,
}

# 错误码识别：必须是 HTTP 4xx/5xx，或带明确前缀的错误码（ERR-1024 / E1001 /
# ERR_CONNECTION_RESET）。早期版本用 [A-Z][A-Z0-9_-]{2,16} 会把任意大写单词
# 当成错误码——"I NEED HELP" 会命中 NEED/HELP，进而给技术域错误加分导致误路由。
_ERROR_CODE_RE = re.compile(
    r"(?<![0-9A-Za-z¥￥$])("
    r"[45]\d{2}"
    r"|(?:ERR|ERROR|ERRNO|CODE|HTTP|SQLSTATE|E)[-_]?\d{3,6}"
    r"|ERR_[A-Z][A-Z0-9_]{2,24}"
    r")(?![0-9A-Za-z])"
    r"(?!\s*(?:元|块钱|块|美元|美金|usd|cny|rmb|dollar))",
    re.I,
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

    初始化时不加载任何本地模型，所有 AI 能力通过 Anthropic API 调用。
    模板 Embedding 在首次请求时懒加载并缓存，后续复用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
        embedding_base_url: str | None = None,
        embedding_api_key: str | None = None,
        embedding_model: str = "text-embedding-3-small",
        enable_fast_path: bool = True,
    ):
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        self.enable_fast_path = enable_fast_path

        # Embedding 这一路始终参与投票。配置了 OpenAI 兼容的 embeddings 端点时
        # 走远端真向量；没配置（例如当前用 DeepSeek 的 Anthropic 兼容协议，该协议
        # 不提供 embeddings 资源）时退化为本地字符 n-gram 哈希向量。
        # 换供应商是改环境变量的事，不需要改代码，三路融合链路始终成立。
        self._embedding_base_url = (embedding_base_url or "").rstrip("/") or None
        self._embedding_api_key  = embedding_api_key or None
        self._embedding_model    = embedding_model
        self._embedding_enabled  = True
        self._remote_embedding_ok = bool(self._embedding_base_url and self._embedding_api_key)

        self._tpl_embeddings: dict[IntentCategory, list[list[float]]] = {}
        self._cache: dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0
        self.fast_path_hits = 0
        self.ensemble_hits  = 0

    @property
    def embedding_backend(self) -> str:
        """当前 Embedding 这一路实际使用的后端，供 /health 和排障使用。"""
        return "remote" if self._remote_embedding_ok else "local_ngram"

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
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
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
        )
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

        prompt = f"""你是客服意图分析专家。根据示例判断用户意图，返回 JSON。
如果用户问题能匹配细粒度业务意图，请优先返回细粒度意图，而不是宽泛大类。
例如退款优先返回 refund，发票优先返回 invoice，登录故障优先返回 technical_login。

示例:
{examples}

{ctx}
用户消息: "{message}"

返回格式（仅 JSON，不要其他文字）:
{{"intent": "<意图值>", "confidence": <0-1>, "reasoning": "<一句话说明>"}}

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=256,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            try:
                data["intent"] = IntentCategory(data["intent"])
            except ValueError:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning(f"LLM 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败", "failed": True}

    async def _embedding_recognize(self, message: str) -> dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)

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
            IntentCategory.ORDER_STATUS: ["订单状态", "发货了吗", "处理到哪", "order status"],
            IntentCategory.LOGISTICS: ["物流", "快递", "配送", "运单", "delivery", "shipping"],
            IntentCategory.REFUND: ["退款", "退货", "refund", "return"],
            IntentCategory.INVOICE: ["发票", "抬头", "税号", "invoice"],
            IntentCategory.PAYMENT_ISSUE: ["重复扣款", "多扣", "支付失败", "扣费", "payment failed"],
            IntentCategory.ACCOUNT_SECURITY: ["被盗", "异常登录", "重置密码", "两步验证", "安全"],
            IntentCategory.TECHNICAL_LOGIN: ["无法登录", "登录失败", "401", "验证码"],
            IntentCategory.TECHNICAL_CRASH: ["崩溃", "闪退", "500", "报错", "crash"],
        }
        generic_patterns = {
            IntentCategory.ESCALATION: ["投诉", "经理", "supervisor"],
            IntentCategory.COMPLAINT:  ["太差", "糟糕", "horrible", "等了很久"],
            IntentCategory.QUERY:      ["?", "？", "怎么", "什么", "status"],
            IntentCategory.REQUEST:    ["帮我", "需要", "please", "help"],
            IntentCategory.GREETING:   ["你好", "嗨", "hello", "hi"],
            IntentCategory.BILLING:    ["退款", "扣款", "发票", "refund"],
            IntentCategory.TECHNICAL:  ["崩溃", "报错", "error", "crash"],
            IntentCategory.ACCOUNT:    ["密码", "邮箱", "账户", "password"],
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
            # LLM 这一路挂了，剩余两路仍能给出结果——这是三路融合的容错价值。
            if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
                return emb["intent"], source_scores["embedding"], source_scores
            if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
                return pat["intent"], source_scores["rules"], source_scores
            return IntentCategory.OTHER, 0.0, source_scores

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
            "amount": self._unique(re.findall(r"((?:¥|￥)\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*(?:元|块|rmb|cny|usd|美元))", message, re.I)),
            "error_code": self._unique(_ERROR_CODE_RE.findall(message)),
        }

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """懒加载所有模板的 Embedding（只在首次调用时执行）。"""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> list[float]:
        """
        生成文本向量。

        优先调用配置好的 OpenAI 兼容 embeddings 端点（EMBEDDING_BASE_URL /
        EMBEDDING_API_KEY）；没配置或调用失败时退化为字符 n-gram 哈希向量。
        无论走哪条路，Embedding 都会作为独立的一路参与投票——这是"三路融合"
        在换供应商时依然成立的原因。
        """
        if self._remote_embedding_ok:
            try:
                return await self._remote_embed(text)
            except Exception as ex:
                # 一次失败就永久降级，避免每条消息都白等一次网络超时
                self._remote_embedding_ok = False
                logger.warning(f"远端 Embedding 不可用，本轮起使用本地向量: {ex}")

        return self._local_embedding(text)

    async def _remote_embed(self, text: str) -> list[float]:
        """调用 OpenAI 兼容的 /embeddings 接口。用 httpx 直连，不额外引入 SDK。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._embedding_base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._embedding_api_key}"},
                json={"model": self._embedding_model, "input": text},
            )
            resp.raise_for_status()
            return list(resp.json()["data"][0]["embedding"])

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
