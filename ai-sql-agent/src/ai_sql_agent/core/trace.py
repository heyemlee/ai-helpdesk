from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from .logging_setup import get_logger, log_event
from .metrics import STAGE_DURATION
from .run_context import RunCancelled, check_cancelled, current_run, stage_frame

_trace_id: ContextVar[str] = ContextVar("ai_sql_trace_id", default="")

# 阶段事件汇（A2A 流式进度的唯一来源）。
#
# 契约 §8.2 挑中 stage() 当钩子的理由：所有阶段本来就都穿过这一个上下文管理器，
# 在这里推一条事件，pipeline 侧零改动。事件在 stage **进入时**发出——等 stage
# 结束再发就又变回事后通知，流式的全部意义就没了（契约 §4.5）。所以附带字段只
# 可能是进入时已经知道的那些（step / database / dialect），阶段结束才有的
# （命中的表、route/confidence）不在进度事件里，它们在终态的 artifact 里。
#
# 汇是 ContextVar：一次请求一个，不同请求互不串。同步 pipeline 跑在工作线程里
# 时，asyncio.to_thread 会把当前上下文拷过去，所以在协程里 use_stage_sink()
# 之后再 to_thread，线程内的 stage() 照样能看到这个汇。
_stage_sink: ContextVar[Optional[Callable[[str, Dict[str, Any]], None]]] = ContextVar(
    "ai_sql_stage_sink", default=None
)


@contextmanager
def use_stage_sink(sink: Callable[[str, Dict[str, Any]], None]) -> Iterator[None]:
    """在本上下文内，每个 stage() 进入时调用 sink(name, fields)。"""
    token = _stage_sink.set(sink)
    try:
        yield
    finally:
        _stage_sink.reset(token)
_usage: ContextVar[Optional["UsageAccumulator"]] = ContextVar(
    "ai_sql_usage", default=None
)

_logger = get_logger("trace")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str:
    return _trace_id.get()


@contextmanager
def use_trace_id(trace_id: str = "") -> Iterator[str]:
    """
    绑定一次请求的 trace_id。

    同时重置 Token 用量累加器，使一次请求的用量可以独立汇总。
    """
    resolved = trace_id or new_trace_id()
    trace_token = _trace_id.set(resolved)
    usage_token = _usage.set(UsageAccumulator())
    try:
        yield resolved
    finally:
        _trace_id.reset(trace_token)
        _usage.reset(usage_token)


@contextmanager
def usage_scope() -> Iterator[None]:
    """确保当前上下文里有 Token 用量累加器。

    HTTP 入口靠 `use_trace_id()` 建上下文；Pipeline 被当成库直接调用时
    （benchmark、脚本、单测）没人建，`record_usage()` 会静默丢弃，汇总恒为 0。
    这里补一个：已经有累加器就原样复用，不新建也不清空——否则嵌套调用会把
    上层已经累计的记录冲掉。
    """
    if _usage.get() is not None:
        yield
        return
    token = _usage.set(UsageAccumulator())
    try:
        yield
    finally:
        _usage.reset(token)


@dataclass
class ModelUsage:
    """单次模型调用的用量。"""

    component: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class UsageAccumulator:
    """请求级 Token 用量累加器。"""

    records: List[ModelUsage] = field(default_factory=list)

    def add(self, usage: ModelUsage) -> None:
        self.records.append(usage)

    def summary(self) -> Dict[str, Any]:
        by_component: Dict[str, int] = {}
        for record in self.records:
            by_component[record.component] = (
                by_component.get(record.component, 0) + record.total_tokens
            )
        return {
            "calls": len(self.records),
            "prompt_tokens": sum(r.prompt_tokens for r in self.records),
            "completion_tokens": sum(r.completion_tokens for r in self.records),
            "total_tokens": sum(r.total_tokens for r in self.records),
            "by_component": by_component,
        }


def record_usage(
    component: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """登记一次模型调用用量；不在请求上下文中时静默忽略。"""
    accumulator = _usage.get()
    if accumulator is None:
        return
    accumulator.add(
        ModelUsage(
            component=component,
            model=model,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
        )
    )


def usage_summary() -> Dict[str, Any]:
    accumulator = _usage.get()
    if accumulator is None:
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "by_component": {},
        }
    return accumulator.summary()


class StageRecorder:
    """阶段内补充字段的句柄，见 `stage()`。"""

    def __init__(self) -> None:
        self.fields: Dict[str, Any] = {}

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)


@contextmanager
def stage(
    name: str,
    logger: Optional[logging.Logger] = None,
    **initial_fields: Any,
) -> Iterator[StageRecorder]:
    """
    给一个处理阶段计时并输出结构化日志。

        with stage("retrieval", query=query) as st:
            result = service.retrieve(...)
            st.set(databases=result.selected_databases)

    正常结束记 INFO（status=ok），抛异常记 ERROR（status=error）后原样向上抛。
    这次请求被取消时记 INFO（status=cancelled）——取消不是故障。

    在一次 run 里（见 `run_context`）还会做两件事：进入前检查取消（已取消就不进这个阶段，
    也不发它的进度事件），进入和退出各向 run 的事件日志发一条 `stage.started` /
    `stage.finished`。不在 run 里时这两件事都是空操作。
    """
    active_logger = logger or _logger
    recorder = StageRecorder()
    recorder.fields.update(initial_fields)

    # 取消检查点：所有阶段都穿过这里，一处覆盖全部阶段边界
    check_cancelled()
    started = time.perf_counter()

    # 进入即推。sink 抛错不能把 pipeline 带崩——进度是附加物，查询才是主体。
    sink = _stage_sink.get()
    if sink is not None:
        try:
            sink(name, dict(initial_fields))
        except Exception as exc:  # pragma: no cover - 防御性
            log_event(active_logger, logging.WARNING, "stage.sink_failed",
                      stage=name, error=f"{type(exc).__name__}: {exc}")

    run = current_run()
    step = initial_fields.get("step")
    step = step if isinstance(step, int) and not isinstance(step, bool) else None
    if run is not None:
        # step 进事件的信封，不在 attrs 里重复
        run.emit("stage.started", stage=name, step=step,
                 **{key: value for key, value in initial_fields.items() if key not in ("stage", "step")})

    with stage_frame(name, step):
        try:
            yield recorder
        except RunCancelled as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            STAGE_DURATION.observe(
                time.perf_counter() - started, stage=name, status="cancelled"
            )
            log_event(
                active_logger,
                logging.INFO,
                f"stage.{name}",
                status="cancelled",
                duration_ms=duration_ms,
                cancel_reason=exc.reason,
                **_without_reserved(
                    recorder.fields, ("status", "duration_ms", "cancel_reason")
                ),
            )
            if run is not None:
                run.emit("stage.finished", stage=name, step=step, status="cancelled",
                         duration_ms=duration_ms, fields=recorder.fields)
            raise
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            STAGE_DURATION.observe(
                time.perf_counter() - started, stage=name, status="error"
            )
            log_event(
                active_logger,
                logging.ERROR,
                f"stage.{name}",
                status="error",
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error=str(exc),
                **_without_reserved(
                    recorder.fields, ("status", "duration_ms", "error_type", "error")
                ),
            )
            if run is not None:
                run.emit("stage.finished", stage=name, step=step, status="error",
                         duration_ms=duration_ms, error_type=type(exc).__name__,
                         error=str(exc), fields=recorder.fields)
            raise
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            STAGE_DURATION.observe(
                time.perf_counter() - started, stage=name, status="ok"
            )
            log_event(
                active_logger,
                logging.INFO,
                f"stage.{name}",
                status="ok",
                duration_ms=duration_ms,
                **_without_reserved(recorder.fields, ("status", "duration_ms")),
            )
            if run is not None:
                run.emit("stage.finished", stage=name, step=step, status="ok",
                         duration_ms=duration_ms, fields=recorder.fields)


def _without_reserved(fields: Dict[str, Any], reserved: tuple) -> Dict[str, Any]:
    """
    阶段自己带的字段里，与这条日志固定字段同名的改名为 `stage_<名字>`。

    修复阶段进来时就带着一个 `error`（要修的那条执行错误）。阶段里再抛异常时，
    「这个阶段是怎么失败的」也叫 `error`——两个同名关键字参数会让这里自己抛 TypeError，
    把原来的异常盖掉：上层等着接 ModelCallError 走降级，接到的却是一个 TypeError，
    一路冒成 500。两个都留着，只是不能叫同一个名字。
    """
    if not any(key in fields for key in reserved):
        return fields
    return {
        (f"stage_{key}" if key in reserved else key): value
        for key, value in fields.items()
    }
