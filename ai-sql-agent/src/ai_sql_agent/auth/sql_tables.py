r"""
表名提取的历史入口。

实现已经换成 AST（`ai_sql_agent.auth.sql_analysis`）——原来的 `\b(?:from|join)\s+`
正则漏掉逗号连接、也没有列级概念，照着这两点各复现过一条越权。

**这个函数不再是授权判据**，授权走 `ai_sql_agent.auth.enforcement.authorize_sql`。
保留它是因为还有两处非安全用途在用：跨库静态校验与审计日志的表清单。
"""
from __future__ import annotations

from .sql_analysis import extract_referenced_tables

__all__ = ["extract_referenced_tables"]
