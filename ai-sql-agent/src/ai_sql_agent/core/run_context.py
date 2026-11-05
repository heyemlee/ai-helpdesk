"""
一次请求的运行上下文（run）：run_id、取消令牌、事件发射。

和 trace_id、用量累加器、授权作用域一样走 ContextVar。三个入口已经把上下文带进了工作线程
（/query 经 anyio、send 经 `asyncio.to_thread`、stream 经 `copy_context()`），这里不用另想办法。

不在 run 里时（benchmark、脚本、单测直接调 pipeline），这里的每个函数都是空操作——
和 `usage_scope()` 对库调用的处理一样，库调用不该因为没有 HTTP 入口就多出行为。

事件写到哪里、怎么落库不归这里管：`RunContext.sink` 是一个回调，由
`ai_sql_agent.runs` 在开始一次 run 时装上。core 不能反过来依赖上层的包。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple


class RunCancelled(Exception):
    """
    这次 run 被取消了。

    **直接继承 Exception**，不能是 RuntimeError / ValueError / ModelCallError：

    - 路由会把 RuntimeError 当成模型失败，退回规则版继续跑（`pipeline/routing.py`）；
    - pipeline 会把 ModelCallError 当成模型故障，转去重放历史 SQL（`_degrade`）。

    取消之后，谁都不该再干活。
    """

    def __init__(self, reason: str = "cancelled"):
        super().__init__(f"cancelled: {reason}")
        self.reason = reason


class CancelToken:
    """线程安全的取消标记。第一次 `cancel()` 生效，之后幂等。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._callbacks: List[Callable[[], None]] = []
        self.requested_at: Optional[float] = None
        """收到取消的时刻（monotonic），用来量"取消到停下"花了多久。"""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        """置取消标记并触发回调。第一次调用返回 True，之后返回 False。"""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason or "cancelled"
            self.requested_at = time.monotonic()
            self._event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            _call_quietly(callback)
        return True

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelled(self._reason)

    def wait(self, seconds: float) -> bool:
        """最多等 `seconds` 秒；被取消就提前返回 True。退避等待用它代替 `time.sleep`。"""
        return self._event.wait(max(0.0, seconds))

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """
        取消时调用 `callback`，返回注销函数。已经取消的话立刻调一次。

        用途是"尽快停下"：比如 PostgreSQL 执行期间注册 `conn.cancel_safe`，
        取消一到就让服务端中断语句，不必等它自己跑完。回调出错不影响取消本身——
        下一个检查点照样会停。
        """
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)

                def remove() -> None:
                    with self._lock:
                        try:
                            self._callbacks.remove(callback)
                        except ValueError:
                            pass

                return remove
        _call_quietly(callback)
        return lambda: None


def _call_quietly(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:  # pragma: no cover - 回调失败只是"停得慢一点"
        pass


EventSink = Callable[[str, str, Optional[int], Dict[str, Any]], None]
"""(type, stage, step, attrs) -> None，由 `ai_sql_agent.runs` 提供。"""


@dataclass
class RunContext:
    """一次 run 在进程里的句柄。"""

    run_id: str
    trace_id: str = ""
    entry: str = ""
    token: CancelToken = field(default_factory=CancelToken)
    sink: Optional[EventSink] = None
    outcome: Dict[str, Any] = field(default_factory=dict)
    """入口在结束前填的业务结论（degraded、execution_status……），写进 run.finished。"""

    status: str = "running"
    """running → completed / failed / cancelled / interrupted，由 `ai_sql_agent.runs` 维护。"""

    deadline: Optional[float] = None
    """总时长上限（monotonic 时刻）。过了就在下一个检查点按 `deadline` 取消；None = 不限。"""

    canceller: Optional[Callable[[str], Any]] = None
    """
    走 RunManager 的取消入口（记事件、计指标、落库），由 `ai_sql_agent.runs` 装上。
    没装时直接置令牌。
    """

    def request_cancel(self, reason: str) -> None:
        if self.canceller is not None:
            self.canceller(reason)
        # 兜底：canceller 没找到这次 run（比如它正在收尾）时令牌也必须置上
        self.token.cancel(reason)

    def emit(self, event_type: str, *, stage: Optional[str] = None,
             step: Optional[int] = None, **attrs: Any) -> None:
        if self.sink is None:
            return
        try:
            self.sink(
                event_type,
                current_stage() if stage is None else stage,
                current_step() if step is None else step,
                attrs,
            )
        except Exception:  # pragma: no cover - 事件是附加物，不能把查询带崩
            pass

    def set_outcome(self, **fields: Any) -> None:
        self.outcome.update(fields)


_current_run: ContextVar[Optional[RunContext]] = ContextVar("ai_sql_run", default=None)
_stage_stack: ContextVar[Tuple[Tuple[str, Optional[int]], ...]] = ContextVar(
    "ai_sql_run_stages", default=()
)


def current_run() -> Optional[RunContext]:
    return _current_run.get()


@contextmanager
def use_run(run: Optional[RunContext]) -> Iterator[Optional[RunContext]]:
    """在本上下文内绑定 run；传 None 等于什么都不做。"""
    if run is None:
        yield None
        return
    token = _current_run.set(run)
    try:
        yield run
    finally:
        _current_run.reset(token)


def check_cancelled() -> None:
    """
    取消检查点：当前 run 已被取消（或超过了总时长上限）就抛 RunCancelled；
    不在 run 里是空操作。
    """
    run = _current_run.get()
    if run is None:
        return
    if run.deadline is not None and not run.token.cancelled and time.monotonic() > run.deadline:
        run.request_cancel("deadline")
    run.token.raise_if_cancelled()


def cancel_token() -> Optional[CancelToken]:
    run = _current_run.get()
    return run.token if run is not None else None


def cancellable_sleep(seconds: float, sleep: Callable[[float], None] = time.sleep) -> None:
    """
    可被取消打断的等待（重试退避用）。

    在 run 里、且用的是真正的 `time.sleep`：改成等取消令牌，被取消就立刻抛 RunCancelled。
    其余情况照旧调 `sleep`——调用方的 `_sleep` 可能被测试换成了不真睡的假函数，
    那时不能替它真的等；睡完再检查一次取消。
    """
    token = cancel_token()
    if token is None or sleep is not time.sleep:
        sleep(seconds)
        check_cancelled()
        return
    token.wait(seconds)
    token.raise_if_cancelled()


def emit(event_type: str, *, step: Optional[int] = None, **attrs: Any) -> None:
    """向当前 run 发一个事件；不在 run 里是空操作。"""
    run = _current_run.get()
    if run is not None:
        run.emit(event_type, step=step, **attrs)


def current_stage() -> str:
    """最内层的内部 stage 名（如 sql_generation）；不在任何 stage 里时为空串。"""
    stack = _stage_stack.get()
    return stack[-1][0] if stack else ""


def current_step() -> Optional[int]:
    """最内层带步骤号的 stage 的步骤号；执行器发的 tool.sql 事件靠它知道自己属于第几步。"""
    for _name, step in reversed(_stage_stack.get()):
        if step is not None:
            return step
    return None


@contextmanager
def stage_frame(name: str, step: Optional[int] = None) -> Iterator[None]:
    """把 (name, step) 压进当前上下文的 stage 栈，退出时弹出。由 `trace.stage()` 调用。"""
    token = _stage_stack.set(_stage_stack.get() + ((name, step),))
    try:
        yield
    finally:
        _stage_stack.reset(token)
