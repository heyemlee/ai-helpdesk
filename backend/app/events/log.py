"""
事件日志的开关和装配。main.py 的 lifespan 只调一次 `EventLog.open(EventLogConfig.from_env(), db)`，
每一轮 `open_turn` 拿一个记录器。

| 变量 | 默认 | 说明 |
|---|---|---|
| AI_HELPDESK_EVENT_LOG | off | full：记结构和原文；meta：只记结构；off：一条都不记（turn_id 照样有） |
| AI_HELPDESK_INSTANCE | 不设 | staff 或 customer。日志打开、它却没设时，日志保持关闭 |
| AI_HELPDESK_EVENT_LOG_BODY_DAYS | 30 | 多少天后清空原文；0 不清 |
| AI_HELPDESK_EVENT_LOG_DAYS | 180 | 多少天后删除整行；0 不删 |
| AI_HELPDESK_EVENT_LOG_READ | on | 读接口开不开；客户实例设 off |
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.metrics import TURNS_CONCURRENT
from app.events.recorder import (
    EventLogStats,
    TurnRecorder,
    new_turn_id,
    register_in_flight,
    unregister_in_flight,
)
from app.events.retention import Retention
from app.events.store import MemoryEventStore, PgEventStore
from app.events.types import ABANDONED_SEQ
from app.events.writer import EventWriter

logger = logging.getLogger(__name__)

INSTANCES = ("staff", "customer")

_FULL_ALIASES = {"on", "true", "1", "yes"}
_OFF_VALUES = {"off", "false", "0", "no", ""}


def instance_from_env(env: Mapping[str, str] | None = None) -> str:
    """AI_HELPDESK_INSTANCE：staff 或 customer。没设、写错都是空串。审计日志的实例列也用它。"""
    env = os.environ if env is None else env
    raw = env.get("AI_HELPDESK_INSTANCE", "").strip().lower()
    return raw if raw in INSTANCES else ""


def _days(env: Mapping[str, str], name: str, default: int, warnings: list[str]) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        warnings.append(f"{name}={raw!r} 不是非负整数，按默认 {default} 天")
        return default
    return value


@dataclass(frozen=True)
class EventLogConfig:
    mode: str = "off"
    instance: str = ""
    body_days: int = 30
    row_days: int = 180
    read: bool = True
    # 配了要开、却开不了的原因（实例没设、取值不认识）。启动日志和 /health 照着说
    disabled_reason: str = ""
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EventLogConfig:
        env = os.environ if env is None else env
        warnings: list[str] = []
        reason = ""

        raw_mode = env.get("AI_HELPDESK_EVENT_LOG", "off").strip().lower()
        if raw_mode in ("meta", "full"):
            mode = raw_mode
        elif raw_mode in _FULL_ALIASES:
            mode = "full"
        elif raw_mode in _OFF_VALUES:
            mode = "off"
        else:
            mode = "off"
            reason = f"AI_HELPDESK_EVENT_LOG={raw_mode!r} 不认识（off / meta / full）"

        raw_instance = env.get("AI_HELPDESK_INSTANCE", "").strip()
        instance = instance_from_env(env)
        if raw_instance and not instance:
            warnings.append(f"AI_HELPDESK_INSTANCE={raw_instance!r} 不认识（staff / customer），按没设处理")
        if mode != "off" and not instance:
            # 标错实例的后果是员工那边看得到客户的轮次，所以宁可不记
            reason = "AI_HELPDESK_INSTANCE 没设（staff / customer），日志不开"
            mode = "off"

        raw_read = env.get("AI_HELPDESK_EVENT_LOG_READ", "on").strip().lower()
        read = raw_read in _FULL_ALIASES or raw_read == ""
        if not read and raw_read not in _OFF_VALUES:
            # 这是个关着才安全的开关（客户实例要关），认不出来的写法按关处理
            warnings.append(f"AI_HELPDESK_EVENT_LOG_READ={raw_read!r} 不认识，按 off 处理")
        if instance == "customer" and read:
            warnings.append("客户实例的读接口开着：应当设 AI_HELPDESK_EVENT_LOG_READ=off")

        return cls(
            mode=mode,
            instance=instance,
            body_days=_days(env, "AI_HELPDESK_EVENT_LOG_BODY_DAYS", 30, warnings),
            row_days=_days(env, "AI_HELPDESK_EVENT_LOG_DAYS", 180, warnings),
            read=read,
            disabled_reason=reason,
            warnings=tuple(warnings),
        )


class EventLog:
    """
    一个进程的事件日志：配置、存储、写入器、保留任务和计数。

    日志关着时也是一个 EventLog（`disabled()`），`open_turn` 照样给 turn_id，只是记录器
    什么都不记。所以调用方从来不用判空。
    """

    def __init__(
        self,
        config: EventLogConfig,
        *,
        store=None,
        sink=None,
        writer: EventWriter | None = None,
        retention: Retention | None = None,
        stats: EventLogStats | None = None,
    ):
        self.config = config
        self.store = store
        self._sink = sink
        self._writer = writer
        self.retention = retention
        self.stats = stats if stats is not None else EventLogStats()
        self._retention_task: asyncio.Task | None = None

    @classmethod
    def disabled(cls) -> EventLog:
        return cls(EventLogConfig())

    @classmethod
    async def open(cls, config: EventLogConfig, db=None) -> EventLog:
        """
        按配置装配。要在事件循环里调：写入器和保留任务是后台任务。

        有 Postgres 就写 `turn_events`；没有就存进程内最近 200 轮。日志关着、但实例设了
        又有库的时候，保留任务照样跑：之前开着时记下的原文，到期也得清。
        """
        stats = EventLogStats()
        for warning in config.warnings:
            logger.warning("事件日志: %s", warning)
        if not config.instance:
            if config.disabled_reason:
                logger.warning("事件日志没开: %s", config.disabled_reason)
            else:
                logger.info("事件日志关着（AI_HELPDESK_EVENT_LOG=off）")
            return cls(config, stats=stats)

        writer = retention = None
        if db is not None:
            store = PgEventStore(db, config.instance)
            if config.mode != "off":
                writer = EventWriter(store, stats)
            sink = writer
            retention = Retention(store, body_days=config.body_days, row_days=config.row_days,
                                  stats=stats)
        else:
            store = MemoryEventStore(config.instance, stats=stats)
            sink = store if config.mode != "off" else None
        log = cls(config, store=store, sink=sink, writer=writer, retention=retention, stats=stats)
        log._start()
        if config.mode == "off":
            logger.info("事件日志关着（AI_HELPDESK_EVENT_LOG=off），实例 %s", config.instance)
        else:
            logger.info(
                "事件日志已打开：%s，实例 %s，%s；原文 %s，整行 %s；读接口%s",
                "结构和原文" if config.mode == "full" else "只记结构",
                config.instance,
                "写进 Postgres" if db is not None else "存进程内最近 200 轮（没有 Postgres，重启就没了）",
                f"{config.body_days} 天后清空" if config.body_days else "不清",
                f"{config.row_days} 天后删除" if config.row_days else "不删",
                "开着" if config.read else "关着",
            )
        return log

    def _start(self) -> None:
        if self._writer is not None:
            self._writer.start()
        if self.retention is not None and self.retention.active:
            self._retention_task = asyncio.create_task(
                self.retention.run_forever(), name="turn-event-retention")

    @property
    def recording(self) -> bool:
        return self.config.mode != "off" and self._sink is not None

    @property
    def readable(self) -> bool:
        """读接口开不开：配置上开着、而且有东西可读。"""
        return self.config.read and self.store is not None

    def open_turn(self, *, conv_id: str, request_id: str = "") -> TurnRecorder:
        """一轮开始：分配 turn_id，登记同会话在途。结束时调 `turn.close()`。"""
        instance = self.config.instance
        turn = TurnRecorder(
            new_turn_id(), instance=instance, conv_id=conv_id, request_id=request_id,
            mode=self.config.mode, sink=self._sink, stats=self.stats,
        )
        turn.concurrent = register_in_flight(instance, conv_id, turn.turn_id)
        if turn.concurrent:
            TURNS_CONCURRENT.inc()
        turn.on_close(lambda: unregister_in_flight(instance, conv_id, turn.turn_id))
        return turn

    def close_abandoned(self, turn_id: str, *, conv_id: str, request_id: str = "",
                        data: dict[str, Any] | None = None) -> None:
        """
        替一轮补上 turn_abandoned：跑它的进程没了，这一轮再也等不到自己的终结事件。

        排队模式下由接手的那一遍（或者送进死信的那一次）调，data 说明后来怎么样了。写在固定的
        保留号上（types.ABANDONED_SEQ），补两次只留一条。只有结构、没有原文。同步、不抛。
        """
        if not turn_id:
            return
        closer = TurnRecorder(
            turn_id, instance=self.config.instance, conv_id=conv_id, request_id=request_id,
            mode=self.config.mode, sink=self._sink, stats=self.stats, first_seq=ABANDONED_SEQ,
        )
        closer.emit("turn_abandoned", data)

    def health(self) -> dict:
        config = self.config
        retention = (self.retention.health() if self.retention is not None else
                     {"body_days": config.body_days, "row_days": config.row_days, "last_run": None})
        out = {
            "mode": config.mode,
            "instance": config.instance,
            "read": self.readable,
            "store": getattr(self.store, "backend", None),
            "queue_depth": self._writer.depth if self._writer is not None else 0,
            "written": self.stats.written,
            "dropped": sum(self.stats.dropped.values()),
            "dropped_by_reason": dict(self.stats.dropped),
            "last_error": self.stats.last_error,
            "retention": retention,
        }
        if config.disabled_reason:
            out["disabled_reason"] = config.disabled_reason
        return out

    async def close(self, timeout_s: float = 10.0) -> None:
        """停机：停保留任务，限时把没写的写完。"""
        if self._retention_task is not None:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except asyncio.CancelledError:
                pass
            self._retention_task = None
        if self._writer is not None:
            await self._writer.close(timeout_s)
