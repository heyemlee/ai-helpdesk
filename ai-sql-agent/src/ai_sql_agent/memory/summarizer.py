from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence

from .objects import MemoryKind, MemoryMessage, MessageRole


class MemorySummarizer(Protocol):
    def summarize_conversation(
        self, previous_summary: str, messages: Sequence[MemoryMessage]
    ) -> str: ...

    def summarize_memory(
        self,
        *,
        kind: MemoryKind,
        content: Any,
        metadata: dict[str, Any],
    ) -> str: ...


class ExtractiveMemorySummarizer:
    """无模型配置时的可运行降级实现，保留事实而非生成新结论。"""

    def __init__(self, max_chars: int = 4000):
        self.max_chars = max_chars

    def summarize_conversation(
        self, previous_summary: str, messages: Sequence[MemoryMessage]
    ) -> str:
        lines: list[str] = []
        if previous_summary:
            lines.append(f"已有背景：{previous_summary}")
        for message in messages:
            role = "用户" if message.role == MessageRole.USER else "助手"
            content = " ".join(message.content.split())
            if message.message_type in {"sql_result", "analysis"} and message.payload:
                payload = json.dumps(message.payload, ensure_ascii=False, default=str)
                content = f"{content}；关键数据：{payload}"
            lines.append(f"{role}：{content}")
        return self._truncate("\n".join(lines))

    def summarize_memory(
        self,
        *,
        kind: MemoryKind,
        content: Any,
        metadata: dict[str, Any],
    ) -> str:
        query = str(metadata.get("query") or metadata.get("title") or "").strip()
        if isinstance(content, str):
            body = " ".join(content.split())
        else:
            body = json.dumps(content, ensure_ascii=False, default=str)
        prefix = "结构化查询记忆" if kind == MemoryKind.STRUCTURED else "业务知识记忆"
        database = metadata.get("database")
        details = [prefix]
        if query:
            details.append(f"问题：{query}")
        if database:
            details.append(f"数据库：{database}")
        details.append(f"核心内容：{body}")
        return self._truncate("；".join(details))

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        marker = "\n……（中间内容已压缩）……\n"
        available = self.max_chars - len(marker)
        head_size = max(1, available * 2 // 5)
        tail_size = max(1, available - head_size)
        return text[:head_size] + marker + text[-tail_size:]


class TextGenerationClient(Protocol):
    def generate(self, prompt: str) -> str: ...


_MIN_PAYLOAD_CHARS = 200
_MAX_EXCERPT_STEPS = 5
_NAME_CHARS = 64
_ERROR_CHARS = 300

_SAMPLED_ROWS_NOTE = (
    "注意：新增对话里有查询结果没有附全。payload.steps 中 rows_omitted 大于 0 的步骤，"
    "rows 只是结果开头的若干行样例，另有 rows_omitted 行未附，row_count 才是该步骤返回的行数。"
    "不要把样例当成完整结果，也不要根据样例推算合计、最值、排名或占比。"
)
_TRUNCATED_RESULT_NOTE = (
    "注意：truncated 为 true 的步骤，查询在执行时已被行数上限 row_limit 截断，"
    "符合条件的记录多于 row_count 行，摘要里不要把 row_count 写成总数。"
)


@dataclass
class SummaryBudgetConfig:
    """
    模型摘要器从**一条消息**里最多读多少，按字符计。

    为什么要有它：查库回答把整份 `PipelineResult.to_dict()` 存成消息的 payload——
    Schema 上下文、规划文本、每一步的全部结果行。摘要提示词以前把它原样拼进去，
    一条顶到行数上限（1000 行）的结果就是约 15 万字符，并且随行数上限线性增长。
    这不是攒够很多轮才会遇到的事：短期窗口按同一份 payload 估 token，
    这样的回答在用户说下一句话时就被挤出窗口、送来摘要。

    这里限的只是**摘要器读到的内容**，存下来的消息一个字不动：
    窗口内的追问（RuleBasedDataQAClient）要从完整的 payload 里读结果行。

    一次摘要调用的总量仍然是「条数 × 单条上限」，条数由
    `ShortTermMemoryConfig.summary_batch_size` 决定。
    """

    max_content_chars: int = 4000
    """
    消息正文，按原文字符计。与抽取式摘要的总长上限取同一个数。

    正常的提问和回答到不了这里（查库回答里的结果预览本身就截在 1500 字符）；
    挡的是没有长度上限的提问，以及规则版问答把整份结果贴进回答的情况。
    """

    max_payload_chars: int = 3000
    """
    一条消息的 payload 摘录，按它在提示词里的 JSON 文本计。

    够放下问题、SQL、列名，再加一二十行常见宽度的结果——
    「前十名」「按月十二行」这类小结果整份都在，摘要里的数字不会因此变少。
    """

    max_sql_chars: int = 1200
    """一步的 SQL。多步查询时各步平分 payload 预算的四成，实际上限取两者中较小的。"""

    max_cell_chars: int = 200
    """结果行里的一个单元格。长文本列截短后整行仍然留得下，不至于因为放不下被整行略去。"""

    def __post_init__(self) -> None:
        if self.max_content_chars < 1:
            raise ValueError("max_content_chars 必须大于 0")
        if self.max_payload_chars < _MIN_PAYLOAD_CHARS:
            # 再小就放不下「payload 被截过」这句说明本身
            raise ValueError(f"max_payload_chars 不能小于 {_MIN_PAYLOAD_CHARS}")
        if self.max_sql_chars < 1:
            raise ValueError("max_sql_chars 必须大于 0")
        if self.max_cell_chars < 1:
            raise ValueError("max_cell_chars 必须大于 0")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _squeeze(text: str, limit: int) -> str:
    """超长文本留下首尾，中间换成一句写明省略了多少字符的标记。"""
    if len(text) <= limit:
        return text
    # 先按最长的标记留位置：真正省略的字数只会比全文少，位数不会更多
    available = limit - len(f"……（此处省略 {len(text)} 字符）……")
    if available < 2:
        return text[:limit]
    # SQL 的筛选条件、长回答的结论都偏后，尾部多留一些
    head = available * 2 // 5
    tail = available - head
    return f"{text[:head]}……（此处省略 {len(text) - head - tail} 字符）……{text[-tail:]}"


def _bounded_cell(value: Any, limit: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else _dumps(value)
    if len(text) <= limit:
        return value
    return f"{text[:limit]}…（共 {len(text)} 字符）"


def _raw_excerpt(text: str, limit: int) -> dict[str, Any]:
    """认不出结构、或摘录之后仍然放不下的 payload：只留 JSON 文本的开头，并写明原长。"""
    note = f"payload 的 JSON 共 {len(text)} 字符，此处只保留开头"
    keep = limit
    while keep > 0:
        excerpt = {"payload_excerpt": text[:keep], "note": note}
        # 引号、换行转义后会变长；少留一个原文字符，编码后至少短一个字符
        overshoot = len(_dumps(excerpt)) - limit
        if overshoot <= 0:
            return excerpt
        keep -= overshoot
    return {"note": note}


def _step_outline(
    log: dict[str, Any], budget: SummaryBudgetConfig, text_share: int
) -> tuple[dict[str, Any], list]:
    """一步查询除结果行以外的要素，以及它的全部结果行（行由调用方按剩余预算再填）。"""
    execution = log.get("execution_result")
    if not isinstance(execution, dict):
        execution = {}
    rows = execution.get("rows")
    if not isinstance(rows, list):
        rows = []

    step: dict[str, Any] = {}
    database = log.get("database") or execution.get("database")
    if database:
        step["database"] = _squeeze(str(database), _NAME_CHARS)
    # final_sql 是修复之后真正跑出这份结果的那一条
    sql = log.get("final_sql") or log.get("sql") or execution.get("sql")
    if sql:
        step["sql"] = _squeeze(str(sql), min(budget.max_sql_chars, text_share))
    if "success" in execution:
        step["success"] = bool(execution["success"])
    if execution.get("error"):
        step["error"] = _squeeze(str(execution["error"]), min(_ERROR_CHARS, text_share))

    row_count = execution.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        row_count = len(rows)
    step["row_count"] = row_count
    truncated = execution.get("truncated")
    if isinstance(truncated, bool):
        step["truncated"] = truncated
    row_limit = execution.get("row_limit")
    if isinstance(row_limit, int) and not isinstance(row_limit, bool):
        step["row_limit"] = row_limit

    columns = execution.get("columns")
    if isinstance(columns, list) and columns:
        names: list[str] = []
        used = 0
        for column in columns:
            name = _squeeze(str(column), _NAME_CHARS)
            used += len(name) + 4  # 引号、逗号和空格
            if used > text_share // 3:
                break
            names.append(name)
        step["columns"] = names
        if len(names) < len(columns):
            step["columns_omitted"] = len(columns) - len(names)

    step["rows"] = []
    # 先按「一行都没附」记：附上行之后这个数只会变小，占的位数不会变多
    step["rows_omitted"] = max(row_count, len(rows))
    return step, rows


def _result_excerpt(
    payload: dict[str, Any], budget: SummaryBudgetConfig
) -> tuple[dict[str, Any], bool, bool]:
    """
    查库结果的 payload（`PipelineResult.to_dict()` 的形状）→ 摘要器读的摘录。

    留下：问题、每一步的库名、实际执行的 SQL、成功与否和报错、返回行数、
    是否被行数上限截断、列名、开头的若干行。
    不留：Schema 上下文、规划文本、关键词、修复过程、重复出现的 SQL 和数据时点——
    它们要么只服务于生成 SQL，要么已经体现在最终的 SQL 和结果里。

    返回值的后两项：是否有步骤的行没附全、是否有步骤在执行时就被行数上限截断过。
    """
    all_logs = [log for log in payload["step_logs"] if isinstance(log, dict)]
    logs = all_logs[-_MAX_EXCERPT_STEPS:]
    # SQL、报错、列名这些文字按步平分预算的四成，其余留给结果行
    text_share = budget.max_payload_chars * 2 // 5 // max(1, len(logs))

    excerpt: dict[str, Any] = {}
    if payload.get("query"):
        excerpt["query"] = _squeeze(str(payload["query"]), budget.max_payload_chars // 10)
    if payload.get("degraded"):
        excerpt["degraded"] = True
        excerpt["degrade_reason"] = _squeeze(str(payload.get("degrade_reason") or ""), _NAME_CHARS)
    if len(all_logs) > len(logs):
        excerpt["steps_omitted"] = len(all_logs) - len(logs)
    outlines = [_step_outline(log, budget, text_share) for log in logs]
    excerpt["steps"] = [step for step, _ in outlines]

    # 剩下的预算从最后一步往前填结果行：最后一步才是用户看到的答案
    used = len(_dumps(excerpt))
    for step, rows in reversed(outlines):
        for row in rows:
            if isinstance(row, dict):
                kept: Any = {
                    str(key): _bounded_cell(value, budget.max_cell_chars)
                    for key, value in row.items()
                }
            else:
                kept = _bounded_cell(row, budget.max_cell_chars)
            cost = len(_dumps(kept)) + 2
            if used + cost > budget.max_payload_chars:
                break
            step["rows"].append(kept)
            used += cost
        step["rows_omitted"] -= len(step["rows"])

    sampled = any(step["rows_omitted"] > 0 for step, _ in outlines)
    truncated = any(step.get("truncated") is True for step, _ in outlines)
    return excerpt, sampled, truncated


def _bounded_payload(
    payload: dict[str, Any], budget: SummaryBudgetConfig
) -> tuple[Any, bool, bool]:
    """一条消息的 payload → 放得进预算的摘录，外加两个要不要在提示词里提醒的标记。"""
    if not payload:
        return payload, False, False
    excerpt, sampled, truncated = payload, False, False
    if isinstance(payload, dict) and isinstance(payload.get("step_logs"), list):
        excerpt, sampled, truncated = _result_excerpt(payload, budget)
    text = _dumps(excerpt)
    if len(text) > budget.max_payload_chars:
        excerpt = _raw_excerpt(text, budget.max_payload_chars)
    return excerpt, sampled, truncated


class ModelMemorySummarizer:
    """通过项目使用的任意文本生成 Client 完成摘要。"""

    def __init__(
        self,
        client: TextGenerationClient,
        budget: Optional[SummaryBudgetConfig] = None,
    ):
        self.client = client
        self.budget = budget or SummaryBudgetConfig()

    def summarize_conversation(
        self, previous_summary: str, messages: Sequence[MemoryMessage]
    ) -> str:
        records = []
        sampled = truncated = False
        for message in messages:
            payload, rows_left_out, hit_row_limit = _bounded_payload(message.payload, self.budget)
            sampled = sampled or rows_left_out
            truncated = truncated or hit_row_limit
            records.append(
                {
                    "role": message.role.value,
                    "content": _squeeze(message.content, self.budget.max_content_chars),
                    "message_type": message.message_type,
                    "payload": payload,
                }
            )
        # 行没附全时必须在规则里说出来：不说的话，模型会把开头几行当成整份结果去总结。
        # 没有省略时规则部分一个字都不加。
        notes = "".join(
            f"{note}\n"
            for note, applies in (
                (_SAMPLED_ROWS_NOTE, sampled),
                (_TRUNCATED_RESULT_NOTE, truncated),
            )
            if applies
        )
        prompt = f"""你是会话记忆压缩器。请把已有摘要与新增对话合并成一份事实性摘要。
必须保留：用户目标、已确认业务口径、筛选条件、关键 SQL/结果、分析结论、未完成任务。
不得补充原文没有的信息。直接输出摘要正文。
{notes}
已有摘要：
{previous_summary or '无'}

新增对话：
{json.dumps(records, ensure_ascii=False, default=str)}
"""
        return self.client.generate(prompt).strip()

    def summarize_memory(
        self,
        *,
        kind: MemoryKind,
        content: Any,
        metadata: dict[str, Any],
    ) -> str:
        prompt = f"""你是长期记忆索引摘要器。请生成适合向量检索的业务摘要。
保留问题目标、业务口径、筛选条件、指标数值、关键结论；不要编造信息。
记忆类型：{kind.value}
元信息：{json.dumps(metadata, ensure_ascii=False, default=str)}
原始内容：{json.dumps(content, ensure_ascii=False, default=str)}
直接输出摘要正文。
"""
        return self.client.generate(prompt).strip()
