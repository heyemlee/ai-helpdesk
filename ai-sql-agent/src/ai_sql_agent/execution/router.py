from __future__ import annotations

import time
from typing import Dict

from ai_sql_agent.core.run_context import cancel_token
from ai_sql_agent.core.run_context import emit as emit_run_event

from .objects import MCPExecutionRequest, MCPExecutionResult


class MCPRouter:
    """
    MCP 路由器：按 execution_request.database 找到对应执行器并转发。

    **授权不在这里判，在执行器入口判**（`ai_sql_agent.execution.authorization.enforce`）。
    路由器只是四条执行路径中最常走的那一条，绕开它直接调执行器一样能打到库——
    复现越权的脚本就是这么干的。关卡要放在绕不开的地方。
    """

    def __init__(self):
        self.executors: Dict[str, object] = {}

    def register_executor(self, database: str, executor: object) -> None:
        """
        注册数据库执行器。
        """
        self.executors[database] = executor

    def execute(self, request: MCPExecutionRequest | dict) -> MCPExecutionResult:
        """
        路由并执行请求。授权由执行器自己把关。
        """
        if isinstance(request, dict):
            request = MCPExecutionRequest(
                database=request["database"],
                sql=request["sql"],
            )

        executor = self.executors.get(request.database)

        if executor is None:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error=f"未找到数据库 {request.database} 对应的 MCP 执行器。",
            )

        started = time.perf_counter()
        result = executor.execute(request)
        # 运行日志：每次执行一条。用途（首次执行 / 修复 / 降级重放）看事件的 stage 字段。
        token = cancel_token()
        if result.success:
            status = "ok"
        elif token is not None and token.cancelled:
            status = "cancelled"
        else:
            status = "error"
        emit_run_event(
            "tool.sql",
            database=request.database,
            status=status,
            row_count=result.row_count,
            truncated=result.truncated,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            sql=request.sql,
            error=result.error,
        )
        return result
