"""
这一轮绑定的 run，和它的工具结果记录（memo）。

**为什么要记。** worker 在委派查库的第 35 秒被杀掉，接手的 worker 从头跑这一轮：没有
记录的话，ai-sql-agent 要再跑一遍 40 秒（还要再花它那边的模型调用），检索的改写和
重排也要再花两次模型往返。所以工具成功返回的结果按 run 记下来（Redis 的一个 hash，
24 小时过期），重跑时同一个工具、同样的参数直接拿记下的结果。这和 Temporal 这类
durable execution 引擎重放 activity 结果是同一个思路，只是粒度停在工具这一层：
模型调用本身不记——重跑时意图、路由可能和上一次不一样，那是新的一次执行，
结局以最后一次为准。

- **只记成功的结果。** 委派失败、超时、熔断的不记：下一次应该真的再试一次。
- **只在重跑时读。** 第一次执行时记录必然是空的，不为它多跑一趟 Redis。
- **键里没有"第几次"。** 同一个 run 的每一次执行共用一份记录；参数是规范化的 JSON，
  同一个问题、同样的参数才算同一次调用。
- 结果存不成 JSON 的（不该有）跳过，不影响这一轮。

`current_run()` 也在这里：工单、审计、记忆按 run 去重时，靠它知道"现在是不是在一个
run 里、是哪一个"。和事件日志的 `events()` 一样放在 contextvar 上，调用点不用顺着参数传。
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MEMO_TTL_S = 24 * 3600


def memo_key(kind: str, name: str, params: Any) -> str:
    """一次工具调用的记录键：种类、工具名和规范化之后的参数，取摘要。"""
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{kind}\x1f{name}\x1f{canonical}".encode()).hexdigest()[:32]


class RedisMemo:
    def __init__(self, redis, *, prefix: str = "ai-helpdesk", ttl_s: int = MEMO_TTL_S):
        self._redis = redis
        self._prefix = prefix
        self._ttl_s = ttl_s

    def _key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:memo"

    async def get(self, run_id: str, key: str) -> dict[str, Any] | None:
        raw = await self._redis.hget(self._key(run_id), key)
        return json.loads(raw) if raw else None

    async def put(self, run_id: str, key: str, value: dict[str, Any]) -> None:
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(self._key(run_id), key, json.dumps(value, ensure_ascii=False))
        pipe.expire(self._key(run_id), self._ttl_s)
        await pipe.execute()


class MemoryMemo:
    def __init__(self):
        self._store: dict[str, dict[str, str]] = {}

    async def get(self, run_id: str, key: str) -> dict[str, Any] | None:
        raw = self._store.get(run_id, {}).get(key)
        return json.loads(raw) if raw else None

    async def put(self, run_id: str, key: str, value: dict[str, Any]) -> None:
        self._store.setdefault(run_id, {})[key] = json.dumps(value, ensure_ascii=False)

    def entries(self, run_id: str) -> int:
        return len(self._store.get(run_id, {}))


@dataclass(frozen=True)
class RunScope:
    """当前这一轮属于哪个 run、第几次执行，以及工具结果记在哪。"""

    run_id: str
    attempt: int
    instance: str = ""
    memo: Any = None

    @property
    def replaying(self) -> bool:
        """是不是重跑：只有重跑才去读记录。"""
        return self.attempt > 1 and self.memo is not None

    def dedup_key(self, what: str) -> str:
        """这个 run 的某一个副作用的去重键：同一个 run 的每一次执行算出来都一样。"""
        return f"run:{self.instance}:{self.run_id}:{what}"

    async def recall(self, key: str) -> dict[str, Any] | None:
        if not self.replaying:
            return None
        try:
            return await self.memo.get(self.run_id, key)
        except Exception:
            logger.warning("读 run %s 的工具记录失败，这一次照常调用", self.run_id, exc_info=True)
            return None

    async def remember(self, key: str, value: dict[str, Any]) -> None:
        if self.memo is None:
            return
        try:
            await self.memo.put(self.run_id, key, value)
        except (TypeError, ValueError):
            logger.debug("工具结果存不成 JSON，不记（run %s）", self.run_id, exc_info=True)
        except Exception:
            logger.warning("写 run %s 的工具记录失败，重跑时这一次要重新调用", self.run_id, exc_info=True)


_current: ContextVar[RunScope | None] = ContextVar("run_scope", default=None)


def current_run() -> RunScope | None:
    """当前绑定的 run；不在 run 里（inline 模式、脚本、评测）时为 None。"""
    return _current.get()


@contextmanager
def bind_run(scope: RunScope) -> Iterator[RunScope]:
    token = _current.set(scope)
    try:
        yield scope
    finally:
        try:
            _current.reset(token)
        except ValueError:
            pass


def deterministic_id(prefix: str, scope: RunScope, what: str, length: int = 12) -> str:
    """由 run 推出来的 id（工单号之类）：同一个 run 重跑多少遍都是这一个，插入时撞主键就是"已经有了"。"""
    digest = hashlib.sha256(scope.dedup_key(what).encode()).hexdigest()[:length]
    return f"{prefix}{digest}"
