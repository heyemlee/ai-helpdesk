"""
一个 run 的帧流：worker 写、API 读，断线后按最后收到的那一帧接着读。

每个 run 一条 Redis Stream（`ai-helpdesk:run:{run_id}:frames`）。一帧就是 /chat/stream 原来的
一个 SSE 事件（progress / meta / routing / delta / result / error），加上它是第几次执行
（attempt）。条目 id 是 Redis 生成的、单调递增，直接当 SSE 的 `id:`：浏览器（或我们自己的
前端）重连时带 `Last-Event-ID`，从它后面接着读，一帧不重、一帧不漏。

- **帧不是状态。** 帧流可以整条丢掉（Redis 没持久化、过期），run 的结局在 Postgres。
  续流的一方发现 run 已经结束、帧流里却没有终结帧时，从库里的结果补出那一帧
  （app/runs/service.py）。
- **过期。** 每写一帧把整条流的过期时间推到 24 小时后；结束之后不再写，24 小时后自己没了。
  断线重连的窗口远小于这个数。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

FRAMES_TTL_S = 24 * 3600
FRAMES_MAXLEN = 5_000


@dataclass(frozen=True)
class Frame:
    id: str
    event: str
    data: dict[str, Any]
    attempt: int


def frames_key(prefix: str, run_id: str) -> str:
    return f"{prefix}:run:{run_id}:frames"


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _frame(message_id, fields) -> Frame:
    def get(key: str):
        return fields.get(key) if key in fields else fields.get(key.encode())

    return Frame(
        id=_text(message_id),
        event=_text(get("e") or ""),
        data=json.loads(_text(get("d") or "{}")),
        attempt=int(_text(get("a") or "0")),
    )


class RedisChannel:
    def __init__(self, redis, *, prefix: str = "ai-helpdesk", ttl_s: int = FRAMES_TTL_S):
        self._redis = redis
        self._prefix = prefix
        self._ttl_s = ttl_s

    async def publish(self, run_id: str, event: str, data: dict[str, Any], attempt: int) -> str:
        key = frames_key(self._prefix, run_id)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(key, {"e": event, "d": json.dumps(data, ensure_ascii=False), "a": str(attempt)},
                  maxlen=FRAMES_MAXLEN, approximate=True)
        pipe.expire(key, self._ttl_s)
        message_id, _ = await pipe.execute()
        return _text(message_id)

    async def read(self, run_id: str, after: str, *, block_ms: int, count: int = 200) -> list[Frame]:
        """after 之后的帧；一帧都没有时最多等 block_ms。after 为 "0-0" 时从头读。"""
        reply = await self._redis.xread({frames_key(self._prefix, run_id): after or "0-0"},
                                        count=count, block=block_ms)
        out = []
        for _key, entries in reply or []:
            for message_id, fields in entries:
                out.append(_frame(message_id, fields))
        return out


class MemoryChannel:
    """进程内的帧流：单测用。"""

    def __init__(self):
        self._frames: dict[str, list[Frame]] = {}
        self._seq = 0
        self._changed = asyncio.Condition()

    async def publish(self, run_id: str, event: str, data: dict[str, Any], attempt: int) -> str:
        self._seq += 1
        frame = Frame(f"{self._seq}-0", event, json.loads(json.dumps(data)), attempt)
        self._frames.setdefault(run_id, []).append(frame)
        async with self._changed:
            self._changed.notify_all()
        return frame.id

    def _after(self, run_id: str, after: str) -> list[Frame]:
        seq = int((after or "0-0").split("-")[0])
        return [f for f in self._frames.get(run_id, []) if int(f.id.split("-")[0]) > seq]

    async def read(self, run_id: str, after: str, *, block_ms: int, count: int = 200) -> list[Frame]:
        frames = self._after(run_id, after)
        if frames:
            return frames[:count]
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: bool(self._after(run_id, after))),
                                       block_ms / 1000)
            except TimeoutError:
                return []
        return self._after(run_id, after)[:count]

    def frames(self, run_id: str) -> list[Frame]:
        return list(self._frames.get(run_id, []))

    def drop(self, run_id: str) -> None:
        """模拟帧流丢了（Redis 重启没持久化）。"""
        self._frames.pop(run_id, None)
