from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable


@runtime_checkable
class SchemaAccessFilter(Protocol):
    """
    Schema 可见性判定。

    检索层不依赖鉴权模块，只要求传进来的对象能回答两个问题：
    这张表能不能看、这个字段能不能看。ai_sql_agent.auth.DataPolicy 直接满足。
    """

    def allows_table(self, database: str, table: str) -> bool: ...

    def allows_column(self, database: str, table: str, column: str) -> bool: ...

    def restricts_columns(self, database: str, table: str) -> bool:
        """
        这张表上有没有列级限制？可选实现。

        执行前的授权关卡判 `SELECT *` 时要问这个：那一层没有 schema，展不开
        通配符，只能反过来确认「这张表没有禁列」。**没有实现这个方法、也没有
        暴露 `deny_columns` 的过滤器会被当成有限制**（拒掉 `SELECT *`）——
        自定义过滤器要放行通配符，就得显式实现它说一声。
        """
        ...


class AllowAll:
    """默认放行，未接入权限时使用。"""

    def allows_table(self, database: str, table: str) -> bool:
        return True

    def allows_column(self, database: str, table: str, column: str) -> bool:
        return True

    def restricts_columns(self, database: str, table: str) -> bool:
        return False

    def is_unrestricted(self) -> bool:
        return True


ALLOW_ALL = AllowAll()


def bind_dialect(access, dialect: str):
    """
    把权限过滤器绑到某个库的方言上（名字的大小写规则跟着数据库走，见
    `ai_sql_agent.auth.identifiers`）。检索层不依赖鉴权模块，所以这里按鸭子类型问：
    实现了 `for_dialect` 的就绑，没实现的原样用。
    """
    hook = getattr(access, "for_dialect", None)
    return hook(dialect) if callable(hook) else access


def filter_schema_graph(graph, access: SchemaAccessFilter):
    """
    按权限裁剪 SchemaGraph：无权表整张移除，无权字段逐个移除。

    裁剪发生在进入 Prompt 之前，模型压根看不到越权的表和字段，
    也就不会把它们写进 SQL。
    """
    database = getattr(graph, "database", "")
    tables = getattr(graph, "tables", None)
    if not tables:
        return graph

    allowed_tables = {
        name: table
        for name, table in tables.items()
        if access.allows_table(database, name)
    }

    allowed_columns: Dict[str, List] = {}
    for table_name, columns in getattr(graph, "columns", {}).items():
        if table_name not in allowed_tables:
            continue
        allowed_columns[table_name] = [
            column
            for column in columns
            if access.allows_column(database, table_name, column.column_name)
        ]

    relations = [
        relation
        for relation in getattr(graph, "relations", [])
        if relation.source_table in allowed_tables
        and relation.target_table in allowed_tables
    ]

    graph.tables = allowed_tables
    graph.columns = allowed_columns
    graph.relations = relations
    return graph
