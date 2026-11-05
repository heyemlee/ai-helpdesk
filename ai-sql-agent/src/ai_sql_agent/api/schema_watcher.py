from __future__ import annotations

import logging
import threading
from typing import Optional

from ai_sql_agent.core import get_logger, log_event, new_trace_id, use_trace_id

_logger = get_logger("schema_watcher")


class SchemaWatcher:
    """
    Schema 变更监听。

    业务库加表加字段后不必重启服务：后台线程按固定间隔比对 Schema 指纹
    （只读系统表，成本极低），发现变化才重建三层检索索引并原子替换。
    配了 pgvector 时，没变化的命名空间会命中向量指纹缓存，
    真正重新 Embedding 的只有变动的那部分。

    interval_seconds <= 0 时不启动（本地开发与单测默认如此）。
    """

    def __init__(self, pipeline, interval_seconds: float):
        self.pipeline = pipeline
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return

        self._thread = threading.Thread(
            target=self._loop, name="ai-sql-schema-watcher", daemon=True
        )
        self._thread.start()
        log_event(
            _logger,
            logging.INFO,
            "schema_watcher.started",
            interval_seconds=self.interval_seconds,
        )

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if self._thread is not None and wait:
            self._thread.join(timeout=5)
        self._thread = None

    def check_once(self, reason: str = "watcher") -> list:
        """比对一次；有变化才重建。返回发生变化的库名。"""
        changed = self.pipeline.detect_schema_changes()
        if not changed:
            return []

        log_event(
            _logger, logging.INFO, "schema_watcher.change_detected", changed=changed
        )
        return self.pipeline.reload_schema(reason=reason)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            with use_trace_id(f"schema-watch-{new_trace_id()[:8]}"):
                try:
                    self.check_once()
                except Exception as exc:
                    # 后台线程不能因为一次失败就退出，下个周期继续
                    log_event(
                        _logger,
                        logging.ERROR,
                        "schema_watcher.check_failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
