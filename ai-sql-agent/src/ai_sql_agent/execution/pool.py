"""
有界连接池：控制连接与执行开销。

`PostgresMCPExecutor` 以前每次执行都 `psycopg.connect()`。单条查询看不出来，
并发一上来两个问题一起冒：建连接的握手与认证开销被乘上 QPS，
而且**连接数没有上限**——业务库的 `max_connections` 由客户端的心情决定。

刻意不引 `psycopg_pool`：这里要的东西很少（借、还、丢弃太旧的），
为它多一个运行时依赖不划算。

**归还前的状态清理由调用方负责。** 这个池不替执行器 rollback——
执行器本来就在 finally 里 rollback，池再来一次只会掩盖「谁忘了收尾」。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Tuple


class BoundedConnectionPool:
    """
    最多 `max_size` 条连接；借不到就现建一条**临时**连接，用完直接关。

    为什么不阻塞等待：SQL 执行已经有 `statement_timeout` 兜底，
    在这里再叠一层「等连接」的排队，会让超时预算变成两段不可见的相加。
    宁可多建一条一次性连接，也不要让调用方在一个没人计时的地方排队。
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        max_size: int = 8,
        idle_seconds: float = 300.0,
    ):
        self._factory = factory
        self._max_size = max(0, max_size)
        self._idle_seconds = idle_seconds
        self._idle: Deque[Tuple[Any, float]] = deque()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._max_size > 0

    def acquire(self) -> Tuple[Any, bool]:
        """
        返回 `(连接, 是否来自池)`。第二个值决定 release 时是还回去还是直接关。
        """
        if not self.enabled:
            return self._factory(), False

        deadline = time.monotonic() - self._idle_seconds
        while True:
            with self._lock:
                if not self._idle:
                    break
                conn, stamp = self._idle.popleft()
            if stamp < deadline or self._is_dead(conn):
                # 空闲太久的连接多半已经被服务端掐了，拿去用只会在下一条 SQL 上报错
                self._close(conn)
                continue
            return conn, True

        return self._factory(), True

    def release(self, conn: Any, pooled: bool, *, reusable: bool = True) -> None:
        """`reusable=False` 表示这条连接出过错，别再还给下一个人。"""
        if not pooled or not reusable or not self.enabled or self._is_dead(conn):
            self._close(conn)
            return
        with self._lock:
            if len(self._idle) >= self._max_size:
                overflow = conn
            else:
                self._idle.append((conn, time.monotonic()))
                return
        self._close(overflow)

    def close_all(self) -> None:
        with self._lock:
            idle, self._idle = list(self._idle), deque()
        for conn, _stamp in idle:
            self._close(conn)

    @staticmethod
    def _is_dead(conn: Any) -> bool:
        closed = getattr(conn, "closed", False)
        return bool(closed)

    @staticmethod
    def _close(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass
