from .dialects import POSTGRES, SQLITE, SqlDialect, get_dialect
from .objects import MCPExecutionRequest, MCPExecutionResult
from .postgres_executor import PostgresMCPExecutor
from .router import MCPRouter
from .sql_guard import check_readonly_sql
from .sqlite_executor import SQLiteMCPExecutor


def load_protocol_executor():
    """延迟导入 MCPProtocolExecutor，避免未安装 mcp SDK 时影响其他功能。"""
    from .mcp_protocol_executor import MCPProtocolExecutor

    return MCPProtocolExecutor


__all__ = [
    "MCPExecutionRequest",
    "MCPExecutionResult",
    "MCPRouter",
    "POSTGRES",
    "PostgresMCPExecutor",
    "SQLITE",
    "SQLiteMCPExecutor",
    "SqlDialect",
    "check_readonly_sql",
    "get_dialect",
    "load_protocol_executor",
]
