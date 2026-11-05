from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditRecord:
    """
    一次查询的审计记录。

    内部自用系统同样需要事后追溯：谁、什么时候、问了什么、系统生成了
    什么 SQL、碰了哪些表、成功没有、烧了多少 token。出问题时用
    trace_id 就能把日志和这条记录对上。
    """

    trace_id: str
    caller: str
    user_id: str
    session_id: str
    query: str

    run_id: str = ""
    """
    这次请求的 run（`ai_sql_agent.runs`）。trace_id 可以由调用方透传、不保证唯一，
    run_id 一定唯一，拿它能取到这次请求按顺序的事件日志（`GET /runs/{run_id}/events`）。
    """

    route: str = ""
    databases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    sqls: List[Dict[str, str]] = field(default_factory=list)
    """每步实际执行的 {database, sql}；跨库查询各步的库不同，不能只存 SQL。"""
    denied_tables: List[str] = field(default_factory=list)

    success: bool = True
    degraded: bool = False
    """模型不可用时走了降级链路。"""

    has_risk: bool = False
    """
    返回给用户时带了软异常提示（空结果 / 基数异常 / 语义不一致）。

    落进审计是为了让「线上静默错误率」算得出来：用户事后标记这条答错时，
    要能分辨他当时是**被提醒过**还是毫无察觉。后者才是最贵的那一类错误，
    而它恰恰是评测集之外唯一能测到它的地方。
    """

    execution_status: str = ""
    """
    业务终态（契约 §4.6.1）：success / no_data / partial_success / failed /
    not_applicable。空串表示这条记录写入时还没有这一列（老记录）或请求没跑到出结果。

    `success` 那一列回答不了「查到了，答案是没有」和「查到了数据」的区别——
    两者都是 success=1。历史页要把这两种分开显示，而结果集不落审计，
    事后算不回来，所以在写入时就记下。
    """

    status_code: int = 200
    failed_steps: int = 0
    repaired_steps: int = 0

    duration_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    error: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "caller": self.caller,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "query": self.query,
            "route": self.route,
            "databases": self.databases,
            "tables": self.tables,
            "sqls": self.sqls,
            "denied_tables": self.denied_tables,
            "success": self.success,
            "degraded": self.degraded,
            "has_risk": self.has_risk,
            "execution_status": self.execution_status,
            "status_code": self.status_code,
            "failed_steps": self.failed_steps,
            "repaired_steps": self.repaired_steps,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "error": self.error,
        }


@dataclass
class DailyUsage:
    """某个用户当天的用量，用于配额判断。"""

    user_id: str
    queries: int = 0
    tokens: int = 0
