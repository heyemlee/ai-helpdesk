from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class MCPExecutionRequest:
    """
    MCP 执行请求。

    database 用于路由到对应数据库连接。
    sql 是当前步骤生成的 SQL。
    """

    database: str
    sql: str

    params: Sequence[Any] = ()
    """
    绑定参数，按位置对应 SQL 里的占位符（SQLite 是 `?`，PostgreSQL 是 `%s`）。

    模型生成的 SQL 不带参数，这个字段一直是空的；数据浏览的预览查询用它：
    表名列名来自服务端核对过的清单，用户输入的搜索词与筛选值只走这里，
    永远不拼进 SQL 文本。
    """

    max_rows: Optional[int] = None
    timeout_seconds: Optional[float] = None
    """
    这次执行的行数与时长上限。**只能收紧，不能放宽**——执行器自己的上限是硬顶，
    这里给的值比它大时按执行器的算（见 `row_cap` / `time_cap`）。
    """

    def row_cap(self, executor_cap: int) -> int:
        if self.max_rows is None:
            return executor_cap
        return max(1, min(int(executor_cap), int(self.max_rows)))

    def time_cap(self, executor_cap: float) -> float:
        if self.timeout_seconds is None or self.timeout_seconds <= 0:
            return executor_cap
        return min(float(executor_cap), float(self.timeout_seconds))


@dataclass
class MCPExecutionResult:
    """
    MCP 执行结果。
    """

    database: str
    sql: str
    success: bool
    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    error: Optional[str] = None
    truncated: bool = False
    """结果行数达到 max_rows 上限并被截断时为 True。"""

    row_limit: Optional[int] = None
    """
    这次执行生效的行数上限（契约 §4.6.3）。

    `truncated=True` 时**必须**有值：调用方要能分清「服务配置的上限封顶」和「被临时
    调小的上限截了」——两者对「共有 N 条」这句话的杀伤力不一样。
    """

    truncated_by: str = ""
    """
    `truncated=True` 时是哪个上限先到的：`rows`（行数）或 `bytes`（字节预算）。
    只在进程内使用（日志、测试），**不进 `to_dict()`**——契约里 `truncated` /
    `row_limit` 两个字段已经够调用方判断，不为这件事给线上载荷加字段。
    """

    as_of: str = ""
    """
    数据时点，RFC 3339（契约 §4.6.4）。SQL 真正执行的时刻，由执行器打点。

    读主库时它就是「现在」；接了只读副本或数仓之后，主从延迟会让「最新状态」
    变成几分钟前的状态，而链路上原来没有任何字段能让调用方发现这件事。
    """

    as_of_source: str = ""
    """`query_time` / `snapshot` / `unknown`（契约 §4.6.4）。"""

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "database": self.database,
            "sql": self.sql,
            "success": self.success,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "error": self.error,
            "truncated": self.truncated,
            "row_limit": self.row_limit,
            "as_of": self.as_of,
            "as_of_source": self.as_of_source,
        }
