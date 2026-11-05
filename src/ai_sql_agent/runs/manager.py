"""
run 的生命周期：开始、事件编号与落库、结束、取消、孤儿清理、保留期清理。

入口（`/query`、A2A send / stream）在准入通过之后、调用 service 之前 `start()` 一次，
再用 `running()` 包住整个处理过程；`running()` 按出口决定终态：正常结束 → completed，
`RunCancelled` → cancelled，其他异常 → failed。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from ai_sql_agent.core import get_logger, get_trace_id, log_event, usage_summary
from ai_sql_agent.core.metrics import (
    RUN_CANCEL_LATENCY,
    RUN_CANCELLATIONS,
    RUN_EVENTS_DROPPED,
    RUNS_TOTAL,
)
from ai_sql_agent.core.run_context import RunCancelled, RunContext, use_run

from .events import (
    FLUSH_ON,
    RUN_CANCEL_REQUESTED,
    RUN_FINISHED,
    RUN_STARTED,
    RunEvent,
    cap_attrs,
    utc_now_iso,
)
from .store import NullRunStore, RunStore, orphans, process_owner

_logger = get_logger("runs")

Subscriber = Callable[[RunEvent], None]


class RunRecorder:
    """
    一次 run 的事件发射器（`RunContext.sink`）：加锁分配序号、缓冲，
    遇到 stage 结束 / 取消 / run 结束时批量落库。

    落库失败只影响这次 run 的事件日志：记一条日志、计数，之后的事件只留在内存订阅者那里。
    查询本身不受影响——事件是附加物。
    """

    def __init__(self, run_id: str, store: RunStore, *, max_attr_bytes: int,
                 subscribers: Sequence[Subscriber] = ()):
        self.run_id = run_id
        self.store = store
        self.max_attr_bytes = max_attr_bytes
        self.subscribers = list(subscribers)
        self.persist = True
        self._seq = 0
        self._buffer: List[RunEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event_type: str, stage: str, step: Optional[int], attrs: Dict[str, Any]) -> None:
        capped = cap_attrs(attrs, self.max_attr_bytes)
        with self._lock:
            self._seq += 1
            event = RunEvent(
                run_id=self.run_id, seq=self._seq, ts=utc_now_iso(), type=event_type,
                stage=stage or "", step=step, attrs=capped,
            )
            self._buffer.append(event)
        for subscriber in self.subscribers:
            try:
                subscriber(event)
            except Exception:  # pragma: no cover - 订阅者是旁路
                pass
        if event_type in FLUSH_ON:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            pending, self._buffer = self._buffer, []
        if not pending or not self.persist:
            return
        try:
            self.store.append_events(pending)
        except Exception as exc:
            self.persist = False
            RUN_EVENTS_DROPPED.inc(len(pending))
            log_event(_logger, logging.WARNING, "runs.persist_failed", run_id=self.run_id,
                      dropped=len(pending), error=f"{type(exc).__name__}: {exc}")


class RunManager:
    def __init__(
        self,
        store: Optional[RunStore] = None,
        *,
        max_attr_bytes: int = 16384,
        retention_days: float = 7.0,
        stale_seconds: float = 3600.0,
        deadline_seconds: float = 0.0,
    ):
        self.store: RunStore = store if store is not None else NullRunStore()
        self.max_attr_bytes = max_attr_bytes
        self.deadline_seconds = deadline_seconds
        self.retention_days = retention_days
        self.stale_seconds = stale_seconds
        self.owner = process_owner()
        self._active: Dict[str, RunContext] = {}
        self._owners: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._maintenance: Optional[threading.Thread] = None
        self.subscribers: List[Subscriber] = []

    # ------------------------------------------------------------------
    # 开始与结束
    # ------------------------------------------------------------------

    @staticmethod
    def new_run_id() -> str:
        return uuid.uuid4().hex

    def start(self, *, entry: str, caller: str, user_id: str, session_id: str, query: str,
              run_id: str = "", trace_id: str = "") -> RunContext:
        """
        登记一次 run（状态 running）并发出 `run.started`。

        先登记、后执行：流式入口在发首帧之前就调它，这样调用方拿到 taskId 之后立刻取消，
        也能在登记表里找到这次 run。
        """
        run_id = run_id or self.new_run_id()
        recorder = RunRecorder(run_id, self.store, max_attr_bytes=self.max_attr_bytes,
                               subscribers=self.subscribers)
        run = RunContext(run_id=run_id, trace_id=trace_id or get_trace_id(), entry=entry,
                         sink=recorder)
        run.canceller = lambda reason, _run_id=run_id: self.cancel(_run_id, reason)
        if self.deadline_seconds > 0:
            run.deadline = time.monotonic() + self.deadline_seconds
        try:
            self.store.create_run({
                "run_id": run_id, "trace_id": run.trace_id, "entry": entry, "caller": caller,
                "user_id": user_id, "session_id": session_id, "query": query, "owner": self.owner,
            })
        except Exception as exc:
            recorder.persist = False
            log_event(_logger, logging.WARNING, "runs.persist_failed", run_id=run_id,
                      error=f"{type(exc).__name__}: {exc}")
        with self._lock:
            self._active[run_id] = run
            self._owners[run_id] = (caller, user_id)
        run.emit(RUN_STARTED, stage="", step=None, entry=entry, caller=caller, user_id=user_id,
                 session_id=session_id, query_chars=len(query or ""))
        return run

    @contextmanager
    def running(self, run: Optional[RunContext]) -> Iterator[Optional[RunContext]]:
        """绑定 run 到当前上下文，并按出口决定终态。传 None 什么都不做。"""
        if run is None:
            yield None
            return
        with use_run(run):
            try:
                yield run
            except RunCancelled as exc:
                self.finish(run, "cancelled", error=str(exc))
                raise
            except BaseException as exc:
                # 入口会把 RunCancelled 翻成自己的响应（HTTP 499 等）再抛出来：令牌置过就还是取消
                status = "cancelled" if run.token.cancelled else "failed"
                self.finish(run, status, error=f"{type(exc).__name__}: {exc}")
                raise
            else:
                self.finish(run, "completed")

    def finish(self, run: RunContext, status: str, *, error: str = "") -> None:
        with self._lock:
            if run.status != "running":
                return
            run.status = status
            self._active.pop(run.run_id, None)
            self._owners.pop(run.run_id, None)
        usage = usage_summary()
        cancel_reason = run.token.reason if run.token.cancelled else ""
        run.emit(RUN_FINISHED, stage="", step=None, status=status, error=error or None,
                 cancel_reason=cancel_reason or None, usage=usage, **run.outcome)
        recorder = run.sink if isinstance(run.sink, RunRecorder) else None
        if recorder is not None:
            recorder.flush()
        if recorder is None or recorder.persist:
            try:
                self.store.finish_run(run.run_id, status=status, error=error,
                                      cancel_reason=cancel_reason, usage=usage,
                                      outcome=dict(run.outcome))
            except Exception as exc:
                log_event(_logger, logging.WARNING, "runs.persist_failed", run_id=run.run_id,
                          error=f"{type(exc).__name__}: {exc}")
        RUNS_TOTAL.inc(entry=run.entry or "unknown", status=status)
        if status == "cancelled" and run.token.requested_at is not None:
            RUN_CANCEL_LATENCY.observe(time.monotonic() - run.token.requested_at,
                                       reason=run.token.reason or "unknown")

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self, run_id: str, reason: str) -> str:
        """
        请求取消一次 run。返回：

        - `cancel_requested`：本进程里正在跑，已置取消标记，下一个检查点生效；
        - `already_finished`：已经结束；
        - `not_found`：没有这次 run，或它不在本进程里跑（单进程部署下就是早已中断的孤儿）。
        """
        with self._lock:
            run = self._active.get(run_id)
        if run is not None:
            if run.token.cancel(reason):
                RUN_CANCELLATIONS.inc(reason=reason)
                run.emit(RUN_CANCEL_REQUESTED, stage="", step=None, reason=reason)
                try:
                    self.store.set_cancel_reason(run_id, reason)
                except Exception:  # pragma: no cover - 取消标记在内存里已经生效
                    pass
            return "cancel_requested"
        stored = self.store.get_run(run_id)
        if stored is None:
            return "not_found"
        return "already_finished" if stored.get("status") != "running" else "not_found"

    def active(self, run_id: str) -> Optional[RunContext]:
        with self._lock:
            return self._active.get(run_id)

    def owner_of(self, run_id: str) -> Optional[tuple]:
        """(caller, user_id)。正在跑的从内存取（关掉持久化时也查得到），否则查存储。"""
        with self._lock:
            owner = self._owners.get(run_id)
        if owner is not None:
            return owner
        stored = self.store.get_run(run_id)
        if stored is None:
            return None
        return (stored.get("caller", ""), stored.get("user_id", ""))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_run(run_id)

    def events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        return self.store.list_events(run_id, after_seq=after_seq, limit=limit)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def sweep_orphans(self) -> int:
        """把不会再结束的 running 标成 interrupted（启动时调用一次，之后随保留期清理一起跑）。"""
        try:
            with self._lock:
                mine = set(self._active)
            candidates = [run for run in self.store.running_runs() if run["run_id"] not in mine]
            found = orphans(candidates, stale_seconds=self.stale_seconds)
            marked = self.store.mark_interrupted(found, "interrupted: process exited before the run finished")
        except Exception as exc:
            log_event(_logger, logging.WARNING, "runs.sweep_failed", error=f"{type(exc).__name__}: {exc}")
            return 0
        if marked:
            log_event(_logger, logging.INFO, "runs.orphans_interrupted", count=marked)
        return marked

    def purge_expired(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        try:
            removed = self.store.purge_finished_before(
                cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
        except Exception as exc:
            log_event(_logger, logging.WARNING, "runs.purge_failed", error=f"{type(exc).__name__}: {exc}")
            return 0
        if removed:
            log_event(_logger, logging.INFO, "runs.purged", count=removed, retention_days=self.retention_days)
        return removed

    def start_maintenance(self, interval_seconds: float = 3600.0) -> None:
        """守护线程：每隔一段时间清一次过期 run 和孤儿。"""
        if self._maintenance is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                self.purge_expired()
                self.sweep_orphans()

        self._maintenance = threading.Thread(target=loop, name="runs-maintenance", daemon=True)
        self._maintenance.start()

    def shutdown(self) -> None:
        """
        进程收尾：uvicorn 默认先等在途请求结束，走到这里还在登记表里的 run 已经不会有人收尾了，
        置取消标记并标成 interrupted。
        """
        self._stop.set()
        with self._lock:
            leftover = list(self._active.values())
        for run in leftover:
            run.token.cancel("shutdown")
            self.finish(run, "interrupted", error="interrupted: service shutting down")
        try:
            self.store.close()
        except Exception:  # pragma: no cover
            pass


def runs_of(app: Any) -> Optional[RunManager]:
    """取 app 上的 RunManager；没有 lifespan 的测试 app、或单测里 request=None 时返回 None。"""
    return getattr(getattr(app, "state", None), "runs", None)
