from __future__ import annotations

import re
from typing import List, Optional

from .objects import ExecutionError, SqlClause


class SqlClauseLocalizer:
    """
    SQL 子句定位器。

    把 SQL 按顶层关键字切分为带位置的子句序列，
    再根据结构化错误定位疑似出错子句。

    说明：切分基于顶层关键字扫描，深度嵌套子查询会被并入
    外层子句处理，这对“定位到需要重生成的片段”已经足够。
    """

    _CLAUSE_KEYWORDS = re.compile(
        r"\b("
        r"SELECT|FROM|"
        r"(?:LEFT\s+|RIGHT\s+|INNER\s+|OUTER\s+|CROSS\s+)?JOIN|"
        r"WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|WITH"
        r")\b",
        re.I,
    )

    def split(self, sql: str) -> List[SqlClause]:
        """
        切分 SQL 为子句序列。
        """
        matches = list(self._CLAUSE_KEYWORDS.finditer(sql))

        if not matches:
            return [SqlClause(name="FULL_QUERY", text=sql, start=0, end=len(sql))]

        clauses: List[SqlClause] = []

        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(sql)
            name = re.sub(r"\s+", " ", match.group(1).upper())
            # text 保留原始切片（不 strip），保证 start/end 与 text 严格对应，
            # 子句替换后可以按位置拼回原 SQL。
            clauses.append(
                SqlClause(
                    name=name,
                    text=sql[start:end],
                    start=start,
                    end=end,
                )
            )

        return clauses

    def localize(self, sql: str, error: ExecutionError) -> Optional[SqlClause]:
        """
        根据结构化错误定位疑似出错子句。

        返回 None 表示无法定位到具体子句（如超时），
        此时应整条重生成而不是局部修复。
        """
        clauses = self.split(sql)

        if error.error_type in ("timeout", "unknown"):
            return None

        detail = error.detail.split(".")[-1] if error.detail else ""

        if error.error_type == "no_such_table":
            # 表名错误优先定位 FROM / JOIN 子句
            for clause in clauses:
                if clause.name in ("FROM", "JOIN") and self._contains_token(
                    clause.text, detail
                ):
                    return clause

        if detail:
            for clause in clauses:
                if self._contains_token(clause.text, detail):
                    return clause

        # 语法错误定位不到 Token 时，返回第一个子句附近作为兜底
        if clauses:
            return clauses[0]

        return None

    @staticmethod
    def _contains_token(text: str, token: str) -> bool:
        if not token:
            return False
        return re.search(rf"\b{re.escape(token)}\b", text, re.I) is not None
