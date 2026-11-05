from __future__ import annotations

import json
import os
from pathlib import Path

from ai_sql_agent.auth import ViewCatalog
from ai_sql_agent.core import get_settings
from mcp.server import MCPServer

from ai_sql_agent.pipeline.demo_data import (
    create_erp_demo_database,
    create_mes_demo_database,
    create_pm_demo_database,
    create_wms_demo_database,
)
from ai_sql_agent.execution import MCPExecutionRequest, MCPRouter, SQLiteMCPExecutor

server = MCPServer(
    name="ai-sql-agent",
    instructions="AI SQL Agent 只读 SQL 执行服务：先用 list_databases 查看可用数据库，"
    "再用 execute_sql 执行 SELECT / WITH 查询。",
)

# 执行治理参数是固定值，与 PipelineConfig 的默认值相同。这个进程不读 AI_SQL_MAX_ROWS /
# AI_SQL_MAX_EXECUTION_SECONDS：那两个配置管的是 API 进程里的执行器。
_MAX_ROWS = 1000
_MAX_EXECUTION_SECONDS = 10.0
_MAX_RESULT_BYTES = 8 * 1024 * 1024
_MAX_VALUE_BYTES = 8 * 1024 * 1024

_DATABASES = {
    "erp_db": ("runtime_data/erp_demo.db", create_erp_demo_database),
    "pm_db": ("runtime_data/pm_demo.db", create_pm_demo_database),
    "mes_db": ("runtime_data/mes_demo.db", create_mes_demo_database),
    "wms_db": ("runtime_data/wms_demo.db", create_wms_demo_database),
}


def _build_router() -> MCPRouter:
    router = MCPRouter()
    for name, (db_path, creator) in _DATABASES.items():
        path = Path(db_path)
        if not path.exists():
            creator(path)
        router.register_executor(
            database=name,
            executor=SQLiteMCPExecutor(
                database=name,
                db_path=path,
                readonly=True,
                max_rows=_MAX_ROWS,
                max_execution_seconds=_MAX_EXECUTION_SECONDS,
                max_result_bytes=_MAX_RESULT_BYTES,
                max_value_bytes=_MAX_VALUE_BYTES,
            ),
        )
    return router


_router = _build_router()

# 视图会遮住它读的基表，授权关卡自己推不出哪些名字是视图（见 ai_sql_agent.auth.views）。
# 管道靠 schema 轮询刷新目录；这个 stdio 进程没有轮询，所以启动时全部扫一次，
# 之后**每次调用前重扫被查的那个库**（只是读一次 sqlite_master，比查询本身便宜得多）。
# 失败语义和管道一致：扫描失败登记为失败，受限策略一律被拒，而不是放行。
_views = ViewCatalog()


def _refresh_views(database: str) -> None:
    entry = _DATABASES.get(database)
    if entry is not None:
        _views.refresh(database, "sqlite", db_path=str(entry[0]))


for _name in _DATABASES:
    _refresh_views(_name)


def list_databases() -> str:
    """列出可查询的数据库。"""
    return json.dumps(
        {"databases": sorted(_DATABASES.keys())},
        ensure_ascii=False,
    )


def _server_policy():
    """
    这个 stdio server 自己的数据权限。

    **它没有调用方身份**：stdio 进程不带 API Key，也就没有角色可解析。所以
    权限只能来自它自己的配置——`AI_SQL_MCP_POLICY_FILE` 指向一个
    `{"allow_tables": [...], "deny_tables": [...], "deny_columns": [...]}`。

    没配的时候给全放开，并且**生产模式下拒绝启动**：一个不认识调用方、又拿着
    全库只读权限的进程，不该出现在生产里。
    """
    from ai_sql_agent.auth import DataPolicy

    path = os.environ.get("AI_SQL_MCP_POLICY_FILE", "").strip()
    if path:
        return DataPolicy.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    if get_settings().app_env == "production":
        raise RuntimeError(
            "生产模式下 ai_sql_agent.mcp_server 必须配置 AI_SQL_MCP_POLICY_FILE："
            "stdio 进程没有调用方身份，不配策略等于给全库只读权限。"
        )
    return DataPolicy.full_access()


def execute_sql(database: str, sql: str) -> str:
    """
    在指定数据库上执行只读 SQL 查询。

    受数据权限策略、只读账号约束（仅 SELECT / WITH）、单查询行数上限与执行
    超时保护。返回 JSON：{success, columns, rows, row_count, truncated, error}。
    """
    from ai_sql_agent.auth import authorization_scope

    # 授权作用域靠 contextvar 传，**不跨进程**——调用方那一侧的策略到不了这里，
    # 所以这个进程必须自己绑一份。不绑的话执行器会一律拒绝（这是有意的默认值）。
    _refresh_views(database)
    with authorization_scope(_server_policy(), caller="mcp-stdio", views=_views):
        result = _router.execute(
            MCPExecutionRequest(database=database, sql=sql)
        )
    return json.dumps(result.to_dict(), ensure_ascii=False, default=str)


server.add_tool(list_databases, name="list_databases")
server.add_tool(execute_sql, name="execute_sql")


if __name__ == "__main__":
    server.run("stdio")
