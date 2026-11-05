from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Protocol

from ai_sql_agent.memory import ConversationMemoryContext

from .token_budget import trim_to_tokens

# 路由提示词中的上下文 Token 预算，与主链路共享 ~8k 总预算口径。
ROUTING_CONTEXT_TOKEN_BUDGET = 2000


class RouteType(str, Enum):
    DATABASE_QUERY = "database_query"
    DATA_QA = "data_qa"


@dataclass(frozen=True)
class RouteDecision:
    route: RouteType
    confidence: float
    reason: str
    signals: List[str] = field(default_factory=list)

    @property
    def needs_database(self) -> bool:
        return self.route == RouteType.DATABASE_QUERY

    def to_dict(self) -> dict:
        return {
            "route": self.route.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "signals": self.signals,
            "needs_database": self.needs_database,
        }


class IntentClassifier(Protocol):
    def classify(
        self, query: str, context: ConversationMemoryContext
    ) -> RouteDecision: ...


class RuleBasedIntentClassifier:
    """离线意图路由器，优先避免历史结果追问重复访问数据库。"""

    RESULT_REFERENCES = (
        "这个结果",
        "这些结果",
        "上述结果",
        "上面的结果",
        "刚才",
        "之前查询",
        "上次查询",
        "查询结果",
        "这些数据",
        "这批数据",
    )
    EXPLANATION_TERMS = (
        "解释",
        "解读",
        "分析",
        "总结",
        "说明",
        "为什么",
        "原因",
        "含义",
        "定义",
        "口径",
        "什么意思",
        "怎么看",
    )
    KNOWLEDGE_PATTERNS = (
        r"什么是",
        r"如何理解",
        r"怎么定义",
        r"业务口径",
        r"指标.*(?:含义|口径|定义)",
        r"(?:含义|口径|定义)是什么",
    )
    DATABASE_TERMS = (
        "查询",
        "查一下",
        "查出",
        "统计",
        "筛选",
        "列出",
        "获取",
        "计算",
        "汇总",
        "排名",
        "趋势",
        "多少",
        "最高",
        "最低",
        "平均",
        "总数",
        "总量",
        "同比",
        "环比",
    )
    FRESH_DATA_TERMS = (
        "重新查询",
        "重新统计",
        "最新",
        "实时",
        "现在",
        "今天",
        "昨日",
        "本周",
        "本月",
        "本季度",
        "今年",
        "过去",
        "最近",
    )

    # 上面几张词表的英文对应。只有中文词表时，英文问句一个词也命不中，全部落到最后的
    # 「默认对话问答」——模型路由一失灵（超时、返回格式不对、服务挂了），用英文问
    # 「哪些供应商准时率低于 0.9」得到的就是一句「上下文里信息不够」，而不是一次查询。
    # 英文按**整词**匹配（`list` 不该命中 price_list，`per` 不该命中 person）。
    RESULT_REFERENCES_EN = re.compile(
        r"\b(?:this result|these results|the results? above|above results?|"
        r"(?:previous|last|earlier) (?:result|results|query|answer)|"
        r"that (?:result|table|answer)|those (?:rows|results|records)|just now)\b",
        re.I,
    )
    EXPLANATION_TERMS_EN = re.compile(
        r"\b(?:explain|interpret|analy[sz]e|analysis|summari[sz]e|summary|why|reason|"
        r"meaning|definition|define[sd]?)\b",
        re.I,
    )
    KNOWLEDGE_PATTERNS_EN = re.compile(
        r"\bwhat does .+ mean\b|\b(?:definition|meaning) of\b|"
        r"\bhow (?:is|are) .+ (?:defined|calculated|computed|measured)\b",
        re.I,
    )
    # re-run / re-query 两边都列：「再查一次」本身就是一次查询请求，
    # 与中文的「重新查询」「重新统计」同时命中两张表是同一个道理。
    DATABASE_TERMS_EN = re.compile(
        r"\b(?:which|list|show|find|look up|get|give me|how many|how much|count|totals?|sum|"
        r"average|avg|top|highest|lowest|most|least|rank(?:ed|ing)?|compare|per|each|every|"
        r"re-?run|re-?query|"
        r"what (?:is|are|was|were) the|who (?:is|are|has|have))\b",
        re.I,
    )
    FRESH_DATA_TERMS_EN = re.compile(
        r"\b(?:latest|current|currently|today|yesterday|right now|up[- ]to[- ]date|recent|recently|"
        r"(?:this|last|past) (?:week|month|quarter|year)|re-?run|re-?query|refresh(?:ed)?)\b",
        re.I,
    )

    def classify(
        self, query: str, context: ConversationMemoryContext
    ) -> RouteDecision:
        normalized = " ".join(query.strip().split())
        if not normalized:
            return RouteDecision(
                route=RouteType.DATA_QA,
                confidence=1.0,
                reason="空问题不应触发数据库查询",
                signals=["empty_query"],
            )

        has_result = self._has_available_result(context)
        result_reference = self._matches_any(normalized, self.RESULT_REFERENCES) or bool(
            self.RESULT_REFERENCES_EN.search(normalized)
        )
        explanation = self._matches_any(normalized, self.EXPLANATION_TERMS) or bool(
            self.EXPLANATION_TERMS_EN.search(normalized)
        )
        knowledge = any(
            re.search(pattern, normalized) for pattern in self.KNOWLEDGE_PATTERNS
        ) or bool(self.KNOWLEDGE_PATTERNS_EN.search(normalized))
        database_request = self._matches_any(normalized, self.DATABASE_TERMS) or bool(
            self.DATABASE_TERMS_EN.search(normalized)
        )
        fresh_data = self._matches_any(normalized, self.FRESH_DATA_TERMS) or bool(
            self.FRESH_DATA_TERMS_EN.search(normalized)
        )

        if fresh_data and database_request:
            return RouteDecision(
                route=RouteType.DATABASE_QUERY,
                confidence=0.98,
                reason="问题明确要求获取或重新计算时效性业务数据",
                signals=["fresh_data", "database_operation"],
            )

        if has_result and result_reference:
            return RouteDecision(
                route=RouteType.DATA_QA,
                confidence=0.97,
                reason="问题引用当前会话或个人知识库中的历史查询结果",
                signals=["available_result", "result_reference"],
            )

        if knowledge:
            return RouteDecision(
                route=RouteType.DATA_QA,
                confidence=0.96,
                reason="问题属于指标定义、业务口径或知识解释",
                signals=["knowledge_question"],
            )

        if explanation and not database_request:
            return RouteDecision(
                route=RouteType.DATA_QA,
                confidence=0.92,
                reason="问题要求解释、分析或总结，而非获取新数据",
                signals=["explanation_request"],
            )

        if database_request:
            return RouteDecision(
                route=RouteType.DATABASE_QUERY,
                confidence=0.90,
                reason="问题包含查询、统计、筛选或指标计算意图",
                signals=["database_operation"],
            )

        if has_result and explanation:
            return RouteDecision(
                route=RouteType.DATA_QA,
                confidence=0.90,
                reason="已有可用结果，当前问题可由对话问答链路完成",
                signals=["available_result", "explanation_request"],
            )

        return RouteDecision(
            route=RouteType.DATA_QA,
            confidence=0.65,
            reason="未发现必须访问数据库的明确意图，默认使用低成本对话问答",
            signals=["safe_default"],
        )

    @staticmethod
    def _matches_any(query: str, terms: tuple[str, ...]) -> bool:
        return any(term in query for term in terms)

    @staticmethod
    def _has_available_result(context: ConversationMemoryContext) -> bool:
        if context.long_term_hits:
            return True
        return any(
            message.message_type == "sql_result"
            for message in context.short_term.messages
        )


class TextGenerationClient(Protocol):
    def generate(self, prompt: str) -> str: ...


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": [RouteType.DATABASE_QUERY.value, RouteType.DATA_QA.value]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["route", "confidence", "reason"],
    "additionalProperties": False,
}
"""路由结果的 JSON Schema。OpenAI 兼容协议下由服务端按它约束输出，不再从一段文字里找 JSON。"""


class ModelIntentClassifier:
    """使用大模型进行二分类；返回格式异常时由上层回退到规则。"""

    def __init__(self, client: TextGenerationClient):
        self.client = client

    def classify(
        self, query: str, context: ConversationMemoryContext
    ) -> RouteDecision:
        context_text = trim_to_tokens(
            context.to_prompt_context(), ROUTING_CONTEXT_TOKEN_BUDGET, keep="tail"
        )
        prompt = f"""你是 AI SQL Agent 意图路由器。只能选择以下路由之一：
- database_query：必须查询实时或当前业务数据库，包含查询、筛选、统计、计算新数据。
- data_qa：结果解释、指标口径、数据分析、历史结果追问、业务知识问答，不应重复查库。

如果上下文已经包含足够的历史查询结果，优先 data_qa；只有确实需要新数据时才选 database_query。
只输出 JSON：{{"route":"database_query|data_qa","confidence":0到1,"reason":"简短原因"}}

上下文：
{context_text or '无'}

当前问题：{query}
"""
        structured = getattr(self.client, "generate_structured", None)
        raw = (
            structured(prompt, ROUTE_SCHEMA, "route_decision")
            if callable(structured)
            else self.client.generate(prompt)
        ).strip()
        payload = self._parse_json(raw)
        route = RouteType(payload["route"])
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.8))))
        return RouteDecision(
            route=route,
            confidence=confidence,
            reason=str(payload.get("reason") or "模型意图分类"),
            signals=["model_classifier"],
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", fenced, flags=re.DOTALL)
            if not match:
                raise ValueError(f"路由模型未返回 JSON：{text}")
            return json.loads(match.group(0))


class DynamicIntentRouter:
    """模型分类优先、规则分类兜底的动态路由门面。"""

    def __init__(
        self,
        model_classifier: Optional[IntentClassifier] = None,
        fallback_classifier: Optional[IntentClassifier] = None,
    ):
        self.model_classifier = model_classifier
        self.fallback_classifier = fallback_classifier or RuleBasedIntentClassifier()

    def route(self, query: str, context: ConversationMemoryContext) -> RouteDecision:
        if self.model_classifier:
            try:
                return self.model_classifier.classify(query, context)
            except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
                pass
        return self.fallback_classifier.classify(query, context)
