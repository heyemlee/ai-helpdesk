"""
SQLite 连接上的默认拒绝授权器（`Connection.set_authorizer`）。

应用层的 `sql_guard` 与授权关卡之后，数据库层原来只有 `file:...?mode=ro` 一道。
实测下来，`mode=ro` 不覆盖的几件事：

- `ATTACH` 一个别的文件，`CREATE TABLE o.x` 真的写进了那个文件；
  `ATTACH` 一个不存在的路径会在磁盘上创建它；
- `VACUUM INTO '<新路径>'` 产出整库副本；
- `sqlite_master` / `pragma_*` / `dbstat` 在宽泛的放行策略下可读，
  能拿到被禁表的 DDL、列名、库文件路径和页统计。

今天从应用层走不到前两件（守卫要求以 SELECT / WITH 开头，每次请求一条语句一条
新连接），所以这是**纵深防御**：守卫与关卡将来出一个洞，这里还能挡住。

默认拒绝：授权器对 SQLite 编译期报上来的每个动作问一次，白名单之外一律 DENY。
白名单就是「一条 SELECT 读数据」需要的那几个动作。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .sql_guard import is_forbidden_function

# SELECT 本身、读一列、递归 CTE。函数调用单独判（下面）。
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_RECURSIVE,
    }
)

# 这些表名在 SQLITE_READ 里出现就拒：库内部表、表值 pragma 函数、页统计虚表。
# `sqlite_dbpage`、`sqlite_stmt`、`sqlite_btreeinfo` 都在 `sqlite_` 前缀里。
_INTERNAL_TABLE_PREFIXES = ("sqlite_", "pragma_")
_INTERNAL_TABLES = frozenset({"dbstat"})

# 授权器自己拒绝时 SQLite 抛出的错误文本，执行器据此给用户可读的说明
NOT_AUTHORIZED = "not authorized"


def readonly_authorizer(
    action: int,
    arg1: Optional[str],
    arg2: Optional[str],
    dbname: Optional[str],
    source: Optional[str],
) -> int:
    """
    `sqlite3.Connection.set_authorizer` 的回调。

    `arg1` / `arg2` 的含义随动作而变：SQLITE_READ 是（表, 列），SQLITE_FUNCTION
    是（None, 函数名）。`dbname` 是 main / temp / ATTACH 出来的库名；ATTACH 本身被拒，
    所以正常只会看到 main。
    """
    if action in _ALLOWED_ACTIONS:
        if action == sqlite3.SQLITE_READ and _is_internal_table(arg1):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if action == sqlite3.SQLITE_FUNCTION:
        # 与 sql_guard 用同一份函数名单：guard 在应用层按语法树拦，这里在引擎里再拦一次
        if arg2 and is_forbidden_function(arg2.lower()):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return sqlite3.SQLITE_DENY


def _is_internal_table(name: Optional[str]) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return lowered in _INTERNAL_TABLES or lowered.startswith(_INTERNAL_TABLE_PREFIXES)
