"""
事件的类型和信封。

类型是代码里的一个固定集合，有一条测试把它列全（和 `KNOWN_LOOP_TOOLS` 一个做法）：
读的一方按类型做投影，拼错一个字母不会报错，只会让某个字段悄悄变空。

信封的字段就是 `turn_events` 表的列（storage/pg.py）。`data` 和 `body` 在发出事件时
就序列化成 JSON 文本存着：之后谁再改原来那个字典，都不影响已经记下的事件。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 事件形状的版本。加字段直接加，读的一方忽略不认识的字段；改名或删字段时这一类事件的
# v 加一，投影按 v 分别处理。
EVENT_VERSION = 1

EVENT_TYPES = frozenset({
    # 轮次：chat、chat_stream 发
    "turn_started", "turn_completed", "turn_failed", "turn_aborted",
    # 准备阶段：记忆、意图、预取的门控
    "memory_loaded", "intent_voted", "prefetch_planned",
    # 工具调用：规则预取和工具循环共用这一对
    "tool_started", "tool_call",
    # 每次模型请求，在 GuardedLLMClient 那一层记
    "model_call",
    # 编排器和专家
    "routed", "agent_started", "agent_fallback", "preamble_discarded",
    "loop_ended", "loop_fallback", "guard_replaced", "agent_answered",
    "handoff", "ticket_created",
})

# 终结事件：三者之一。没有终结事件的轮次，说明没写完或进程在这一轮中间挂了。
# 终结事件不一定是最后一行：轮次之后的后台任务（画像更新）的模型调用排在它后面。
TERMINAL_TYPES = frozenset({"turn_completed", "turn_failed", "turn_aborted"})


@dataclass(frozen=True, slots=True)
class Event:
    """一条事件，也就是 `turn_events` 的一行。"""

    turn_id: str
    seq: int
    instance: str
    conv_id: str
    request_id: str
    type: str
    agent: str
    step: int | None
    call_id: str
    ts: float              # 墙钟，epoch 秒（和 tickets、conversation_log 一致）
    t_ms: float            # 距轮次开始的毫秒数，单调时钟，算耗时用
    v: int
    data: str              # 结构：JSON 文本，只放代码自己产生的东西
    body: str | None       # 原文：JSON 文本；没有、只记结构、或已按保留期清空时为 None

    def row(self) -> tuple[Any, ...]:
        """按 `turn_events` 的列序排好的一行，给批量写入用。"""
        return (self.turn_id, self.seq, self.instance, self.conv_id, self.request_id,
                self.type, self.agent, self.step, self.call_id, self.ts, self.t_ms,
                self.v, self.data, self.body)

    def to_dict(self) -> dict[str, Any]:
        """解开 data 和 body 的样子：读接口、投影、测试都用它。"""
        return {
            "turn_id": self.turn_id, "seq": self.seq, "instance": self.instance,
            "conv_id": self.conv_id, "request_id": self.request_id, "type": self.type,
            "agent": self.agent, "step": self.step, "call_id": self.call_id,
            "ts": self.ts, "t_ms": self.t_ms, "v": self.v,
            "data": json.loads(self.data) if self.data else {},
            "body": json.loads(self.body) if self.body is not None else None,
        }


COLUMNS = ("turn_id", "seq", "instance", "conv_id", "request_id", "type", "agent",
           "step", "call_id", "ts", "t_ms", "v", "data", "body")


def event_from_row(row: Any) -> Event:
    """数据库的一行 → Event。asyncpg 默认把 jsonb 读成文本，正好就是 Event 存的样子。"""
    return Event(**{name: row[name] for name in COLUMNS})
