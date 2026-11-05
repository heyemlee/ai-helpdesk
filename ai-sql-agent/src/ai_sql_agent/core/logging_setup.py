from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

ROOT_LOGGER_NAME = "ai_sql"

_FIELDS_ATTR = "ai_sql_fields"
_configured = False
_configure_lock = threading.Lock()


def get_logger(name: str) -> logging.Logger:
    """
    获取 ai_sql 命名空间下的 Logger。

    name 传模块名即可，例如 get_logger("pipeline") -> "ai_sql.pipeline"。
    """
    if name.startswith(ROOT_LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


class TraceIdFilter(logging.Filter):
    """把当前请求的 trace_id 注入每条日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        from .trace import get_trace_id

        if not hasattr(record, "trace_id"):
            record.trace_id = get_trace_id()
        return True


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


class KeyValueFormatter(logging.Formatter):
    """人读友好的 key=value 结构化格式，本地开发默认使用。"""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<5s} "
            f"[{getattr(record, 'trace_id', '-') or '-'}] "
            f"{record.name} {record.getMessage()}"
        )
        fields: Dict[str, Any] = getattr(record, _FIELDS_ATTR, {}) or {}
        if fields:
            rendered = " ".join(
                f"{key}={_format_value(value)}" for key, value in fields.items()
            )
            base = f"{base} {rendered}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


class JsonFormatter(logging.Formatter):
    """行 JSON 格式，供采集到 ELK / Loki 等日志系统。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", "") or "",
            "event": record.getMessage(),
        }
        payload.update(getattr(record, _FIELDS_ATTR, {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: Optional[str] = None,
    log_format: Optional[str] = None,
    *,
    force: bool = False,
) -> None:
    """
    初始化 ai_sql 日志。

    幂等：重复调用不会叠加 Handler。默认从 Settings 读取级别与格式
    （AI_SQL_LOG_LEVEL / AI_SQL_LOG_FORMAT）。
    """
    global _configured

    if _configured and not force:
        return

    with _configure_lock:
        if _configured and not force:
            return
        _apply_logging_config(level, log_format)
        _configured = True


def _apply_logging_config(
    level: Optional[str],
    log_format: Optional[str],
) -> None:
    from .settings import get_settings

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    resolved_format = (log_format or settings.log_format).lower()

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if resolved_format == "json" else KeyValueFormatter()
    )
    handler.addFilter(TraceIdFilter())

    logger.addHandler(handler)
    logger.setLevel(resolved_level)
    # 业务日志自成一棵树，不污染宿主应用的 root logger
    logger.propagate = False


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    """
    记录一条结构化事件。

    event 是稳定的事件名（如 "stage.retrieval"、"model.call"），
    可变信息放 fields，方便后续按字段检索而不是正则扒日志。
    """
    configure_logging()
    logger.log(
        level,
        event,
        exc_info=exc_info,
        extra={_FIELDS_ATTR: fields},
    )
