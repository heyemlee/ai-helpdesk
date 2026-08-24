"""
一轮对话的事件日志。

每一轮 /chat 从开始到结束发生的事，按顺序记成一串事件，追加进 Postgres 的
`turn_events` 表，主键是 turn_id + seq。写入在回答路径之外批量进行，写不进去只丢日志、
不影响回答。每条事件分成结构（data）和原文（body）两半：原文到期清空，结构留半年。

- `types`：事件类型和信封
- `recorder`：记录器（contextvar 上的 `events()` / `bind()`）、序列化和截断、同会话在途表
- `writer`：Postgres 的批量写入器
- `store`：存储和查询（Postgres、进程内环形缓冲），按实例隔离
- `retention`：保留期任务
- `log`：开关和装配，main.py 只和它打交道
"""
from app.events.log import EventLog, EventLogConfig, instance_from_env
from app.events.recorder import NOOP, TurnRecorder, bind, events, new_turn_id
from app.events.types import EVENT_TYPES, TERMINAL_TYPES, Event

__all__ = [
    "EVENT_TYPES", "NOOP", "TERMINAL_TYPES", "Event", "EventLog", "EventLogConfig",
    "TurnRecorder", "bind", "events", "instance_from_env", "new_turn_id",
]
