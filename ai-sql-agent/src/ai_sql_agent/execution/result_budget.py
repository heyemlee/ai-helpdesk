"""
结果的字节预算。

执行器原来只限行数（`max_rows`）。行数上限管不住体积：`SELECT x, zeroblob(5000000)`
每行 5 MB，40 行就是 200 MB。这些字节
先进执行器的内存，再被序列化进 HTTP 响应、A2A 数据段、对话记忆库和下一步的提示词——
每一环都按「行数不多」设计。

这里给的是第二个维度：一次查询返回的**值**一共最多多少字节。口径是各个值的字节数之和
（字符串按 UTF-8、二进制按原长、数字按 8 字节），不含列名和 JSON 的引号逗号，
所以它是下限估计；默认值留了足够的余量（见 `Settings.max_result_bytes`）。

只在结果**已经取到**之后算。能在取数前拦住的是 SQLite 的 `SQLITE_LIMIT_LENGTH`
（单个值 / 单行的上限，见 `sqlite_executor`）；这里管的是「每个值都不大，加起来很大」。
"""
from __future__ import annotations

from typing import Any, Iterable, List, Sequence, Tuple

# 数字、布尔、NULL 的固定估价。真实占用随序列化格式变，量级对就够了。
_SCALAR_BYTES = 8


class RowTooLarge(Exception):
    """第一行就超过了字节预算：没有可以返回的部分结果。"""

    def __init__(self, size: int, limit: int):
        super().__init__(f"single row is {size} bytes, over the {limit} byte budget")
        self.size = size
        self.limit = limit


def value_bytes(value: Any) -> int:
    """一个值的字节数估计。容器递归；认不出的类型按它的 `str()` 算。"""
    if value is None or isinstance(value, (bool, int, float)):
        return _SCALAR_BYTES
    if isinstance(value, str):
        # isascii() 在 CPython 里是 O(1)：纯 ASCII 字符串不用真的编码一遍
        return len(value) if value.isascii() else len(value.encode("utf-8", "replace"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, memoryview):
        return value.nbytes
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(value_bytes(item) for item in value)
    if isinstance(value, dict):
        return sum(value_bytes(key) + value_bytes(item) for key, item in value.items())
    return len(str(value))


def row_bytes(row: Iterable[Any]) -> int:
    return sum(value_bytes(value) for value in row)


def trim_to_budget(rows: Sequence[Sequence[Any]], max_bytes: int) -> Tuple[List[Any], bool]:
    """
    按预算取 `rows` 的前缀，返回 `(保留的行, 是否被截)`。

    第一行就放不下时抛 `RowTooLarge`：返回零行并标「已截断」会让调用方以为
    表是空的或只是行数多，那是误导。
    """
    kept: List[Any] = []
    used = 0
    for row in rows:
        size = row_bytes(row.values() if isinstance(row, dict) else row)
        if used + size > max_bytes:
            if not kept:
                raise RowTooLarge(size, max_bytes)
            return kept, True
        used += size
        kept.append(row)
    return kept, False
