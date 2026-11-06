"""
找出把字段和它自己比较的筛选条件：`order_no IN (order_no)`、`order_no = order_no`，
以及反过来的 `order_no NOT IN (order_no)`、`order_no != order_no`。

前两种恒为真（字段不为空时），等于没写筛选；后两种恒为假，一行都查不出来。哪一种都不是
「按取值筛选」，结果都不能当真。多步查询里前一种是这样出来的：上一步执行成功
但一行都没有，下一步按计划要「用上一步结果里的单号筛选」，手里却没有单号，模型就把字段名
本身填进了 IN 列表。2026-11-06 客服问法那一轮（c18）里，这一步因此把配送表 936 行全查了
出来——问的是一张单，返回的是所有客户的配送记录。

只认**同一个字段**：两边的字段名相同，表限定相同或者都没写。`a.order_no = b.order_no`
是正常的关联条件，不算；一边带表名、一边不带的也不算（多表查询里分不清指的是谁）。
解析不了的 SQL 返回空列表：这里只负责多拦，不负责判 SQL 合不合法。
"""
from __future__ import annotations

from typing import List


def find_self_comparisons(sql: str, dialect: str = "") -> List[str]:
    """返回所有「字段和它自己比较」的条件原文；没有就是空列表。"""
    try:
        import sqlglot
        from sqlglot import exp

        from ai_sql_agent.auth.sql_analysis import _sqlglot_dialect

        tree = sqlglot.parse_one(sql, read=_sqlglot_dialect(dialect) or None)
    except Exception:
        return []
    if tree is None:
        return []

    found: List[str] = []
    for node in tree.find_all(exp.In):
        subject = node.this
        values = node.expressions
        if (
            isinstance(subject, exp.Column)
            and values
            # 列表里只要有一项是字段自己就够了：`order_no IN (order_no, 'J1')` 照样恒为真
            and any(_same_column(subject, value) for value in values)
        ):
            # NOT IN 在语法树里是 Not 包着 In：报外面那一层，读的人才看得出是哪个方向
            whole = node.parent if isinstance(node.parent, exp.Not) else node
            found.append(whole.sql())
    for node_type in (exp.EQ, exp.NEQ):
        for node in tree.find_all(node_type):
            if _same_column(node.left, node.right):
                found.append(node.sql())
    return found


def _same_column(left, right) -> bool:
    from sqlglot import exp

    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return False
    return (left.name.lower(), left.table.lower()) == (right.name.lower(), right.table.lower())
