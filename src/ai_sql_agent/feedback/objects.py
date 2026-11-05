from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Verdict(str, Enum):
    """用户对一次回答的判定。"""

    CORRECT = "correct"
    """答对了。这条查询会成为认证范例，注入后续同类问题的生成 Prompt。"""

    WRONG = "wrong"
    """答错了。这条查询会进反面教材清单，用来补评测集。"""

    UNSURE = "unsure"
    """看不准。既不当范例也不当反例，但值得有人来看一眼。"""


@dataclass
class FeedbackRecord:
    """
    一条用户反馈。

    问题原文、SQL、命中库表都不从客户端取，而是按 trace_id 从审计记录里回填：
    用户只表态「对/错」，「对的是哪条 SQL」由服务端说了算。
    """

    trace_id: str
    user_id: str
    verdict: Verdict

    query: str = ""
    databases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    sqls: List[Dict[str, str]] = field(default_factory=list)
    """每步实际执行的 {database, sql}，来自审计记录。"""

    comment: str = ""
    """用户补充说明。答错时最有价值的一栏——它常常直接指出缺哪条元数据。"""

    caller: str = ""
    route: str = ""
    had_risk: bool = False
    """回答当时是否带了风险提示。用于分辨「提示有用」和「提示是噪音」。"""

    resolved: bool = False
    """这条反馈是否已经处理（补了元数据 / 进了评测集）。"""

    created_at: str = field(default_factory=utc_now)

    @property
    def is_certifiable(self) -> bool:
        """能否作为认证范例复用：判定为对、且确实执行过 SQL。"""
        return self.verdict == Verdict.CORRECT and bool(self.sqls)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "user_id": self.user_id,
            "caller": self.caller,
            "verdict": self.verdict.value,
            "query": self.query,
            "databases": self.databases,
            "tables": self.tables,
            "sqls": self.sqls,
            "comment": self.comment,
            "route": self.route,
            "had_risk": self.had_risk,
            "resolved": self.resolved,
        }


@dataclass
class CertifiedQuery:
    """一条被用户确认正确的查询，可作为后续生成的范例。"""

    query: str
    database: str
    sql: str
    tables: List[str] = field(default_factory=list)
    user_id: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "database": self.database,
            "sql": self.sql,
            "tables": self.tables,
            "user_id": self.user_id,
            "created_at": self.created_at,
        }


@dataclass
class FeedbackStats:
    """反馈汇总，用于判断「这套系统现在到底准不准」。"""

    total: int = 0
    correct: int = 0
    wrong: int = 0
    unsure: int = 0
    unresolved_wrong: int = 0
    wrong_with_risk: int = 0
    """答错且当时给了风险提示的条数——用户被提醒过。"""

    wrong_without_risk: int = 0
    """答错且当时毫无提示的条数，即线上口径的静默错误。"""

    @property
    def accuracy(self) -> float:
        """有明确对错判定的样本里答对的占比。"""
        decided = self.correct + self.wrong
        return self.correct / decided if decided else 0.0

    @property
    def silent_error_rate(self) -> float:
        """
        线上静默错误率：答错且没有任何提示的占比。

        与评测集口径一致（分母是全部有判定的样本），
        差别是这里的样本来自真实用户，不来自我们自己出的题。
        """
        decided = self.correct + self.wrong
        return self.wrong_without_risk / decided if decided else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "unsure": self.unsure,
            "unresolved_wrong": self.unresolved_wrong,
            "wrong_with_risk": self.wrong_with_risk,
            "wrong_without_risk": self.wrong_without_risk,
            "accuracy": round(self.accuracy, 4),
            "silent_error_rate": round(self.silent_error_rate, 4),
        }
