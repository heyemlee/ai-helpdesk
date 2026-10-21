"""run 的形状和状态。"""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

QUEUED = "queued"        # 提交了，等 worker 领
RUNNING = "running"      # 有一个 worker 拿着租约在跑
SUCCEEDED = "succeeded"  # 这一轮有了结局：答了、转人工、追问、或者报了"模型服务不可用"
FAILED = "failed"        # 这一轮自己出错（编排器抛异常）。不重试，和原来一样报给客户
DEAD = "dead"            # worker 反复失联，超过上限：进死信，转人工

STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED, DEAD)
TERMINAL = frozenset({SUCCEEDED, FAILED, DEAD})

# 帧流里标志"这一轮结束了"的两种事件：和 /chat/stream 的终结事件一致
TERMINAL_FRAMES = frozenset({"result", "error"})


def new_run_id() -> str:
    """
    12 位十六进制毫秒时间戳加 20 位随机数，按生成时间排序。

    run_id 同时是读这一轮的凭据（没开令牌鉴权时，拿着它就能续流），所以随机部分给到
    80 位，猜不到。
    """
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(10)}"


@dataclass
class RunRecord:
    run_id: str
    instance: str
    conv_id: str
    user_id: str
    message: str
    request_id: str = ""
    # 客户端给的幂等键（Idempotency-Key）；没给就是 run_id 自己。同一个实例里唯一
    idempotency_key: str = ""
    transport: str = "stream"
    status: str = QUEUED
    # 第几次执行：每被领走一次加一。同时是栅栏（见 app/runs/__init__.py）
    attempt: int = 0
    lease_owner: str = ""
    lease_until: float = 0.0
    # 最近一次执行的那一轮（事件日志里的 turn_id）
    turn_id: str = ""
    degrade_level: int = 0
    # 提交点：这一轮的回答已经定下来、还没开始落副作用（工单、记忆、审计）时写下的那一份。
    # 之后失联重跑的那一遍不再重新作答，照着它把副作用补完（见 app/runs/__init__.py）
    checkpoint: dict[str, Any] | None = None
    # 终态时的响应体：/chat 的形状（meta 和 result 两个事件合起来）；出错时为 None
    result: dict[str, Any] | None = None
    error: str = ""
    # 每次执行一条：谁跑的、哪一轮、怎么结束的
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    dispatched_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = self.run_id

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        """
        GET /runs/{id} 返回的样子。不带客户原话（审计里有），不带租约细节以外的内部状态。
        """
        return {
            "run_id": self.run_id,
            "conv_id": self.conv_id,
            "status": self.status,
            "attempt": self.attempt,
            "turn_id": self.turn_id,
            "degrade_level": self.degrade_level,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "attempts": [
                {k: v for k, v in item.items() if k != "owner"} for item in self.history
            ],
        }
