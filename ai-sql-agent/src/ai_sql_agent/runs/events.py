"""
run 事件：类型、信封、体积上限。

事件日志是给人排查和给控制台回放用的，不是契约面——A2A 的进度帧仍然只来自
`trace.stage()` 的 sink（契约 §4.5），这里加什么字段都不会出现在线上帧里。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

RUN_STARTED = "run.started"
RUN_FINISHED = "run.finished"
RUN_CANCEL_REQUESTED = "run.cancel_requested"
STAGE_FINISHED = "stage.finished"

FLUSH_ON = frozenset({STAGE_FINISHED, RUN_CANCEL_REQUESTED, RUN_FINISHED})
"""遇到这几类事件就把缓冲落库：一次请求十几次提交，崩溃最多丢掉当前 stage 的事件。"""

_STRING_LIMITS = {"sql": 8192, "final_sql": 8192, "error": 1024}
_DEFAULT_STRING_LIMIT = 1024
_MAX_ITEMS = 50


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    seq: int
    ts: str
    type: str
    stage: str = ""
    step: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "stage": self.stage,
            "step": self.step,
            "attrs": self.attrs,
        }


def _cap(key: str, value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        limit = _STRING_LIMITS.get(key, _DEFAULT_STRING_LIMIT)
        if len(value) <= limit:
            return value
        return f"{value[:limit]}…(+{len(value) - limit} chars)"
    if depth >= 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        capped = [_cap(key, item, depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            capped.append(f"…(+{len(items) - _MAX_ITEMS} items)")
        return capped
    if isinstance(value, dict):
        items = list(value.items())
        capped = {str(name): _cap(str(name), item, depth + 1) for name, item in items[:_MAX_ITEMS]}
        if len(items) > _MAX_ITEMS:
            capped["…"] = f"+{len(items) - _MAX_ITEMS} keys"
        return capped
    return _cap(key, str(value), depth)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def cap_attrs(attrs: Dict[str, Any], max_bytes: int) -> Dict[str, Any]:
    """
    截断长字符串和长列表；整体仍超过 `max_bytes` 时从最大的字段开始丢，
    丢掉的字段名记在 `_dropped` 里——丢了什么必须看得出来。
    """
    capped = {str(key): _cap(str(key), value) for key, value in (attrs or {}).items()}
    if _size(capped) <= max_bytes:
        return capped
    dropped = []
    for key in sorted(capped, key=lambda name: _size(capped[name]), reverse=True):
        dropped.append(key)
        capped.pop(key)
        if _size({**capped, "_dropped": dropped}) <= max_bytes:
            break
    capped["_dropped"] = dropped
    return capped
