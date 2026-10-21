"""
排队模式的开关和参数。

| 变量 | 默认 | 说明 |
|---|---|---|
| AI_HELPDESK_RUN_MODE | inline | inline：一轮跑在请求里（原来的样子）；queued：提交成 run，交给 worker |
| AI_HELPDESK_ROLE | all | api：只收请求、续流；worker：只领 run 执行；all：两样都做（单容器） |
| AI_HELPDESK_WORKER_CONCURRENCY | 4 | 一个 worker 进程同时跑几个 run |
| AI_HELPDESK_RUN_LEASE_S | 30 | 租约多长。续租间隔是它的三分之一 |
| AI_HELPDESK_RUN_MAX_ATTEMPTS | 3 | 失联重跑到第几次还不成就进死信 |
| AI_HELPDESK_RUN_QUEUE_MAX | 200 | 积压到多少条开始拒收（429 + Retry-After） |
| AI_HELPDESK_RUN_REDISPATCH_S | 30 | 库里 queued 多久没人领，清扫任务重新派发 |
| AI_HELPDESK_RUN_RETENTION_DAYS | 14 | 结束多少天后删掉 run（原话和结果在审计、事件日志里另有留存）；0 不删 |
| AI_HELPDESK_RUN_WAIT_S | 120 | 排队模式下 /chat（非流式）最多等结果多久 |

**排队模式要 Postgres 和 Redis，还要标明实例。** run 的状态在 Postgres，派发和帧流在 Redis，
两个实例共用这两样东西、按实例分开：没标实例的进程不知道该去哪条队列领活，员工那边的
worker 可能跑了客户的问题（员工实例能委派查订单）。缺哪样都不起（core/preflight 之外的
另一道启动检查，见 `problems()`）。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MODES = ("inline", "queued")
ROLES = ("api", "worker", "all")


def _number(env: Mapping[str, str], name: str, default: float, warnings: list[str],
            minimum: float = 0.0) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        warnings.append(f"{name}={raw!r} 不是数字，按默认 {default:g}")
        return default
    if value < minimum:
        warnings.append(f"{name}={raw!r} 小于 {minimum:g}，按默认 {default:g}")
        return default
    return value


@dataclass(frozen=True)
class RunConfig:
    mode: str = "inline"
    role: str = "all"
    concurrency: int = 4
    lease_s: float = 30.0
    max_attempts: int = 3
    queue_max: int = 200
    redispatch_after_s: float = 30.0
    retention_days: int = 14
    wait_s: float = 120.0
    warnings: tuple[str, ...] = ()

    @property
    def queued(self) -> bool:
        return self.mode == "queued"

    @property
    def serves_api(self) -> bool:
        """这个进程收不收提交、给不给续流。"""
        return self.queued and self.role in ("api", "all")

    @property
    def runs_worker(self) -> bool:
        """这个进程领不领 run 来执行。"""
        return self.queued and self.role in ("worker", "all")

    @property
    def heartbeat_s(self) -> float:
        return max(self.lease_s / 3, 0.05)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunConfig:
        env = os.environ if env is None else env
        warnings: list[str] = []
        mode = env.get("AI_HELPDESK_RUN_MODE", "inline").strip().lower() or "inline"
        if mode not in MODES:
            warnings.append(f"AI_HELPDESK_RUN_MODE={mode!r} 不认识（inline / queued），按 inline")
            mode = "inline"
        role = env.get("AI_HELPDESK_ROLE", "all").strip().lower() or "all"
        if role not in ROLES:
            warnings.append(f"AI_HELPDESK_ROLE={role!r} 不认识（api / worker / all），按 all")
            role = "all"
        return cls(
            mode=mode,
            role=role,
            concurrency=int(_number(env, "AI_HELPDESK_WORKER_CONCURRENCY", 4, warnings, minimum=1)),
            lease_s=_number(env, "AI_HELPDESK_RUN_LEASE_S", 30.0, warnings, minimum=0.1),
            max_attempts=int(_number(env, "AI_HELPDESK_RUN_MAX_ATTEMPTS", 3, warnings, minimum=1)),
            queue_max=int(_number(env, "AI_HELPDESK_RUN_QUEUE_MAX", 200, warnings, minimum=1)),
            redispatch_after_s=_number(env, "AI_HELPDESK_RUN_REDISPATCH_S", 30.0, warnings, minimum=0.1),
            retention_days=int(_number(env, "AI_HELPDESK_RUN_RETENTION_DAYS", 14, warnings)),
            wait_s=_number(env, "AI_HELPDESK_RUN_WAIT_S", 120.0, warnings, minimum=1),
            warnings=tuple(warnings),
        )

    def problems(self, *, instance: str, postgres: bool, redis: bool) -> list[str]:
        """排队模式起不来的原因。inline 模式永远是空的。"""
        if not self.queued:
            return []
        out = []
        if not instance:
            out.append("AI_HELPDESK_INSTANCE 没设（staff / customer）：run 按实例分队列，不标实例就不知道该领哪条")
        if not postgres:
            out.append("没配 POSTGRES_DSN：run 的状态存在 Postgres，worker 和 API 是不同的进程")
        if not redis:
            out.append("没配 REDIS_URL：派发队列和帧流在 Redis")
        return out
