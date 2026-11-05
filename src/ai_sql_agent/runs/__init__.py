"""
每次请求的运行记录（run）与事件日志。

一次请求 = 一个 run：一条记录（状态、用量、结论）、一份按序号递增的事件日志、一个取消令牌。
事件的生产者分散在各层（`trace.stage()`、模型传输、SQL 路由），它们只认
`ai_sql_agent.core.run_context`；这个包负责编号、落库、查询与生命周期。

A2A 的进度帧不从这里来：契约面仍然是 `trace.stage()` 的 sink（a2a-contract §4.5 / §8.2），
事件日志是新增的旁路，不改线上格式。
"""
from .events import RunEvent
from .manager import RunManager, RunRecorder, runs_of
from .store import NullRunStore, PostgresRunStore, SQLiteRunStore, create_run_store

__all__ = [
    "NullRunStore",
    "PostgresRunStore",
    "RunEvent",
    "RunManager",
    "RunRecorder",
    "SQLiteRunStore",
    "create_run_store",
    "runs_of",
]
