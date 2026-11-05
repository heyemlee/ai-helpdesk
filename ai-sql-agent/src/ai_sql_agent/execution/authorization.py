"""
执行器入口的强制授权检查。

`ai_sql_agent.auth.enforcement` 定义**判什么**，这里定义**在哪判**：每个执行器的
`execute()` 第一件事就是过这道关。放在执行器而不是调用点，是因为调用点有四个
（首次生成、失败修复、历史重放、MCP 工具调用），实际复现过「每处
各写一次」会漏——漏的是修复那条。执行器只有一个入口，漏不掉。

放在执行器而不是 `MCPRouter` 也是有意的：复现越权的脚本就是绕开路由器直接
调 `SQLiteMCPExecutor.execute()` 的。真实链路不这么走，但「真实链路不这么走」
不是安全属性。
"""
from __future__ import annotations

from typing import Optional

from .objects import MCPExecutionRequest, MCPExecutionResult


def enforce(request: MCPExecutionRequest, dialect: str = "") -> Optional[MCPExecutionResult]:
    """
    过授权关卡。放行返回 None，拒绝返回一个 `success=False` 的执行结果。

    拒绝走「返回失败结果」而不是抛异常：越权对上层是一种**业务失败**
    （某一步查不了），不是服务故障；抛异常会让整条 pipeline 挂掉，
    把「这一步没权限」升级成「这次请求 500」。
    """
    from ai_sql_agent.auth.enforcement import authorize_sql

    decision = authorize_sql(
        database=request.database, sql=request.sql, dialect=dialect
    )
    if decision.allowed:
        return None

    return MCPExecutionResult(
        database=request.database,
        sql=request.sql,
        success=False,
        error=decision.audit_error,
    )
