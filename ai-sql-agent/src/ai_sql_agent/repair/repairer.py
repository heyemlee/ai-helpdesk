from __future__ import annotations

import difflib
import logging
import re
from typing import List, Optional

from ai_sql_agent.core import get_logger, log_event
from ai_sql_agent.core.run_context import check_cancelled
from ai_sql_agent.execution.dialects import get_dialect

from .clause_localizer import SqlClauseLocalizer
from .log_parser import ExecutionLogParser
from .objects import ExecutionError, RepairAttempt, RepairOutcome, SqlClause

_logger = get_logger("repair")


class SqlRepairService:
    """
    Log-driven SQL 修复服务。

    修复回路：
    1. 解析执行日志，得到结构化错误；
    2. 定位疑似出错子句；
    3. 重生成出错子句（配置了模型时走模型，否则走启发式近名替换）；
    4. 重新执行，失败则继续，最多 max_attempts 次。
    """

    def __init__(
        self,
        coder_client=None,
        max_attempts: int = 2,
        log_parser: Optional[ExecutionLogParser] = None,
        localizer: Optional[SqlClauseLocalizer] = None,
    ):
        self.coder_client = coder_client
        self.max_attempts = max_attempts
        self.log_parser = log_parser or ExecutionLogParser()
        self.localizer = localizer or SqlClauseLocalizer()

    def repair(
        self,
        database: str,
        sql: str,
        error_message: str,
        local_schema,
        execute,
        sql_dialect: str = "sqlite",
    ) -> RepairOutcome:
        """
        执行修复回路。

        Args:
            database: 目标数据库。
            sql: 失败的 SQL。
            error_message: 执行错误日志。
            local_schema: 当前步骤局部 Schema（LocalSchema）。
            execute: 执行回调，签名 execute(database, sql) -> MCPExecutionResult。
            sql_dialect: 目标库方言，注入修复 Prompt，避免改出别的方言的语法。
        """
        attempts: List[RepairAttempt] = []
        current_sql = sql
        current_error = error_message

        for attempt_no in range(1, self.max_attempts + 1):
            # 取消检查点：每一轮修复之前（模型调用和 SQL 执行各自也会检查，这里写明是为了好读）
            check_cancelled()
            error = self.log_parser.parse(current_error)
            clause = self.localizer.localize(current_sql, error)

            repaired_sql, strategy = self._regenerate(
                sql=current_sql,
                error=error,
                clause=clause,
                local_schema=local_schema,
                sql_dialect=sql_dialect,
            )

            if not repaired_sql or repaired_sql.strip() == current_sql.strip():
                # 无法产生新的候选 SQL，提前终止
                log_event(
                    _logger,
                    logging.INFO,
                    "repair.exhausted",
                    database=database,
                    attempt=attempt_no,
                    error_type=error.error_type,
                    reason="未能生成新的候选 SQL",
                )
                break

            result = execute(database, repaired_sql)
            success = bool(getattr(result, "success", False))

            log_event(
                _logger,
                logging.INFO if success else logging.WARNING,
                "repair.attempt",
                database=database,
                attempt=attempt_no,
                error_type=error.error_type,
                failing_clause=clause.text if clause else "",
                strategy=strategy,
                success=success,
                repaired_sql=repaired_sql,
                error_after_repair=getattr(result, "error", None),
            )

            attempts.append(
                RepairAttempt(
                    attempt_no=attempt_no,
                    error_type=error.error_type,
                    failing_clause=clause.text if clause else "",
                    original_sql=current_sql,
                    repaired_sql=repaired_sql,
                    strategy=strategy,
                    success=success,
                    error_after_repair=getattr(result, "error", None),
                )
            )

            if success:
                return RepairOutcome(
                    repaired=True,
                    final_sql=repaired_sql,
                    final_result=result,
                    attempts=attempts,
                )

            current_sql = repaired_sql
            current_error = getattr(result, "error", "") or ""

        return RepairOutcome(
            repaired=False,
            final_sql=current_sql,
            final_result=None,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # 重生成
    # ------------------------------------------------------------------

    def _regenerate(
        self,
        sql: str,
        error: ExecutionError,
        clause: Optional[SqlClause],
        local_schema,
        sql_dialect: str = "sqlite",
    ) -> tuple[str, str]:
        """
        重生成 SQL。返回 (repaired_sql, strategy)。
        """
        if self._model_available():
            repaired = self._model_repair(
                sql, error, clause, local_schema, sql_dialect
            )
            if repaired:
                return repaired, "model"

        repaired = self._heuristic_repair(sql, error, clause, local_schema)
        return repaired, "heuristic"

    def _model_available(self) -> bool:
        if self.coder_client is None:
            return False
        # client 构造时抄下的 Key 不一定是现在的：页面上刚配好模型连接、还没有调用过的话，
        # config 里还是空的，修复会一直走规则版。先让它按当前的模型连接刷新一遍。
        available = getattr(self.coder_client, "model_available", None)
        if callable(available):
            return bool(available())
        config = getattr(self.coder_client, "config", None)
        return bool(getattr(config, "api_key", ""))

    def _model_repair(
        self,
        sql: str,
        error: ExecutionError,
        clause: Optional[SqlClause],
        local_schema,
        sql_dialect: str = "sqlite",
    ) -> str:
        """
        模型修复：只重写出错子句，保持其余部分不变。
        """
        schema_context = (
            local_schema.to_prompt_context()
            if hasattr(local_schema, "to_prompt_context")
            else str(local_schema)
        )

        clause_section = ""
        if clause is not None:
            clause_section = f"""
# 错误定位
疑似出错子句（{clause.name}）：
{clause.text}
"""

        prompt = f"""你是SQL修复助手。下面这条SQL执行失败，请修复。

要求：
1. 只修改出错的子句，其余部分保持原样。
2. 只能使用Schema中存在的表和字段。
3. 只输出修复后的完整SQL，不输出解释。
4. 不要输出Markdown代码块。
5. 修复后的SQL需要符合下方「方言约束」。

# 方言约束
{get_dialect(sql_dialect).to_prompt_rules()}

# 原始SQL
{sql}

# 执行错误日志
{error.raw_message}

# 错误类型
{error.error_type}（相关对象：{error.detail or '未知'}）
{clause_section}
# Schema信息
{schema_context}
"""
        raw_output = self.coder_client.generate_sql(prompt)
        return self.coder_client.clean_sql(raw_output)

    def _heuristic_repair(
        self,
        sql: str,
        error: ExecutionError,
        clause: Optional[SqlClause],
        local_schema,
    ) -> str:
        """
        启发式修复：在出错子句内做最近名替换。

        没有 API Key 时的离线兜底，覆盖表名/字段名拼写类错误。
        """
        if not error.detail:
            return ""

        wrong_name = error.detail.split(".")[-1]

        if error.error_type == "no_such_table":
            candidates = self._schema_table_names(local_schema)
        elif error.error_type in ("no_such_column", "ambiguous_column"):
            candidates = self._schema_column_names(local_schema)
        else:
            return ""

        matches = difflib.get_close_matches(wrong_name, candidates, n=1, cutoff=0.4)
        if not matches:
            return ""

        replacement = matches[0]

        if error.error_type == "no_such_table":
            # 表名可能出现在 FROM/JOIN 和字段前缀里，全局替换
            return re.sub(rf"\b{re.escape(wrong_name)}\b", replacement, sql)

        if clause is None:
            return re.sub(rf"\b{re.escape(wrong_name)}\b", replacement, sql)

        # 字段名错误只在出错子句范围内替换，避免误伤其他子句
        repaired_clause = re.sub(
            rf"\b{re.escape(wrong_name)}\b", replacement, clause.text
        )
        return sql[: clause.start] + repaired_clause + sql[clause.end :]

    @staticmethod
    def _schema_table_names(local_schema) -> List[str]:
        tables = getattr(local_schema, "tables", {}) or {}
        return list(tables.keys())

    @staticmethod
    def _schema_column_names(local_schema) -> List[str]:
        tables = getattr(local_schema, "tables", {}) or {}
        names: List[str] = []
        for table in tables.values():
            for column in getattr(table, "columns", []) or []:
                names.append(column.column_name)
        return names
