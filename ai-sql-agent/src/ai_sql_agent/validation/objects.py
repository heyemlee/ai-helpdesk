from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class AnomalyType(str, Enum):
    """软异常类型。

    与硬错误（语法错误、未知字段）区分：硬错误 SQL 跑不通，执行日志里有明确
    错误码；软异常是 **SQL 跑通了、有结果、但结果可能是错的**——这类错误最危险，
    因为它不会自己暴露。
    """

    EMPTY_RESULT = "empty_result"
    """空结果：可能确实没有，也可能是过滤条件写错。"""

    CARDINALITY_DEVIATION = "cardinality_deviation"
    """基数异常：实际行数与计划预期量级偏离过大，典型成因是 join 扇出重复计数。"""

    SEMANTIC_MISMATCH = "semantic_mismatch"
    """语义不一致：SQL 实际计算的内容与用户问题对不上。"""

    AGGREGATION_UNREQUESTED = "aggregation_unrequested"
    """用户没要求汇总，SQL 却把明细聚合成了一个数（或按某维度折叠）。

    这是实测中静默错误的头号形态：问「台面类供应商的采购金额」，
    模型答一个 SUM——用户以为看到的是每一单的金额，实际拿到的是总额。
    数字本身没错，但回答的不是他问的问题，而且没有任何迹象提示这一点。
    """

    SHAPE_CONTRADICTS_PLAN = "shape_contradicts_plan"
    """SQL 的结构性行数与计划里声明的预期量级矛盾（如计划说数条、SQL 结构上只能出一行）。"""

    SELF_CHECK_NOT_RUN = "self_check_not_run"
    """语义自检本该做、但没做成（模型调用失败）。

    结果本身未必有问题，问题在于少了一道核对：以前这种情况什么都不留，
    从响应上看和「自检做了、没发现问题」一模一样。没核对过不能当成核对过。
    """


SELF_CHECK_PASSED = "passed"
"""语义自检做了，SQL 的实际行为与问题一致。"""

SELF_CHECK_MISMATCH = "mismatch"
"""语义自检做了，发现不一致（对应一条 `semantic_mismatch`）。"""

SELF_CHECK_NOT_RUN = "not_run"
"""语义自检本该做，模型调用失败，没有结论（对应一条 `self_check_not_run`）。"""

SELF_CHECK_NOT_APPLICABLE = "not_applicable"
"""这一步本来就不做语义自检：没开、没配模型、空结果，或者是没有步骤目标的中间步骤。"""


@dataclass
class CardinalityExpectation:
    """把「数条」这类自然语言量级映射为可比较的行数区间。"""

    label: str
    low: int
    high: int

    def contains(self, row_count: int) -> bool:
        return self.low <= row_count <= self.high


@dataclass
class ValidationFinding:
    """一条校验发现。"""

    anomaly: AnomalyType
    detail: str
    severity: str = "warn"
    """warn：提示但不阻断；block：需要回溯重来。"""


@dataclass
class ValidationOutcome:
    """校验结论。"""

    passed: bool
    """没有需要回溯的发现，**并且**该做的语义自检确实做了。自检没做成时为 False。"""

    findings: List[ValidationFinding] = field(default_factory=list)
    self_check_paraphrase: Optional[str] = None
    """语义自检时模型对 SQL 的自然语言复述，便于排查与展示。"""

    self_check: str = SELF_CHECK_NOT_APPLICABLE
    """语义自检的实际状态：passed / mismatch / not_run / not_applicable。"""

    self_check_error: str = ""
    """自检没做成时，是哪一类错误（异常类名）。"""

    @property
    def should_retry(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "should_retry": self.should_retry,
            "findings": [
                {"anomaly": f.anomaly.value, "detail": f.detail, "severity": f.severity}
                for f in self.findings
            ],
            "self_check_paraphrase": self.self_check_paraphrase,
            "self_check": self.self_check,
            "self_check_error": self.self_check_error,
        }
