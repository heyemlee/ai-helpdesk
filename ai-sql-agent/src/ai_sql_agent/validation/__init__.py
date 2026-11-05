"""执行结果校验：捕捉「SQL 跑通但答案错」这一类软异常。"""

from .cardinality import check_cardinality, parse_expectation
from .objects import (
    AnomalyType,
    CardinalityExpectation,
    ValidationFinding,
    ValidationOutcome,
)
from .self_check import SemanticSelfChecker
from .shape import (
    check_shape_against_plan,
    check_unrequested_aggregation,
    wants_aggregation,
)
from .validator import ResultValidator

__all__ = [
    "AnomalyType",
    "CardinalityExpectation",
    "ValidationFinding",
    "ValidationOutcome",
    "SemanticSelfChecker",
    "check_shape_against_plan",
    "check_unrequested_aggregation",
    "wants_aggregation",
    "ResultValidator",
    "check_cardinality",
    "parse_expectation",
]
