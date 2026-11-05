from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from ai_sql_agent.core.model_text import plain_text
from ai_sql_agent.core.run_context import RunCancelled

from .objects import (
    SELF_CHECK_MISMATCH,
    SELF_CHECK_NOT_APPLICABLE,
    SELF_CHECK_NOT_RUN,
    SELF_CHECK_PASSED,
    AnomalyType,
    ValidationFinding,
)


@dataclass
class SelfCheckResult:
    """一次语义自检的结果。`status` 说的是**做没做成**，不只是有没有发现。"""

    status: str
    """passed / mismatch / not_run / not_applicable（常量见 objects.py）。"""

    finding: Optional[ValidationFinding] = None
    paraphrase: Optional[str] = None
    error: str = ""
    """没做成时的异常类名。"""


class ParaphraseClient(Protocol):
    """让模型用自然语言复述 SQL 实际算了什么。"""

    def generate(self, prompt: str) -> str: ...


_PARAPHRASE_PROMPT = """你是一个SQL审阅助手。

请用一句中文说明下面这条SQL**实际**做了什么：筛选了什么、按什么分组、输出了什么。
只描述SQL字面执行的内容，不要推测意图，不要评价对错，不要输出SQL。

SQL：
{sql}

输出格式：
复述：<一句话>
"""

_COMPARE_PROMPT = """你是一个语义一致性审阅助手。

判断「SQL 的实际行为」是否回答了「用户的问题」。
只看是否答非所问（例如问的是金额却统计了数量、问的是A项目却查了B项目、
漏掉了问题里明确要求的筛选条件）。措辞差异、字段命名差异不算不一致。

用户问题：{query}
SQL 的实际行为：{paraphrase}
{facts}
输出格式（严格遵守，只输出两行）：
结论：一致 或 不一致
理由：<一句话>
"""

_MULTI_STEP_COMPARE_PROMPT = """你是一个语义一致性审阅助手。

这个问题被拆成了多个步骤在**不同数据库**里分别执行，下面是**最后一步**。
前面的步骤已经执行完毕，它们的筛选结果已经以字面量（如 IN ('A','B')）的形式
写进了最后一步的 SQL 里。

因此：最后一步的 SQL **本来就不会**再出现前置步骤的筛选条件和表，
这是正确的设计，不是遗漏。看到 SQL 里有一串看不出来历的字面量取值，
应当理解为「前置步骤算出来的结果」，不要因此判为不一致。

判断标准只有一条：在前置步骤已经完成的前提下，最后一步是否正确地
完成了剩下的工作（该聚合的聚合、该输出的字段输出）。

用户问题：{query}
前置步骤已完成：{preceding}
最后一步 SQL 的实际行为：{paraphrase}
{facts}
输出格式（严格遵守，只输出两行）：
结论：一致 或 不一致
理由：<一句话>
"""

_UNIT_FACTS = """已知事实（来自数据字典，不在 SQL 里）：结果列的单位是 {units}。
这些列存的本来就是这个单位，SQL 不需要再按单位或币种筛选、换算；
问题里要的是同一个单位时不算不一致，要的是**别的**单位而 SQL 没有换算才算。
"""
"""
审阅者只看得到 SQL 的复述，看不到数据字典。问题里写了「以美元计」，而 SQL 里
自然没有任何币种字样——金额列存的本来就是美元——它就会判成「漏掉了币种条件」。
这不是它判断力的问题，是它缺一条事实；把这条事实给它，而不是让它猜。

没有声明单位的列不出现在这里，那时提示词与原来逐字相同。
"""


class SemanticSelfChecker:
    """
    语义自检。

    做法分两步，而不是直接问模型「这条SQL对不对」：
    1. 先让模型**只复述** SQL 字面行为——不给它看问题，避免它顺着问题脑补；
    2. 再把复述与原问题一起给模型判一致性。

    这样拆开的原因：一步到位地问「对不对」时，模型倾向于回答「对」，
    因为它同时看到了问题和SQL，会自动把两者对齐。先隔离复述能显著减少这种偏袒。
    """

    def __init__(self, client: Optional[ParaphraseClient] = None):
        self.client = client

    @property
    def available(self) -> bool:
        return self.client is not None

    def check(
        self,
        query: str,
        sql: str,
        preceding_summary: str = "",
        known_units: Optional[Dict[str, str]] = None,
    ) -> tuple[Optional[ValidationFinding], Optional[str]]:
        """
        返回 (发现, 复述文本)。模型不可用时返回 (None, None)，不阻断主链路。

        只想知道「有没有发现」时用它。**它分不出「做了、没问题」和「没做成」**——
        要区分这两种（校验器必须区分）用 `run`。
        """
        result = self.run(
            query, sql, preceding_summary=preceding_summary, known_units=known_units
        )
        return result.finding, result.paraphrase

    def run(
        self,
        query: str,
        sql: str,
        preceding_summary: str = "",
        known_units: Optional[Dict[str, str]] = None,
    ) -> "SelfCheckResult":
        """
        做一次语义自检，并如实说明做没做成。

        preceding_summary 非空表示这是多步计划的最后一步，比对时要告诉审阅者
        前面已经做完了什么。不告诉它的后果是系统性误报：跨库查询的最后一步
        只做「后半段」，拿原问题去比必然判成「漏掉了筛选条件」——
        实测跨库样本的误报率因此达到 100%，等于把提示变成噪音。

        客户端此刻在用 Mock 回答（没有 Key、允许走 Mock）时按「不适用」算：
        Mock 不做语义判断，读不出结论是必然的，记成「没做成」会让每条离线答案
        都带上风险提示。每次调用时现问——页面保存了 Key 之后，下一次就会真做。
        """
        if not self.client or not sql.strip():
            return SelfCheckResult(status=SELF_CHECK_NOT_APPLICABLE)
        answers_from_mock = getattr(self.client, "answers_from_mock", None)
        if callable(answers_from_mock) and answers_from_mock():
            return SelfCheckResult(status=SELF_CHECK_NOT_APPLICABLE)

        facts = ""
        if known_units:
            facts = _UNIT_FACTS.format(
                units="、".join(f"{column}={unit}" for column, unit in known_units.items())
            )

        try:
            raw = self.client.generate(_PARAPHRASE_PROMPT.format(sql=sql.strip()))
            paraphrase = self._extract(raw, "复述") or raw.strip()

            if preceding_summary.strip():
                compare_prompt = _MULTI_STEP_COMPARE_PROMPT.format(
                    query=query,
                    preceding=preceding_summary.strip(),
                    paraphrase=paraphrase,
                    facts=facts,
                )
            else:
                compare_prompt = _COMPARE_PROMPT.format(
                    query=query, paraphrase=paraphrase, facts=facts
                )
            verdict_raw = self.client.generate(compare_prompt)
        except RunCancelled:
            # 请求被取消：照实往上抛，不能记成"自检没做"再把答案交出去
            raise
        except Exception as exc:
            # 自检是增强项，不能因为它挂掉就让整条查询失败——但也不能假装它做过了。
            return SelfCheckResult(status=SELF_CHECK_NOT_RUN, error=type(exc).__name__)

        conclusion = self._extract(verdict_raw, "结论")
        reason = self._extract(verdict_raw, "理由") or verdict_raw.strip()

        if not conclusion:
            # 读不出结论就是没核对成，不能当成「一致」放过去——以前就是这样：
            # 模型写成「**结论**：不一致」时标签找不到，不一致的 SQL 被记成了核对通过。
            return SelfCheckResult(
                status=SELF_CHECK_NOT_RUN, paraphrase=paraphrase, error="UnreadableVerdict"
            )
        if "不一致" in conclusion:
            return SelfCheckResult(
                status=SELF_CHECK_MISMATCH,
                finding=ValidationFinding(
                    anomaly=AnomalyType.SEMANTIC_MISMATCH,
                    detail=f"SQL 实际行为与问题不一致：{reason}（复述：{paraphrase}）",
                    severity="block",
                ),
                paraphrase=paraphrase,
            )

        return SelfCheckResult(status=SELF_CHECK_PASSED, paraphrase=paraphrase)

    @staticmethod
    def _extract(text: str, label: str) -> str:
        match = re.search(rf"{label}\s*[:：]\s*(.+)", plain_text(text))
        return match.group(1).strip() if match else ""
