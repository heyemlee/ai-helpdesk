"""
信任判据（契约 §4.6.8 的那张表）。

调用方要回答的是「这份数据能不能拿去回答客户」。契约把它拆成四问，这里实现的是
最关键的一问——**能不能当出处**：

    execution_status ∈ {success, partial_success} 且 has_risk == false 且 degraded == false

服务端为什么也要有这份实现：CONSUMER-TESTS 的 C 组（信任反例）要求两侧各跑一遍
同一批 fixture。服务端跑它是为了证明**自己产出的载荷会被正确分类**——
光把字段发出去不算数，得证明按契约的判据读出来是对的。

严格布尔（§4.6.7）：`"false"` 是非空字符串，`bool("false")` 为真。三个信任布尔
必须是真 JSON 布尔，类型不对按「不可信」处置，不做宽容转换。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

USABLE_AS_SOURCE = "usable_as_source"
PARTIAL = "partial"
NO_DATA = "no_data"
NOT_USABLE = "not_usable"

_DATABASE_QUERY_ROUTE = "database_query"


def _strict_bool(payload: Dict[str, Any], key: str, missing: bool) -> bool:
    """
    §4.6.7：缺失 / 类型不对时按**降低信任**的方向取值，不做宽容转换。

    `missing` 就是那个方向：`has_risk` 缺失按 True，`queried_database` 缺失按 False。
    一个「降低信任的信号」缺席时若按「没有这个信号」处理，等于默认信任——
    于是任何序列化 bug、版本错配、字段改名，都会静默地把不可信数据升级成可信数据。
    """
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return missing


SOURCE_CONTRACT = "contract"
"""结论来自服务端下发的字段——可以直接采信。"""

SOURCE_DERIVED = "derived"
"""服务端没下发，调用方按 §4.6.1 从 `steps[]` 推出来的。"""

SOURCE_MISSING = "missing"
"""连 `steps[]` 都拿不到，只能按最不可信的那一档处置。"""


def execution_status_of(payload: Dict[str, Any]) -> Tuple[str, str]:
    """
    取业务终态（§4.6.1），返回 `(状态, 来源)`。

    来源是 §4.6.7 的 `status_source`（v1.7 收进契约）。**必须标出来**，
    理由是 §4.6.2 自己的警告：「三个汇总数调用方不应自己 reduce，reduce 的口径
    会漂」——只要来源还是 `derived`，那句警告就还成立，缺口就没真关。

    三档判据（§4.6.7 的过渡表，是规范不是权宜）：

    | 情况 | 处置 | 来源 |
    | --- | --- | --- |
    | 服务端下发了 `execution_status` | 直接采信 | `contract` |
    | 没下发，但 `steps[]` 在 | 按 §4.6.1 从 `steps[]` 推导 | `derived` |
    | `steps[]` 也拿不到 | `failed` | `missing` |

    这不是发明新规则：§4.6.1 明确写了五个值的判据全部可由现有 `steps[]` 算出。
    """
    declared = payload.get("execution_status")
    if isinstance(declared, str) and declared:
        return declared, SOURCE_CONTRACT

    steps = payload.get("steps")
    if not isinstance(steps, list):
        return "failed", SOURCE_MISSING
    return _derive_status(payload, steps), SOURCE_DERIVED


def _derive_status(payload: Dict[str, Any], steps: List[Any]) -> str:
    if payload.get("route") != _DATABASE_QUERY_ROUTE:
        return "not_applicable"
    successes = [_strict_bool(s, "success", missing=False) for s in steps if isinstance(s, dict)]
    if not any(successes):
        return "failed"
    if not all(successes):
        return "partial_success"
    total = sum(
        int(s.get("row_count") or 0)
        for s in steps
        if isinstance(s, dict) and isinstance(s.get("row_count"), int)
    )
    return "success" if total > 0 else "no_data"


def result_truncated_of(payload: Dict[str, Any]) -> bool:
    """§4.6.3。顶层字段优先；没有就看 `any(steps[].truncated)`。缺失按 True。"""
    if payload.get("result_truncated") is True:
        return True
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return True
    return any(
        _strict_bool(step, "truncated", missing=True)
        for step in steps
        if isinstance(step, dict)
    )


def assess(payload: Optional[Dict[str, Any]], *, schema_valid: bool) -> str:
    """
    完整判定 = 先过 schema，再过业务判据。

    两步的顺序不能反，也不能省。CONSUMER-TESTS 的 B 组要的正是这个：
    schema 校验失败之后**走哪条分支**才是真正的安全属性——形状不对的载荷
    （缺 has_risk、`"false"` 字符串布尔、success=true 却带 error）必须落到
    「不可信」，而不是靠业务判据碰巧也拒掉它。
    """
    if not schema_valid:
        return NOT_USABLE
    return trust_verdict(payload)


def trust_verdict(payload: Optional[Dict[str, Any]]) -> str:
    """
    这份 data part 能不能当出处。返回 CONSUMER-TESTS 里 `trust` 那一列的值。

    判定顺序就是「哪个信号最能否定它」：先看载荷本身在不在，再看两个降信任布尔，
    最后才看跑成什么样。
    """
    if not isinstance(payload, dict):
        return NOT_USABLE

    if not _strict_bool(payload, "queried_database", missing=False):
        # 对话路径：那段 answer 是模型说的话，不是数据，不得当出处（§4.6.1 的 not_applicable）
        return NOT_USABLE
    if _strict_bool(payload, "has_risk", missing=True):
        return NOT_USABLE
    if _strict_bool(payload, "degraded", missing=True):
        return NOT_USABLE

    status, _source = execution_status_of(payload)
    steps = payload.get("steps")
    if _source != SOURCE_CONTRACT or not isinstance(steps, list) or status != _derive_status(payload, steps):
        return NOT_USABLE
    if any(not isinstance(s, dict) or type(s.get("success")) is not bool
           or type(s.get("row_count")) is not int or s["row_count"] < 0
           or not isinstance(s.get("rows"), list) or s["row_count"] != len(s["rows"])
           or any(not isinstance(row, dict) for row in s["rows"])
           or (s["success"] and (s.get("error") or s.get("risk_findings")))
           for s in steps):
        return NOT_USABLE
    if status == "no_data":
        # 「查到了，答案是没有」≠「没查成」。对客户是两句完全不同的话（§4.6.1）
        return NO_DATA
    if status == "partial_success":
        # 成功那步的数据是真的，不该整段丢——丢了比现状更糟（§4.6.1）
        return PARTIAL
    if status != "success":
        return NOT_USABLE

    if result_truncated_of(payload):
        # 数据可用，但任何「共有 N 条 / 全部 / 一共」的表述都不成立（§4.6.3）
        return PARTIAL
    return USABLE_AS_SOURCE
