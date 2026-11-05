"""
数据时点（契约 §4.6.4）。

`as_of` 回答的是「这份数据代表哪一刻的业务状态」，不是「什么时候返回给你的」。
读主库时两者几乎一样，所以今天看不出差别；接只读副本或数仓之后就不一样了——
主从延迟能让「最新状态」变成几小时前的状态。**那一天到来时，链路上必须已经
有这个字段**，否则调用方会继续对客户说「截至目前」。
"""
from __future__ import annotations

from datetime import datetime, timezone

QUERY_TIME = "query_time"
"""读的是主库当前状态：`as_of` 就是 SQL 执行时刻。"""

SNAPSHOT = "snapshot"
"""读的是快照 / 数仓 / 只读副本：`as_of` 是快照时点，可能显著早于现在。"""

UNKNOWN = "unknown"
"""说不清楚。契约 §4.6.4：调用方按不可信处置，不得表述任何时点。"""


def now_rfc3339() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
