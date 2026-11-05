from __future__ import annotations

import re
from typing import Optional

from .objects import (
    AnomalyType,
    CardinalityExpectation,
    ValidationFinding,
)


# 计划里的自然语言量级 → 行数区间。
# 区间刻意给得宽：目的是抓「差一个数量级」的离谱偏差（join 扇出），
# 不是做精确预测。宁可漏报也不要频繁误报，否则回溯成本反而更高。
_MIN_OVER_RATIO = 2.0
"""超出上界多少倍才报。低于这个倍数的偏差归于量级估计本身的误差。"""

_SCALE_TABLE = (
    (("单条", "一条", "1条"), 1, 1),
    (("数条", "几条", "少量"), 1, 20),
    (("数十条", "十几条", "几十条"), 5, 200),
    (("数百条", "上百条"), 50, 2000),
    (("数千条", "上千条"), 500, 20000),
    (("大量", "全量"), 100, 10 ** 9),
)


def parse_expectation(label: str) -> Optional[CardinalityExpectation]:
    """把计划里的「预期输出量级」文本解析成行数区间。"""
    if not label:
        return None

    text = label.strip()

    # 显式数字优先，例如「约 20 条」「10-50 条」
    range_match = re.search(r"(\d+)\s*[-~到至]\s*(\d+)", text)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        return CardinalityExpectation(text, min(low, high), max(low, high))

    single_match = re.search(r"(\d+)\s*条", text)
    if single_match:
        n = int(single_match.group(1))
        # 单个数字按数量级上下放宽一档，避免把「约 20 条」当成精确值
        return CardinalityExpectation(text, max(1, n // 5), n * 5)

    for keywords, low, high in _SCALE_TABLE:
        if any(k in text for k in keywords):
            return CardinalityExpectation(text, low, high)

    return None


def check_cardinality(
    expected_label: str,
    row_count: int,
    *,
    sql: str = "",
) -> Optional[ValidationFinding]:
    """
    基数校验：实际行数是否落在计划预期量级内。

    只在**明显超出上界**时报异常——行数远多于预期通常意味着 join 扇出
    造成的重复计数（例如一对多关联后又做 SUM，把金额重复累加）。
    行数少于预期更可能是数据本身就少，交给空结果检查处理。
    """
    expectation = parse_expectation(expected_label)
    if expectation is None or row_count == 0:
        return None

    if row_count <= expectation.high:
        return None

    has_join = bool(re.search(r"\bJOIN\b", sql, re.I))
    over_ratio = row_count / max(expectation.high, 1)

    # 只报「差一个量级」的偏差。量级标签本来就是模型随手给的估计，
    # 21 行 vs 上界 20 行这种擦边不说明任何问题，报了只会让提示变噪音——
    # 而提示一旦被当成噪音忽略，真正的静默错误也就跟着漏过去了。
    if over_ratio < _MIN_OVER_RATIO:
        return None

    detail = (
        f"实际返回 {row_count} 行，超出计划预期量级「{expectation.label}」"
        f"（上界约 {expectation.high} 行）{over_ratio:.1f} 倍。"
    )
    if has_join:
        detail += "SQL 含 JOIN，疑似一对多关联导致扇出重复计数。"

    # 超出一个数量级且含 JOIN 才阻断，否则仅提示
    severity = "block" if (has_join and over_ratio >= 3) else "warn"

    return ValidationFinding(
        anomaly=AnomalyType.CARDINALITY_DEVIATION,
        detail=detail,
        severity=severity,
    )
