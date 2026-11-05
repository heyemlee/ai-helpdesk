from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExecutionError:
    """从执行日志解析出的结构化错误。"""

    error_type: str
    """错误类型：no_such_table / no_such_column / ambiguous_column /
    syntax_error / timeout / unknown。"""

    detail: str = ""
    """错误相关对象，如出错的表名、字段名或语法位置附近的 Token。"""

    raw_message: str = ""
    """原始错误日志。"""


@dataclass
class SqlClause:
    """SQL 子句及其在原文中的位置。"""

    name: str
    """子句名称，如 SELECT / FROM / JOIN / WHERE / GROUP BY。"""

    text: str
    """子句文本（含子句关键字）。"""

    start: int
    """子句在原 SQL 中的起始偏移。"""

    end: int
    """子句在原 SQL 中的结束偏移。"""


@dataclass
class RepairAttempt:
    """单次修复尝试记录。"""

    attempt_no: int
    error_type: str
    failing_clause: str
    original_sql: str
    repaired_sql: str
    strategy: str
    """修复策略：model / heuristic。"""

    success: bool = False
    error_after_repair: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_no": self.attempt_no,
            "error_type": self.error_type,
            "failing_clause": self.failing_clause,
            "original_sql": self.original_sql,
            "repaired_sql": self.repaired_sql,
            "strategy": self.strategy,
            "success": self.success,
            "error_after_repair": self.error_after_repair,
        }


@dataclass
class RepairOutcome:
    """修复流程整体结果。"""

    repaired: bool
    final_sql: str
    final_result: Optional[object] = None
    attempts: List[RepairAttempt] = field(default_factory=list)
