from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from .coreference import CoreferenceResolver
from .objects import FiscalConfig, NormalizationResult
from .time_parser import RelativeTimeParser


class QueryNormalizer:
    """
    问题归一化：链路的第一步。

    做两件事：
    1. **代词消解**——把「它们」换成上一轮的查询对象；
    2. **时间表达式解析**——把「上季度」换成绝对日期区间。

    都放在检索之前，因为这两者都会影响关键词抽取与 Schema 召回：
    「它们」不消解就丢了筛选条件，「上季度」不解析就让模型自己猜区间。
    """

    def __init__(
        self,
        fiscal: Optional[FiscalConfig] = None,
        *,
        enable_coreference: bool = True,
        enable_time_parsing: bool = True,
    ):
        self.time_parser = RelativeTimeParser(fiscal)
        self.coreference = CoreferenceResolver()
        self.enable_coreference = enable_coreference
        self.enable_time_parsing = enable_time_parsing

    def normalize(
        self,
        query: str,
        *,
        history_queries: Sequence[str] = (),
        today: Optional[date] = None,
    ) -> NormalizationResult:
        today = today or date.today()
        normalized = query
        resolved_references: list[str] = []

        if self.enable_coreference:
            normalized, resolved_references = self.coreference.resolve(
                normalized, history_queries
            )

        date_ranges = []
        if self.enable_time_parsing:
            date_ranges = self.time_parser.parse(normalized, today)
            for date_range in date_ranges:
                normalized = normalized.replace(
                    date_range.source_text,
                    f"{date_range.source_text}（{date_range.to_text()}）",
                    1,
                )

        return NormalizationResult(
            original_query=query,
            normalized_query=normalized,
            date_ranges=date_ranges,
            resolved_references=resolved_references,
        )
