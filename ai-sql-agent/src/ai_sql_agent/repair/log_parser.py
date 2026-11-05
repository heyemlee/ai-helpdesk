from __future__ import annotations

import re

from .objects import ExecutionError


class ExecutionLogParser:
    """
    执行日志解析器。

    把数据库执行器返回的错误日志解析为结构化错误，
    供子句定位与修复使用。当前覆盖 SQLite 错误族，
    错误模式可按方言扩展。
    """

    _PATTERNS = [
        ("no_such_table", re.compile(r"no such table:\s*([\w.]+)", re.I)),
        ("no_such_column", re.compile(r"no such column:\s*([\w.]+)", re.I)),
        ("ambiguous_column", re.compile(r"ambiguous column name:\s*([\w.]+)", re.I)),
        ("syntax_error", re.compile(r'near "([^"]+)":\s*syntax error', re.I)),
        ("no_such_function", re.compile(r"no such function:\s*([\w.]+)", re.I)),
        ("timeout", re.compile(r"超时|interrupt", re.I)),
        ("misuse_aggregate", re.compile(r"misuse of aggregate(?::|\s+function)?\s*([\w.]*)", re.I)),
    ]

    def parse(self, error_message: str) -> ExecutionError:
        """
        解析错误日志。
        """
        message = (error_message or "").strip()

        for error_type, pattern in self._PATTERNS:
            match = pattern.search(message)
            if match:
                detail = match.group(1).strip() if match.groups() else ""
                return ExecutionError(
                    error_type=error_type,
                    detail=detail,
                    raw_message=message,
                )

        return ExecutionError(
            error_type="unknown",
            detail="",
            raw_message=message,
        )
