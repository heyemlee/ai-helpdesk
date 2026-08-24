"""
一轮对话的记录器。

调用点只写一行 `events().emit("intent_voted", data, body)`，不用判空、不用顺着参数传：
记录器放在一个 contextvar 里，和 `request_context.request_id_var` 一个做法。asyncio
建任务时会复制 context，所以 /chat/stream 的准备任务、作答的驱动任务、并行的两个专家、
轮次之后的后台任务（画像更新），都自动带着同一个记录器。没绑定时（启动、脚本）
`events()` 返回什么都不做的 NOOP。

`emit` 是同步的、O(1)，里面没有 await，所以也能在 `finally` 里、在流式生成器被关闭时
调用。它当场把 data 和 body 序列化成 JSON（快照），放进写入器的队列就返回；出任何
异常都只计数，不往外抛——记日志不能打断回答。

data 和 body 的分法是这份日志处理个人信息的基础：data 只放代码自己产生的枚举、数字、
布尔、id、哈希；任何来自客户、模型、知识库、业务系统或异常信息的文字都放 body。
body 可以单独清空（保留期），清空之后剩下的行里没有能指向人的东西。
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.core.metrics import TURN_EVENTS_DROPPED, TURN_EVENTS_WRITTEN
from app.events.types import EVENT_TYPES, EVENT_VERSION, TERMINAL_TYPES, Event

logger = logging.getLogger(__name__)

# 单条 body 序列化后的上限：超过就截最长的几个字符串，标 data.truncated。
MAX_EVENT_BODY_BYTES = 64 * 1024
# 一轮的 body 合计上限：超过之后的事件只记 data，标 data.body_dropped。
MAX_TURN_BODY_BYTES = 1024 * 1024
# data 本该很小；超过这个数说明有人往里放了不该放的东西，整条丢掉（too_large）。
MAX_DATA_BYTES = 64 * 1024
# 信封里的 id 最长多少字：conv_id 和 call_id 来自客户端和模型，超长会撑破索引项
MAX_ID_CHARS = 200

MODES = ("off", "meta", "full")

_MAX_DEPTH = 12
_CUT_MARK = "…[截断]"


def new_turn_id() -> str:
    """12 位十六进制毫秒时间戳加 14 位随机数：服务端生成，按生成时间排序。"""
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(7)}"


@dataclass
class EventLogStats:
    """一个进程的写入计数。/health 和 Prometheus 都从这里来。"""

    written: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    # 只记异常的类名：/health 在客户实例上是匿名可读的，异常信息里可能有库的地址
    last_error: str = ""
    last_error_at: float | None = None

    def wrote(self, n: int) -> None:
        self.written += n
        TURN_EVENTS_WRITTEN.inc(n)

    def drop(self, reason: str, n: int = 1) -> None:
        """reason 是固定集合：queue_full / db_error / too_large / serialize / shutdown。"""
        self.dropped[reason] = self.dropped.get(reason, 0) + n
        TURN_EVENTS_DROPPED.labels(reason=reason).inc(n)

    def error(self, ex: BaseException) -> None:
        self.last_error = type(ex).__name__
        self.last_error_at = time.time()


class EventSink(Protocol):
    """事件写到哪。`put` 必须同步、不抛：Postgres 那种只是放进队列。"""

    def put(self, event: Event) -> None: ...


# ── 序列化 ────────────────────────────────────────────────────────────────────

def clean_text(value: str) -> str:
    """
    去掉 Postgres 存不下的字符：NUL，和编不成 UTF-8 的孤立代理字符。

    这些字符客户端能直接送进来（请求体里的原话、conv_id，pydantic 照收），一条这样的事件
    会让同一批的两百条一起写不进去。
    """
    if "\x00" in value:
        value = value.replace("\x00", "")
    if value.isascii():
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def _clean_id(value: Any, limit: int) -> str:
    """信封里的 id（conv_id、request_id、call_id……）：清掉坏字符再截长。超长会撑破索引项。"""
    return clean_text(str(value or ""))[:limit]


def _jsonable(value: Any, depth: int = 0) -> Any:
    """
    转成能进 JSONB 的样子，同时就是一份深拷贝（快照）。

    JSONB 不收 `\\u0000`，也不收 NaN 和 Infinity，孤立代理字符连 UTF-8 都编不成：一条这样的
    事件会让同一批的两百条一起写不进去，所以在这里就清掉，不等数据库报错。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value, depth)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return clean_text(str(value))
    if isinstance(value, bytes):
        return _jsonable(value.decode("utf-8", "replace"), depth)
    if depth >= _MAX_DEPTH:
        return _jsonable(repr(value)[:2000], depth)
    if isinstance(value, dict):
        return {_key(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v, depth + 1) for v in value]
    return _jsonable(str(value), depth)


def _key(key: Any) -> str:
    if isinstance(key, Enum):
        key = key.value
    return clean_text(str(key))


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _string_sizes(value: Any) -> list[int]:
    """body 里每个字符串的字节数（UTF-8）。中文一个字三个字节，按字数算会差三倍。"""
    if isinstance(value, str):
        return [_size(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _string_sizes(v)]
    if isinstance(value, list):
        return [n for v in value for n in _string_sizes(v)]
    return []


def _cap_strings(value: Any, cap: int) -> Any:
    """长于 cap 字节的字符串按比例截到约 cap 字节，结尾标上截断。"""
    if isinstance(value, str):
        size = _size(value)
        if size <= cap:
            return value
        return value[:len(value) * cap // size] + _CUT_MARK
    if isinstance(value, dict):
        return {k: _cap_strings(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_strings(v, cap) for v in value]
    return value


def _cap_for(sizes: list[int], excess: int) -> int:
    """
    最大的 cap：把所有长于 cap 的字符串截到 cap，至少省下 excess 个字节。

    只截最长的那几个，短字段（查询词、工具名）原样留着。
    """
    ordered = sorted(sizes, reverse=True)
    prefix = 0
    for i, size in enumerate(ordered):
        prefix += size
        below = ordered[i + 1] if i + 1 < len(ordered) else 0
        cap = (prefix - excess) // (i + 1)
        if cap >= below:
            return max(cap, 0)
    return 0


def encode_body(body: Any, limit: int = MAX_EVENT_BODY_BYTES) -> tuple[str | None, bool]:
    """
    body → JSON 文本，超过 limit 字节就截最长的几个字符串。

    返回 (文本, 截没截过)。截到每个字符串只剩标记还放不下时返回 None：这一条只记结构。
    """
    text = _encode(body)
    size = _size(text)
    if size <= limit:
        return text, False
    sizes = _string_sizes(body)
    excess = size - limit
    for _ in range(6):
        cap = _cap_for(sizes, excess)
        text = _encode(_cap_strings(body, cap))
        size = _size(text)
        if size <= limit:
            return text, True
        if cap == 0:
            break
        # 还差的那一点是截断标记占的，加进去再截一次
        excess += size - limit
    return None, True


# ── 记录器 ────────────────────────────────────────────────────────────────────

class TurnRecorder:
    """
    一轮对话的记录器：分配 seq、打时间、快照、截断，交给写入器。

    `events` 留着这一轮发过的所有事件：结束时拿它做投影和合计，不读库。
    日志关着（mode=off）时什么都不记，但 turn_id 照样有、照样返回给前端。
    """

    def __init__(
        self,
        turn_id: str = "",
        *,
        instance: str = "",
        conv_id: str = "",
        request_id: str = "",
        mode: str = "off",
        sink: EventSink | None = None,
        stats: EventLogStats | None = None,
    ):
        self.turn_id = turn_id
        self.instance = instance
        # conv_id、request_id 都是客户端带来的：只清只截，不信。一个坏值不能连累同批别的轮次
        self.conv_id = _clean_id(conv_id, MAX_ID_CHARS)
        self.request_id = _clean_id(request_id, 64)
        self.mode = mode if sink is not None and mode in MODES else "off"
        self._sink = sink
        self._stats = stats if stats is not None else EventLogStats()
        self._seq = itertools.count()
        self._t0 = time.monotonic()
        self._body_bytes = 0
        self._totals = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
        self.events: list[Event] = []
        # 发过的第一个终结事件。流式的 finally 靠它判断要不要补一条 turn_aborted
        self.terminal = ""
        # 同一会话里还在途的其他轮次（turn_started 时登记，见 EventLog.open_turn）
        self.concurrent: list[str] = []
        self._on_close: Callable[[], None] | None = None

    @property
    def recording(self) -> bool:
        return self.mode != "off"

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self._t0) * 1000, 1)

    def totals(self) -> dict[str, int]:
        """到目前为止这一轮的模型调用数、token 和工具调用数。轮次之后的后台任务另算。"""
        return dict(self._totals)

    def emit(
        self,
        type: str,
        data: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        *,
        agent: str = "",
        step: int | None = None,
        call_id: str = "",
    ) -> None:
        """发一条事件。同步、不抛；日志关着时直接返回。"""
        if type in TERMINAL_TYPES and not self.terminal:
            self.terminal = type
        if self.mode == "off":
            return
        try:
            event = self._build(type, data, body, agent, step, call_id)
            if event is None:
                return
            self.events.append(event)
            self._sink.put(event)
        except Exception:
            logger.warning("事件 %s 没记下来（序列化失败）", type, exc_info=True)
            self._stats.drop("serialize")

    def _build(self, type, data, body, agent, step, call_id) -> Event | None:
        if type not in EVENT_TYPES:
            raise ValueError(f"未知的事件类型: {type}")
        data_obj = _jsonable(dict(data or {}))
        body_text: str | None = None
        if body is not None and self.mode == "full":
            if self._body_bytes >= MAX_TURN_BODY_BYTES:
                data_obj["body_dropped"] = True
            else:
                body_text, truncated = encode_body(_jsonable(body))
                if truncated:
                    data_obj["truncated"] = True
                if body_text is None:
                    data_obj["body_dropped"] = True
                else:
                    self._body_bytes += _size(body_text)
        data_text = _encode(data_obj)
        if _size(data_text) > MAX_DATA_BYTES:
            logger.warning("事件 %s 的 data 有 %d 字节，超过上限，这一条不记", type, _size(data_text))
            self._stats.drop("too_large")
            return None
        self._count(type, data_obj)
        return Event(
            turn_id=self.turn_id, seq=next(self._seq), instance=self.instance,
            conv_id=self.conv_id, request_id=self.request_id, type=type,
            agent=_clean_id(agent, MAX_ID_CHARS), step=step, call_id=_clean_id(call_id, MAX_ID_CHARS),
            ts=time.time(), t_ms=self.elapsed_ms(), v=EVENT_VERSION,
            data=data_text, body=body_text,
        )

    def _count(self, type: str, data: dict[str, Any]) -> None:
        if type == "tool_call":
            self._totals["tool_calls"] += 1
        elif type == "model_call":
            self._totals["model_calls"] += 1
            usage = data.get("usage") or {}
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key) if isinstance(usage, dict) else None
                if isinstance(value, int):
                    self._totals[key] += value

    def on_close(self, callback: Callable[[], None]) -> None:
        self._on_close = callback

    def close(self) -> None:
        """这一轮结束：从同会话在途表里拿掉。可以重复调。"""
        callback, self._on_close = self._on_close, None
        if callback is not None:
            callback()


# 没绑定时的那一个：什么都不记，调用点不用判空
NOOP = TurnRecorder()

_current: ContextVar[TurnRecorder | None] = ContextVar("turn_recorder", default=None)


def events() -> TurnRecorder:
    """当前这一轮的记录器；没绑定时返回 NOOP。"""
    return _current.get() or NOOP


@contextmanager
def bind(recorder: TurnRecorder) -> Iterator[TurnRecorder]:
    """在这个 with 里（以及从这里建出去的任务里），`events()` 就是这个记录器。"""
    token = _current.set(recorder)
    try:
        yield recorder
    finally:
        try:
            _current.reset(token)
        except ValueError:
            # 流式的生成器没跑完就被丢下时，由终结器在另一个 context 里关闭它：
            # 那个 context 里本来就没有绑定，不用还原
            pass


# ── 同一会话在途的轮次 ────────────────────────────────────────────────────────
#
# v1 只检测、只记录，不拦（第 7 节）。进程内的表只在单进程时准：两个实例现在都是单进程，
# 以后开多 worker 要换成 Redis 计数。正常情况下每轮结束都会拿掉自己；流式生成器一直
# 没被关掉的那种，过了 _STALE_S 当它不在了，免得表只增不减。

_IN_FLIGHT: dict[tuple[str, str], dict[str, float]] = {}
_STALE_S = 900.0


def register_in_flight(instance: str, conv_id: str, turn_id: str) -> list[str]:
    """登记这一轮，返回同一会话里还在途的其他轮次（按开始先后）。"""
    now = time.monotonic()
    turns = _IN_FLIGHT.setdefault((instance, conv_id), {})
    for other, started in list(turns.items()):
        if now - started > _STALE_S:
            del turns[other]
    others = sorted(turns, key=turns.__getitem__)
    turns[turn_id] = now
    return others


def unregister_in_flight(instance: str, conv_id: str, turn_id: str) -> None:
    turns = _IN_FLIGHT.get((instance, conv_id))
    if turns is None:
        return
    turns.pop(turn_id, None)
    if not turns:
        _IN_FLIGHT.pop((instance, conv_id), None)
