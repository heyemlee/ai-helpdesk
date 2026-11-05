from __future__ import annotations

import json
import sys
import threading
from typing import Optional

from .authorization import enforce
from .objects import MCPExecutionRequest, MCPExecutionResult


class MCPProtocolExecutor:
    """
    通过真实 MCP 协议（stdio）调用远端 SQL 执行工具的执行器。

    与 SQLiteMCPExecutor 同接口，可直接注册进 MCPRouter，
    使整条链路的 SQL 执行走标准 MCP tool call：
        Pipeline → MCPRouter → MCPProtocolExecutor
        → (MCP stdio) → ai_sql_agent.mcp_server.execute_sql → 数据库

    说明：Demo 实现每次调用拉起一个 stdio 会话，简单且无状态；
    生产部署应维护常驻 ClientSession 复用连接。
    """

    def __init__(
        self,
        database: str,
        server_command: Optional[list[str]] = None,
        working_dir: Optional[str] = None,
    ):
        self.database = database
        self.server_command = server_command or [
            sys.executable,
            "-m",
            "ai_sql_agent.mcp_server.server",
        ]
        # 包已安装（pip install -e .），子进程在哪个目录都能 import；
        # 不传就继承当前进程的工作目录，演示库的相对路径 runtime_data/ 才对得上。
        self.working_dir = working_dir

    dialect = "sqlite"
    """远端 ai_sql_agent.mcp_server 目前挂的是 SQLite 演示库；解析 SQL 时按它的方言。"""

    supports_params = False
    """
    `execute_sql` 这个 MCP 工具只收 `database` 与 `sql` 两个参数，传不了绑定参数。
    带参数的请求在这里**明确拒绝**，不把参数丢掉再执行——丢掉之后 SQL 里的
    占位符没有值，报出来的会是一条与原因无关的语法错误。
    """

    _spawn_guard = threading.BoundedSemaphore(8)
    """
    同时能拉起几个 stdio 子进程（控制连接与执行开销）。

    这里**没有做会话复用**——每次调用仍然起一个进程、建一次会话，
    只是把并发数封了顶。诚实说明为什么只做一半：复用要维护常驻
    `ClientSession` 的生命周期与失败重连，而这条路径今天只在
    `use_mcp_protocol=True` 的演示配置里走，生产链路用的是进程内执行器。
    封顶挡住的是真实风险（并发一上来把进程数打爆），会话复用省的是
    这条路径上今天没人付的开销。
    """

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """
        通过 MCP tool call 执行 SQL。

        本地先判一次权限：远端进程有它自己的授权作用域，但 contextvar 不跨进程，
        调用方这一侧的策略只能在这里落实。
        """
        denied = enforce(request, self.dialect)
        if denied is not None:
            return denied

        if request.params:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error="MCP 协议执行器不支持带绑定参数的查询。",
            )

        import anyio

        try:
            with self._spawn_guard:
                payload = anyio.run(self._call_tool_async, request)
        except Exception as exc:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error=f"MCP 调用失败：{exc}",
            )

        return MCPExecutionResult(
            database=payload.get("database", request.database),
            sql=payload.get("sql", request.sql),
            success=bool(payload.get("success")),
            columns=list(payload.get("columns") or []),
            rows=list(payload.get("rows") or []),
            row_count=int(payload.get("row_count") or 0),
            error=payload.get("error"),
            truncated=bool(payload.get("truncated")),
            row_limit=payload.get("row_limit"),
            as_of=str(payload.get("as_of") or ""),
            as_of_source=str(payload.get("as_of_source") or ""),
        )

    async def _call_tool_async(self, request: MCPExecutionRequest) -> dict:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=self.server_command[0],
            args=self.server_command[1:],
            cwd=self.working_dir,
        )

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_sql",
                    {"database": request.database, "sql": request.sql},
                )

        text_blocks = [
            block.text
            for block in result.content
            if getattr(block, "type", "") == "text"
        ]
        if not text_blocks:
            raise RuntimeError("MCP 工具未返回文本内容")

        return json.loads(text_blocks[0])
