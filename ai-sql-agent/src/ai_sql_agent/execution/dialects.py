from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class SqlDialect:
    """
    SQL 方言描述。

    生产库是 PostgreSQL，本地 Demo 用 SQLite——同一句「取最近 7 天」两边
    写法就不一样。这里把方言差异显式建模，注入 SQL 生成与修复 Prompt，
    避免模型按「标准SQL」瞎猜。

    接入新方言时在这里加一个 SqlDialect 并配一个对应的执行器即可。
    """

    name: str
    display_name: str
    limit_syntax: str
    string_concat: str
    identifier_quote: str
    current_date_expr: str
    date_diff_hint: str
    extra_rules: tuple[str, ...] = ()

    def to_prompt_rules(self) -> str:
        """生成注入 Prompt 的方言约束段落。"""
        lines = [
            f"目标方言：{self.display_name}",
            f"- 限制行数：{self.limit_syntax}",
            f"- 字符串拼接：{self.string_concat}",
            f"- 标识符引用：{self.identifier_quote}",
            f"- 当前日期：{self.current_date_expr}",
            f"- 日期计算：{self.date_diff_hint}",
        ]
        lines.extend(f"- {rule}" for rule in self.extra_rules)
        return "\n".join(lines)


SQLITE = SqlDialect(
    name="sqlite",
    display_name="SQLite",
    limit_syntax="LIMIT n",
    string_concat="||",
    identifier_quote='双引号 "col"',
    current_date_expr="DATE('now')",
    date_diff_hint="用 DATE(col, '-7 day') 做日期偏移，JULIANDAY(a) - JULIANDAY(b) 求天数差",
    extra_rules=(
        "没有 RIGHT JOIN / FULL JOIN，需要时改写为 LEFT JOIN",
        "布尔值用 1/0，不要写 TRUE/FALSE",
    ),
)

POSTGRES = SqlDialect(
    name="postgres",
    display_name="PostgreSQL",
    limit_syntax="LIMIT n",
    string_concat="||",
    identifier_quote='双引号 "col"',
    current_date_expr="CURRENT_DATE",
    date_diff_hint="用 col - INTERVAL '7 day' 做日期偏移，日期相减直接得到天数",
    extra_rules=(
        "整数相除结果为整数，求比例时先 CAST(x AS numeric)",
        "字符串比较区分大小写，忽略大小写用 ILIKE",
    ),
)

_REGISTRY: Dict[str, SqlDialect] = {
    dialect.name: dialect for dialect in (SQLITE, POSTGRES)
}

# 常见别名
_ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
    "sqlite3": "sqlite",
}


def get_dialect(name: str) -> SqlDialect:
    """按名称取方言，未知方言回退 SQLite（本地 Demo 的默认库）。"""
    key = (name or "").strip().lower()
    key = _ALIASES.get(key, key)
    return _REGISTRY.get(key, SQLITE)
