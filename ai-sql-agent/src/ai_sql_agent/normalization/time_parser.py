from __future__ import annotations

import calendar
import re
from datetime import date
from typing import List, Optional, Tuple

from .objects import DateRange, FiscalConfig


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


class RelativeTimeParser:
    """
    相对时间表达式解析。

    把「上季度」「近三个月」「去年」这类口语表达翻译成绝对日期区间。

    为什么必须做：模型直接看到「上季度」会自己猜一个区间，猜得对不对不可控，
    而且同一个问题在不同时间问会得到不同结果却无从察觉。归一化成绝对日期后，
    区间是确定的、可复现的，出错也能一眼看出来。
    """

    def __init__(self, fiscal: Optional[FiscalConfig] = None):
        self.fiscal = fiscal or FiscalConfig()

    def parse(self, text: str, today: date) -> List[DateRange]:
        ranges: List[DateRange] = []
        for handler in (
            self._parse_recent_n,
            self._parse_quarter,
            self._parse_year,
            self._parse_month,
        ):
            ranges.extend(handler(text, today))
        # 同一段文本只保留第一个匹配，避免「上季度」同时被季度和月份规则命中
        seen: set[str] = set()
        unique: List[DateRange] = []
        for r in ranges:
            if r.source_text in seen:
                continue
            seen.add(r.source_text)
            unique.append(r)
        return unique

    # ---- 近 N 天/周/月 ----
    def _parse_recent_n(self, text: str, today: date) -> List[DateRange]:
        out: List[DateRange] = []
        pattern = r"(近|最近|过去)\s*(\d+|[一二三四五六七八九十两]+)\s*(天|日|周|个月|月|年)"
        for m in re.finditer(pattern, text):
            n = self._to_int(m.group(2))
            if n is None:
                continue
            unit = m.group(3)
            if unit in ("天", "日"):
                start = date.fromordinal(today.toordinal() - n + 1)
            elif unit == "周":
                start = date.fromordinal(today.toordinal() - n * 7 + 1)
            elif unit in ("个月", "月"):
                y, mo = _shift_month(today.year, today.month, -(n - 1))
                start = date(y, mo, 1)
            else:
                start = date(today.year - n + 1, 1, 1)
            out.append(DateRange(start=start, end=today, source_text=m.group(0)))
        return out

    # ---- 季度 ----
    def _parse_quarter(self, text: str, today: date) -> List[DateRange]:
        out: List[DateRange] = []
        offset_map = {"本季度": 0, "这个季度": 0, "当季": 0, "上季度": -1, "上个季度": -1, "去年同期季度": -4}
        for word, offset in offset_map.items():
            if word not in text:
                continue
            start_month = self._fiscal_quarter_start(today)
            y, mo = _shift_month(today.year, start_month, offset * 3)
            end_y, end_mo = _shift_month(y, mo, 2)
            out.append(
                DateRange(start=date(y, mo, 1), end=_month_end(end_y, end_mo), source_text=word)
            )
        return out

    def _fiscal_quarter_start(self, today: date) -> int:
        """当前所处财年季度的起始月份。"""
        offset = (today.month - self.fiscal.fiscal_year_start_month) % 12
        quarter_index = offset // 3
        _, month = _shift_month(
            today.year, self.fiscal.fiscal_year_start_month, quarter_index * 3
        )
        return month

    # ---- 年 ----
    def _parse_year(self, text: str, today: date) -> List[DateRange]:
        out: List[DateRange] = []
        fy_start = self.fiscal.fiscal_year_start_month
        for word, offset in (("今年", 0), ("本年", 0), ("去年", -1), ("上一年", -1), ("前年", -2)):
            if word not in text:
                continue
            base_year = today.year if today.month >= fy_start else today.year - 1
            y = base_year + offset
            start = date(y, fy_start, 1)
            end_y, end_mo = _shift_month(y, fy_start, 11)
            out.append(DateRange(start=start, end=_month_end(end_y, end_mo), source_text=word))
        return out

    # ---- 月 ----
    def _parse_month(self, text: str, today: date) -> List[DateRange]:
        out: List[DateRange] = []
        for word, offset in (("本月", 0), ("这个月", 0), ("当月", 0), ("上月", -1), ("上个月", -1)):
            if word not in text:
                continue
            y, mo = _shift_month(today.year, today.month, offset)
            out.append(DateRange(start=date(y, mo, 1), end=_month_end(y, mo), source_text=word))
        return out

    @staticmethod
    def _to_int(token: str) -> Optional[int]:
        if token.isdigit():
            return int(token)
        cn = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if token in cn:
            return cn[token]
        if token.startswith("十") and len(token) == 2:
            return 10 + cn.get(token[1], 0)
        if len(token) == 3 and token[1] == "十":
            return cn.get(token[0], 0) * 10 + cn.get(token[2], 0)
        return None
