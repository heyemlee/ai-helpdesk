from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List


@dataclass
class FiscalConfig:
    """
    财年配置。

    公司财年不一定与自然年一致，「上季度」「去年」的绝对区间要按财年算。
    默认 1 月起始即自然年。
    """

    fiscal_year_start_month: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month 必须在 1-12 之间")


@dataclass
class DateRange:
    """解析出的绝对日期区间，闭区间。"""

    start: date
    end: date
    source_text: str
    """原始相对时间表达式，如「上季度」。"""

    def to_text(self) -> str:
        return f"{self.start.isoformat()} 至 {self.end.isoformat()}"


@dataclass
class NormalizationResult:
    """归一化结果。"""

    original_query: str
    normalized_query: str
    date_ranges: List[DateRange] = field(default_factory=list)
    resolved_references: List[str] = field(default_factory=list)
    """被消解掉的指代，形如「它们 → 超预算的项目」。"""

    @property
    def changed(self) -> bool:
        return self.normalized_query != self.original_query

    def to_dict(self) -> dict:
        return {
            "original_query": self.original_query,
            "normalized_query": self.normalized_query,
            "changed": self.changed,
            "date_ranges": [
                {"source": r.source_text, "start": r.start.isoformat(), "end": r.end.isoformat()}
                for r in self.date_ranges
            ],
            "resolved_references": self.resolved_references,
        }
