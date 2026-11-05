from __future__ import annotations

import logging
import math
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ai_sql_agent.core import get_logger, log_event

from .objects import MemoryMessage, MessageRole, ShortTermContext
from .storage import SQLiteMemoryStore
from .summarizer import ExtractiveMemorySummarizer, MemorySummarizer

_logger = get_logger("memory.summary")


@dataclass
class ShortTermMemoryConfig:
    # 12 条消息 = 最近 6 轮用户/助手对话
    max_window_messages: int = 12
    max_window_tokens: int = 4000
    summary_batch_size: int = 50
    async_summary: bool = True

    def __post_init__(self) -> None:
        if self.max_window_messages < 1:
            raise ValueError("max_window_messages 必须大于 0")
        if self.max_window_tokens < 1:
            raise ValueError("max_window_tokens 必须大于 0")
        if self.summary_batch_size < 1:
            raise ValueError("summary_batch_size 必须大于 0")


class ShortTermMemoryService:
    """滑动窗口 + 非阻塞增量摘要。"""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        summarizer: Optional[MemorySummarizer] = None,
        config: Optional[ShortTermMemoryConfig] = None,
    ):
        self.store = store
        self.summarizer = summarizer or ExtractiveMemorySummarizer()
        self.config = config or ShortTermMemoryConfig()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-summary")
        self._lock = threading.RLock()
        self._jobs: Dict[Tuple[str, str], Future[None]] = {}
        self._closed = False

    def add_message(
        self,
        *,
        user_id: str,
        session_id: str,
        role: MessageRole | str,
        content: str,
        message_type: str = "text",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryMessage:
        self._validate_scope(user_id, session_id)
        resolved_role = role if isinstance(role, MessageRole) else MessageRole(role)
        message = self.store.add_message(
            user_id=user_id,
            session_id=session_id,
            role=resolved_role,
            content=content,
            message_type=message_type,
            payload=payload,
            metadata=metadata,
            token_count=self.estimate_tokens(content, payload),
        )
        self._schedule_if_needed(user_id=user_id, session_id=session_id)
        return message

    def get_context(self, *, user_id: str, session_id: str) -> ShortTermContext:
        self._validate_scope(user_id, session_id)
        messages = self.store.list_messages(user_id=user_id, session_id=session_id)
        window = self._select_window(messages)
        self._schedule_if_needed(user_id=user_id, session_id=session_id)
        key = (user_id, session_id)
        with self._lock:
            future = self._jobs.get(key)
            pending = future is not None and not future.done()
        # 在检查后台任务状态后读取，可拿到刚刚完成的最新摘要版本。
        summary = self.store.get_latest_summary(user_id=user_id, session_id=session_id)
        return ShortTermContext(
            session_id=session_id,
            user_id=user_id,
            messages=window,
            summary=summary,
            summary_pending=pending,
        )

    def wait_for_summary(
        self, *, user_id: str, session_id: str, timeout: Optional[float] = None
    ) -> None:
        key = (user_id, session_id)
        while True:
            with self._lock:
                future = self._jobs.get(key)
            if future is None:
                return
            future.result(timeout=timeout)

    def close(self, wait: bool = True) -> None:
        if wait:
            while True:
                with self._lock:
                    futures = list(self._jobs.values())
                if not futures:
                    break
                for future in futures:
                    future.result()
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)

    @staticmethod
    def estimate_tokens(content: str, payload: Optional[Dict[str, Any]] = None) -> int:
        chinese = len(re.findall(r"[\u4e00-\u9fff]", content))
        remaining = len(re.sub(r"[\u4e00-\u9fff\s]", "", content))
        payload_chars = len(str(payload)) if payload else 0
        return max(1, chinese + math.ceil((remaining + payload_chars) / 4))

    def _select_window(self, messages: List[MemoryMessage]) -> List[MemoryMessage]:
        selected: List[MemoryMessage] = []
        token_total = 0
        for message in reversed(messages):
            if len(selected) >= self.config.max_window_messages:
                break
            if selected and token_total + message.token_count > self.config.max_window_tokens:
                break
            selected.append(message)
            token_total += message.token_count
        return list(reversed(selected))

    def _get_unsummarized_overflow(
        self, *, user_id: str, session_id: str
    ) -> tuple[str, List[MemoryMessage]]:
        messages = self.store.list_messages(user_id=user_id, session_id=session_id)
        window = self._select_window(messages)
        window_start = window[0].id if window else float("inf")
        latest = self.store.get_latest_summary(user_id=user_id, session_id=session_id)
        through_id = latest.through_message_id if latest else 0
        overflow = [
            message
            for message in messages
            if through_id < message.id < window_start
        ][: self.config.summary_batch_size]
        return (latest.content if latest else "", overflow)

    def _schedule_if_needed(self, *, user_id: str, session_id: str) -> None:
        key = (user_id, session_id)
        with self._lock:
            if self._closed:
                return
            current = self._jobs.get(key)
            if current is not None and not current.done():
                return
            previous, overflow = self._get_unsummarized_overflow(
                user_id=user_id, session_id=session_id
            )
            if not overflow:
                self._jobs.pop(key, None)
                return
            if not self.config.async_summary:
                self._run_summary(user_id, session_id, previous, overflow)
                self._schedule_if_needed(user_id=user_id, session_id=session_id)
                return
            future = self._executor.submit(
                self._run_summary, user_id, session_id, previous, overflow
            )
            self._jobs[key] = future
            future.add_done_callback(lambda done, job_key=key: self._on_job_done(job_key, done))

    def _run_summary(
        self,
        user_id: str,
        session_id: str,
        previous: str,
        messages: List[MemoryMessage],
    ) -> None:
        content = self.summarizer.summarize_conversation(previous, messages)
        self.store.save_summary(
            user_id=user_id,
            session_id=session_id,
            content=content,
            through_message_id=messages[-1].id,
        )

    def _on_job_done(self, key: Tuple[str, str], future: Future[None]) -> None:
        with self._lock:
            if self._jobs.get(key) is future:
                self._jobs.pop(key, None)

        if future.cancelled():
            log_event(
                _logger,
                logging.WARNING,
                "memory.summary.cancelled",
                user_id=key[0],
                session_id=key[1],
            )
            return

        error = future.exception()
        if error is not None:
            # 后台摘要失败过去是静默的：摘要停更但没有任何信号，
            # 这里显式记一条，配合告警可以及时发现。
            log_event(
                _logger,
                logging.ERROR,
                "memory.summary.failed",
                user_id=key[0],
                session_id=key[1],
                error_type=type(error).__name__,
                error=str(error),
            )
            return

        log_event(
            _logger,
            logging.DEBUG,
            "memory.summary.done",
            user_id=key[0],
            session_id=key[1],
        )
        self._schedule_if_needed(user_id=key[0], session_id=key[1])

    @staticmethod
    def _validate_scope(user_id: str, session_id: str) -> None:
        if not user_id.strip():
            raise ValueError("user_id 不能为空")
        if not session_id.strip():
            raise ValueError("session_id 不能为空")
