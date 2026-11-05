from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ai_sql_agent.core import get_settings
from ai_sql_agent.core.run_context import cancel_token, check_cancelled

from .authorization import enforce
from .data_time import QUERY_TIME, now_rfc3339
from .dialects import POSTGRES
from .pool import BoundedConnectionPool
from .objects import MCPExecutionRequest, MCPExecutionResult
from .result_budget import RowTooLarge, trim_to_budget
from .sql_guard import check_readonly_sql


def _current_user_id() -> str:
    """当前授权作用域里的最终用户。没有作用域时返回空串（fail closed）。"""
    from ai_sql_agent.auth.enforcement import current_scope

    scope = current_scope()
    return scope.user_id if scope is not None else ""


def _canceller(conn):
    """
    向服务端发取消请求的回调（取消令牌在别的线程里触发它）。

    psycopg 3.2 起推荐 `cancel_safe()`（不阻塞事件循环、走新的取消协议），老版本只有 `cancel()`。
    服务端收到后以 `canceling statement due to user request` 中断语句，执行线程在 except 里收尾。
    """
    method = getattr(conn, "cancel_safe", None) or getattr(conn, "cancel", None)
    return method if callable(method) else (lambda: None)


class PostgresMCPExecutor:
    """
    PostgreSQL 执行器。

    与 SQLiteMCPExecutor 同接口，执行治理对齐同一套规则，但每条都落到
    PostgreSQL 自己的机制上：

    - 只读（双防线）：应用层 sql_guard 校验语句 + 连接层
      `SET TRANSACTION READ ONLY`（生产上再叠加只读账号）；
    - 执行超时：会话级 `statement_timeout`，由数据库负责中断，
      不依赖应用轮询；
    - 行数上限：服务端游标按需取数，多取一行用于判断是否被截断，
      避免把整张表拉进内存；
    - 字节上限：取回的值加起来最多 max_result_bytes，超出截断并标记。
      **这一条只管「返回多少」**：驱动一次取回的这批行已经在内存里，单个值再大
      也会先收下来（PostgreSQL 没有「单值最大长度」这样的会话参数）。要在源头
      挡住，用 SQL 里的 `left()` / `substr()`，或让只读账号看不到那些列。
    """

    dialect = POSTGRES.name

    supports_params = True
    """支持 `MCPExecutionRequest.params`（占位符 `%s`）。"""

    def __init__(
        self,
        database: str,
        dsn: str,
        connect_timeout: int = 10,
        readonly: bool = True,
        max_rows: int = 1000,
        max_execution_seconds: float = 10.0,
        as_of_source: str = QUERY_TIME,
        pool_max_connections: Optional[int] = None,
        pool_idle_seconds: Optional[float] = None,
        max_result_bytes: int = 8 * 1024 * 1024,
    ):
        self.database = database
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.readonly = readonly
        self.max_rows = max_rows
        self.max_execution_seconds = max_execution_seconds
        self.max_result_bytes = max_result_bytes
        self.as_of_source = as_of_source
        """默认 query_time（读主库）。DSN 指向只读副本或数仓时改成 snapshot。"""

        settings = get_settings()
        self._pool = BoundedConnectionPool(
            factory=self._connect,
            max_size=(
                settings.db_pool_max_connections
                if pool_max_connections is None
                else pool_max_connections
            ),
            idle_seconds=(
                settings.db_pool_idle_seconds
                if pool_idle_seconds is None
                else pool_idle_seconds
            ),
        )

    def _connect(self):
        import psycopg

        return psycopg.connect(
            self.dsn, connect_timeout=self.connect_timeout, autocommit=False
        )

    def close(self) -> None:
        """释放池里的空闲连接。进程退出或重建执行器时调用。"""
        self._pool.close_all()

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """执行 SQL。"""
        denied = enforce(request, self.dialect)
        if denied is not None:
            return denied

        if request.database != self.database:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error=f"数据库路由错误：当前执行器只处理 {self.database}",
            )

        sql = request.sql.strip()

        if self.readonly:
            guard = check_readonly_sql(sql, dialect=self.dialect)
            if not guard.allowed:
                return MCPExecutionResult(
                    database=request.database,
                    sql=sql,
                    success=False,
                    error=guard.reason,
                )

        # 请求可以把行数与时长上限调小（预览一页只要几十行），调不大
        max_rows = request.row_cap(self.max_rows)
        max_seconds = request.time_cap(self.max_execution_seconds)

        # 取消检查点：已经取消就不借连接、不发语句
        check_cancelled()
        token = cancel_token()
        stop_watching = lambda: None  # noqa: E731

        conn = None
        pooled = False
        reusable = True
        try:
            conn, pooled = self._pool.acquire()
            if token is not None:
                # 执行期间被取消：让服务端中断这条语句，不等它自己跑完
                stop_watching = token.on_cancel(_canceller(conn))
            timeout_ms = int(max_seconds * 1000)
            conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            if self.readonly:
                conn.execute("SET TRANSACTION READ ONLY")

            # 把最终用户身份放进事务，让行级安全（RLS）在**经由 agent 发起的
            # 查询**上真的生效——不然 RLS 只在有人手工连库时管用。
            #
            # SET LOCAL 是事务级的，随 rollback 一起还原，所以连接池复用不会串身份。
            # 没有授权作用域时设成空串：RLS 的
            # `USING (owner_id = current_setting('app.user_id', true))` 对空串
            # 不匹配任何行——fail closed，与执行器那道关卡方向一致。
            conn.execute(
                "SELECT set_config('app.user_id', %s, true)", (_current_user_id(),)
            )

            started = time.perf_counter()
            # 时点在真正执行 SQL 之前打：读到的就是这一刻的库状态
            as_of = now_rfc3339()
            with conn.cursor(name=f"ai_sql_{int(started * 1000)}") as cursor:
                cursor.itersize = min(max_rows, 1000)
                # 不带参数时不传第二个实参：psycopg 只在给了参数时才解释 `%`，
                # 模型生成的 SQL 里的 `LIKE '%x%'` 才不会被当成占位符。
                if request.params:
                    cursor.execute(sql, tuple(request.params))
                else:
                    cursor.execute(sql)
                # 多取一行以区分「刚好取满」和「被截断」
                raw_rows = cursor.fetchmany(max_rows + 1)
                columns = [desc.name for desc in (cursor.description or [])]

            truncated = len(raw_rows) > max_rows
            raw_rows = raw_rows[:max_rows]
            truncated_by = "rows" if truncated else ""
            row_limit = max_rows

            # 字节预算：第一行就放不下时抛 RowTooLarge（见下面的 except），
            # 否则取到放不下的那一行为止，标记截断
            raw_rows, over_budget = trim_to_budget(raw_rows, self.max_result_bytes)
            if over_budget:
                truncated, truncated_by, row_limit = True, "bytes", len(raw_rows)

            rows: List[Dict[str, Any]] = [
                dict(zip(columns, row)) for row in raw_rows
            ]

            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                row_limit=row_limit,
                truncated_by=truncated_by,
                as_of=as_of,
                as_of_source=self.as_of_source,
            )

        except RowTooLarge as exc:
            # 这是结果太大，不是连接出了问题：连接照常还回池
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=(
                    f"单行结果约 {exc.size} 字节，超过结果字节上限 {exc.limit}。"
                    "请只选择需要的列，或对长文本用 substr / left 截取。"
                ),
            )

        except Exception as exc:  # psycopg 未安装或执行失败都走这里
            # 出过错的连接不还回池：它可能停在一个半死的事务里，
            # 下一个人拿到它会收到一条与自己无关的错误，最难排查的那种。
            reusable = False
            cancelled = token is not None and token.cancelled
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error="查询已取消。" if cancelled else self._describe_error(exc, max_seconds),
            )

        finally:
            stop_watching()
            if conn is not None:
                # 事务级设置（statement_timeout / READ ONLY）随 rollback 一起还原，
                # 所以归还的连接是干净的，下一次执行会重新设一遍。
                try:
                    conn.rollback()
                except Exception:
                    reusable = False
                self._pool.release(conn, pooled, reusable=reusable)

    def _describe_error(self, exc: Exception, max_seconds: Optional[float] = None) -> str:
        message = str(exc).strip()
        if "statement timeout" in message.lower() or "canceling statement" in message.lower():
            limit = self.max_execution_seconds if max_seconds is None else max_seconds
            return f"查询执行超时（超过 {limit} 秒），已中止。"
        if "read-only" in message.lower():
            return "只读事务中不允许写操作。"
        return message
