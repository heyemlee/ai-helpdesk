"""
标识符的大小写规则：策略里写的名字、数据库里存的名字、SQL 里写的名字，
什么时候算「同一个」。目录、检索裁剪、SQL 授权关卡三处共用这一份。

原来三处各有各的做法：关卡把 SQL 里解析出来的名字一律转成小写，策略照原样
（区分大小写）去比，目录则把「原名」和「小写名」各问一次。结果是一条写成
`shop_db.Orders` 的拒绝规则在关卡那里永远对不上——目录里看不到这张表，
`SELECT * FROM Orders` 却照样查得出来。

「全部转小写再比」修得了上面这一条，但修不对 PostgreSQL：在那里
`"Orders"`（带引号建的）和 `orders` 是**两张不同的表**。全转小写之后，
一条只放行 `orders` 的规则会把 `"Orders"` 也一起放出去。

所以规则跟着数据库自己的语义走：

| 数据库 | 名字怎么算同一个 | 规范名 |
| --- | --- | --- |
| SQLite（以及没有专门处理的方言） | 不区分大小写，加不加引号都一样 | ASCII 字母折成小写 |
| PostgreSQL | 没加引号的折成小写；加了引号的区分大小写 | 数据库里存的那个名字，原样 |

策略规则怎么匹配规范名，分两个方向——**拒绝从宽，放行从严**：

- 拒绝规则（`deny_tables` / `deny_columns`）与行范围规则（`row_filters`）在任何库上
  都**不区分大小写**。PostgreSQL 里 `Orders` 这条拒绝规则会把 `"Orders"` 和 `orders`
  一起拒掉：多拒一张同名表，代价是有人来问一句；少拒一张，是数据已经出去了。
- 放行规则（`allow_tables`）在 SQLite 上不区分大小写（那里本来就是同一张表）；
  在 PostgreSQL 上必须与存储名**大小写一致**：`orders` 只放行 `orders`
  （SQL 里不加引号写成 `Orders`、`ORDERS` 的也是它），放不到 `"Orders"` 头上。
  规则写错了大小写，后果是「这张表查不了」——看得见、有人会来问，
  不是悄悄多放出去一张表。
"""
from __future__ import annotations

from typing import Any

_CASE_SENSITIVE_DIALECTS = {"postgres", "postgresql"}

# 只折 ASCII 字母。SQLite 的标识符只对 ASCII 不区分大小写；PostgreSQL（UTF-8 库）
# 折叠没加引号的标识符时也只动 ASCII。`Ärger` 与 `ärger` 在两边都是两个名字。
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold(name: str) -> str:
    """ASCII 字母折成小写，其余字符不动。"""
    return str(name or "").translate(_ASCII_FOLD)


def quoted_names_are_case_sensitive(dialect: str) -> bool:
    """这个方言里，加了引号的标识符是否区分大小写（目前只有 PostgreSQL 是）。"""
    return (dialect or "").strip().lower() in _CASE_SENSITIVE_DIALECTS


def stored_name(name: str, dialect: str) -> str:
    """数据库里存的名字（表结构里读出来的）→ 规范名。"""
    return name if quoted_names_are_case_sensitive(dialect) else fold(name)


def referenced_name(name: str, *, quoted: bool, dialect: str) -> str:
    """
    SQL 里写的一个标识符 → 它指向的规范名。

    PostgreSQL 里没加引号的会被数据库折成小写，加了引号的原样；
    其余方言一律折成小写。
    """
    if quoted and quoted_names_are_case_sensitive(dialect):
        return name
    return fold(name)


def bind_dialect(access: Any, dialect: str) -> Any:
    """
    把一份策略绑到某个库的方言上，得到按那个库的大小写规则判定的视图。

    `DataPolicy` / `IntersectionPolicy` 实现了 `for_dialect`；自定义的权限过滤器
    没有这个方法，原样返回——它们自己决定怎么比名字。
    """
    hook = getattr(access, "for_dialect", None)
    return hook(dialect) if callable(hook) else access
