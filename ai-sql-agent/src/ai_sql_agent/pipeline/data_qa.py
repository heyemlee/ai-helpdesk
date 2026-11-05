from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Protocol

from ai_sql_agent.memory import ConversationMemoryContext


class DataQAClient(Protocol):
    def answer(self, query: str, context: ConversationMemoryContext) -> str: ...


def _load_metric_definitions() -> Dict[str, str]:
    """当前语义层里的业务指标定义。取不到时返回空表，问答链路照常工作。"""
    try:
        from ai_sql_agent.semantic import active_semantic_layer

        return dict(active_semantic_layer().metric_definitions)
    except Exception:
        # 指标定义是增强项：语义层配错了应该在启动与校验时暴露，
        # 不该让一次「这个指标什么意思」的提问变成 500。
        return {}


class RuleBasedDataQAClient:
    """无模型环境下的对话问答降级实现，用于验证路由与结果复用。"""

    def __init__(self, definitions: Optional[Dict[str, str]] = None):
        """
        definitions 为空时从语义层读取。

        以前这份定义是写死在类里的常量，只覆盖四个演示库的指标；
        接真实业务库时用户问「这个口径怎么算」只能得到「没有足够信息」。
        现在它跟着 semantic/_shared.yaml 的 metric_definitions 走。
        """
        self.definitions = (
            definitions if definitions is not None else _load_metric_definitions()
        )

    def answer(self, query: str, context: ConversationMemoryContext) -> str:
        if any(term in query for term in ("什么是", "含义", "定义", "口径", "什么意思")):
            for name, definition in self.definitions.items():
                if name in query:
                    return definition

        result = self._latest_sql_result(context)
        if result is not None:
            return self._answer_from_result(query, result)

        # 固定答复跟着问题的语言走：用英文提问的人读不了一句中文的「信息不够」
        english = not any("\u4e00" <= char <= "\u9fff" for char in query)

        if context.long_term_hits:
            summaries = [hit.memory.summary for hit in context.long_term_hits[:3]]
            lead = (
                "From your saved results:" if english else "根据个人知识库中的历史内容："
            )
            return lead + "\n" + "\n".join(
                f"{index}. {summary}" for index, summary in enumerate(summaries, start=1)
            )

        for name, definition in self.definitions.items():
            if name in query:
                return definition

        if english:
            return (
                "There is not enough in this conversation to answer that, and it was not "
                "treated as a request for new data. If you need figures from the database, "
                "ask for them directly — for example “list …” or “how many …”."
            )
        return "当前上下文中没有足够的信息回答该问题，但该请求不需要重新查询数据库。"

    @staticmethod
    def _latest_sql_result(context: ConversationMemoryContext) -> Dict[str, Any] | None:
        for message in reversed(context.short_term.messages):
            if message.message_type != "sql_result" or not message.payload:
                continue
            logs = message.payload.get("step_logs", [])
            if logs:
                return logs[-1].get("execution_result")
        for hit in context.long_term_hits:
            content = hit.memory.content
            if not isinstance(content, dict):
                continue
            result = content.get("result", content)
            logs = result.get("step_logs", []) if isinstance(result, dict) else []
            if logs:
                return logs[-1].get("execution_result")
        return None

    @staticmethod
    def _answer_from_result(query: str, result: Dict[str, Any]) -> str:
        if not result.get("success", True):
            return f"最近一次数据库查询未成功：{result.get('error') or '未知错误'}"
        rows: List[Dict[str, Any]] = result.get("rows", [])
        columns = result.get("columns", [])
        if not rows:
            return "最近一次查询没有返回数据。"
        values = [row.get(columns[0]) for row in rows] if len(columns) == 1 else []
        if values:
            numeric_values = [value for value in values if isinstance(value, (int, float))]
            details = f"最近一次查询返回 {len(rows)} 条记录，{columns[0]} 分别为：{', '.join(map(str, values))}。"
            if numeric_values and any(term in query for term in ("分析", "总结", "解读", "最高", "最低")):
                details += (
                    f"其中最小值为 {min(numeric_values)}，最大值为 {max(numeric_values)}，"
                    f"平均值约为 {sum(numeric_values) / len(numeric_values):.4f}。"
                )
            return details
        return "最近一次查询结果如下：" + json.dumps(rows, ensure_ascii=False, default=str)


class ModelDataQAClient:
    """基于记忆上下文回答，不具备数据库执行能力。"""

    def __init__(self, client, definitions: Optional[Dict[str, str]] = None):
        self.client = client
        self.definitions = (
            definitions if definitions is not None else _load_metric_definitions()
        )
        """
        业务指标定义，来自语义层。

        注入它是因为口径问题不能靠模型编：用户问「超预算怎么算的」，
        模型按常识答一个听起来合理的定义，与这家公司实际的算法可能不同，
        而用户无从分辨。有定义就照抄，没有就明说不知道。
        """

    def answer(self, query: str, context: ConversationMemoryContext) -> str:
        context_text = context.to_prompt_context()
        if len(context_text) > 12000:
            context_text = context_text[-12000:]
        glossary = "\n".join(
            f"- {term}：{definition}" for term, definition in self.definitions.items()
        )
        glossary_section = (
            f"""
业务指标口径（这是本公司的口径，解释指标时必须以此为准，不要按常识另行发挥）：
{glossary}
"""
            if glossary
            else ""
        )
        # 作答语言跟着问题走。这段话是这条链路上唯一直接给人读的模型原文——
        # 查库路径的答案由页面按结构化结果自己拼，不经过模型；这里不说清楚的话，
        # 上下文与口径都是中文，英文提问得到的也会是一段中文。
        prompt = f"""你是 AI SQL Agent 数据问答助手。请只依据给定上下文回答当前问题。
你负责结果解释、指标说明、数据分析、历史结果追问和业务知识问答。
不得声称执行了新的数据库查询；上下文不足时明确说明缺少什么信息。
用与「当前问题」相同的语言作答（问题是英文就用英文作答）；转述业务指标口径时只换语言，不改含义。
{glossary_section}
上下文：
{context_text or '无'}

当前问题：{query}
"""
        return self.client.generate(prompt).strip()
