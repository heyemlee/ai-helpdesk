from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ai_sql_agent.core.run_context import cancel_token, check_cancelled

from .authorization import enforce
from .data_time import QUERY_TIME, now_rfc3339
from .objects import MCPExecutionRequest, MCPExecutionResult
from .result_budget import RowTooLarge, row_bytes
from .sqlite_authorizer import NOT_AUTHORIZED, readonly_authorizer
from .sql_guard import check_readonly_sql, readonly_sqlite_uri


class SQLiteMCPExecutor:
    """
    SQLite MCP 执行器。

    这里模拟 MCP 路由到数据库 API 后的执行过程，并落实执行治理：
    - 只读约束（双防线）：应用层用 sql_guard 校验语句，连接层用
      `file:...?mode=ro` URI 打开只读连接，写操作在数据库层同样失败；
    - 行数上限：单条查询最多返回 max_rows 行，超出即截断并标记；
    - 字节上限：返回的值加起来最多 max_result_bytes，超出同样截断并标记；
      单个值 / 单行最长 max_value_bytes（`SQLITE_LIMIT_LENGTH`，超出时语句报错中止）；
    - 执行超时：通过 progress handler 中止超过 max_execution_seconds 的查询。
      progress handler 打断不了**单次函数调用**，所以上面的长度上限才是
      `hex(randomblob(150000000))` 这类写法的真正限制；
    - 数据库层授权器（readonly=True 时）：默认拒绝，只放行 SELECT 读数据，
      ATTACH、VACUUM INTO、PRAGMA、sqlite_* 内部表在引擎里就被拒。
    """

    dialect = "sqlite"

    supports_params = True
    """支持 `MCPExecutionRequest.params`（占位符 `?`）。"""

    # progress handler 回调间隔（虚拟机指令数），值越小超时检查越频繁
    _PROGRESS_HANDLER_INTERVAL = 10_000

    def __init__(
        self,
        database: str,
        db_path: str | Path,
        timeout: int = 30,
        readonly: bool = True,
        max_rows: int = 1000,
        max_execution_seconds: float = 10.0,
        as_of_source: str = QUERY_TIME,
        max_result_bytes: int = 8 * 1024 * 1024,
        max_value_bytes: int = 8 * 1024 * 1024,
    ):
        self.database = database
        self.db_path = str(db_path)
        self.timeout = timeout
        self.readonly = readonly
        self.max_rows = max_rows
        self.max_execution_seconds = max_execution_seconds
        self.max_result_bytes = max_result_bytes
        self.max_value_bytes = max_value_bytes
        self.as_of_source = as_of_source
        """默认 query_time（读主库）。挂只读副本或快照库时改成 snapshot。"""

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """
        执行 SQL。
        """
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
        deadline = time.monotonic() + max_seconds

        # 取消检查点：已经取消就不连库；执行期间被取消，进度回调下一次就中断语句
        check_cancelled()
        token = cancel_token()

        conn = None
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline or (token is not None and token.cancelled) else 0,
                self._PROGRESS_HANDLER_INTERVAL,
            )

            # 时点在真正执行 SQL 之前打：读到的就是这一刻的库状态
            as_of = now_rfc3339()
            if request.params:
                cursor = conn.execute(sql, tuple(request.params))
            else:
                cursor = conn.execute(sql)
            # 多取一行以区分“刚好取满”和“被截断”；同时按字节预算收行
            rows, truncated_by = self._fetch_bounded(cursor, max_rows)
            truncated = bool(truncated_by)
            # 按字节截的时候，「上限是多少行」没有固定答案：给实际返回的行数。
            # 契约 §4.6.3 要求 truncated 为真时 row_limit 有值，这样调用方
            # 至少能知道「这个结果就到这儿」，并且不会把它当成完整总量。
            row_limit = max_rows if truncated_by != "bytes" else len(rows)

            columns = list(rows[0].keys()) if rows else []
            result_rows: List[Dict[str, Any]] = [dict(row) for row in rows]

            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=True,
                columns=columns,
                rows=result_rows,
                row_count=len(result_rows),
                truncated=truncated,
                row_limit=row_limit,
                truncated_by=truncated_by,
                observed_at=now_rfc3339(),
                as_of=as_of,
                as_of_source=self.as_of_source,
            )

        except RowTooLarge as exc:
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=(
                    f"单行结果约 {exc.size} 字节，超过结果字节上限 {exc.limit}。"
                    "请只选择需要的列，或对长文本用 SUBSTR / LEFT 截取。"
                ),
            )

        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupt" in message.lower() and token is not None and token.cancelled:
                message = "查询已取消。"
            elif "interrupt" in message.lower():
                message = f"查询执行超时（超过 {max_seconds} 秒），已中止。"
            else:
                message = self._describe_error(exc)
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=message,
            )

        except Exception as exc:
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=self._describe_error(exc),
            )

        finally:
            if conn is not None:
                conn.close()

    def _fetch_bounded(
        self, cursor: sqlite3.Cursor, max_rows: int
    ) -> Tuple[List[sqlite3.Row], str]:
        """
        逐行取数，行数与字节任一先到上限就停。返回 `(行, 截断原因)`，
        原因是 "" / "rows" / "bytes"。

        逐行（`fetchone`）而不是 `fetchmany(max_rows + 1)`：后者会先把一千零一行
        全部取进内存，字节预算就只是事后的摆设。这样内存峰值是预算加一行。
        第一行就超预算时抛 `RowTooLarge`，没有可返回的部分结果。
        """
        rows: List[sqlite3.Row] = []
        used = 0
        while True:
            row = cursor.fetchone()
            if row is None:
                return rows, ""
            if len(rows) >= max_rows:
                # 多取的这一行只用来证明「后面还有」，不返回
                return rows, "rows"
            size = row_bytes(row)
            if used + size > self.max_result_bytes:
                if not rows:
                    raise RowTooLarge(size, self.max_result_bytes)
                return rows, "bytes"
            used += size
            rows.append(row)

    def _describe_error(self, exc: Exception) -> str:
        message = str(exc)
        lowered = message.lower()
        if "string or blob too big" in lowered:
            return (
                f"查询产生了超过 {self.max_value_bytes} 字节的单个值（数据库长度上限），"
                "已中止。请缩小范围，或对长文本 / 二进制列做截取。"
            )
        if NOT_AUTHORIZED in lowered or "is prohibited" in lowered:
            return (
                "数据库层拒绝了这条语句：它用到了只读查询不允许的操作或系统对象"
                "（ATTACH、PRAGMA、sqlite_* 内部表、文件读写函数等）。"
            )
        return message

    def _connect(self) -> sqlite3.Connection:
        """
        建立数据库连接。

        readonly=True 时用 `file:...?mode=ro` URI 打开，让只读约束真正落到
        数据库层——本地对应生产环境的只读账号。
        """
        if not self.readonly:
            conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        else:
            uri = readonly_sqlite_uri(self.db_path)
            if uri is None:
                raise sqlite3.OperationalError(
                    f"数据库文件不存在，只读模式不会自动创建：{self.db_path}"
                )
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)

        # 单个字符串 / BLOB / 一行的最大长度。progress handler 打断不了单次函数
        # 调用，`SELECT hex(randomblob(150000000))` 要靠这个上限才会在分配前就报错。
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.max_value_bytes)

        if self.readonly:
            # 只装在执行用户 SQL 的这条连接上。表结构读取、指纹、目录元数据各自
            # 开自己的连接（retrieval/sqlite_loader.py、schema_fingerprint.py），
            # 它们要读 sqlite_master 与 PRAGMA，不受这里影响。
            conn.set_authorizer(readonly_authorizer)
        return conn
