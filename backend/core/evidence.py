"""
委派结果的信任判定——「这份数据能不能拿去回答客户」。

**为什么要单独一个模块。** 这个判断原先散在三个地方各写一遍：
`tools/sql_agent_client.py` 的 `/query` 归一、`tools/a2a_client.py` 的 A2A
归一、`api/main.py` 里那段决定进不进 `grounding_sources` 的分支。三份实现
各自漂了一点，合起来的效果是**两条路径对同一份载荷给出不同的信任结论**，
而回滚恰恰会让流量从一条路换到另一条路（契约 §6.2）。判定只能有一份。

判据出自 a2a-contract v1.6 的 §4.6「业务终态语义」与 §4.6.7「缺失 = 不可信」。
下面三条是这个模块存在的全部理由，改之前请先读懂它们：

1. **缺一个降低信任的信号，不等于可以信任。** `has_risk` / `degraded` /
   `steps[].success` 的作用都是**降低**信任。原来的 `bool(data.get("has_risk", False))`
   把"字段没送到"翻译成了"没有风险"，于是任何序列化 bug、版本错配、字段改名
   都会静默地把不可信数据升级成可信数据。所以这里每个字段的缺省值是
   **不利于信任的那个方向**（§4.6.7 的表）。

2. **`bool()` 不是布尔校验。** `bool("false")` 为真——字面写着 false 的字段
   会被读成 True。契约要求严格 JSON 布尔，类型不对按缺失处置，不做宽容转换。

3. **`queried_database` 不代表查询成功。** 服务端那边它的定义只有一行
   `return self.pipeline_result is not None`——SQL 全跑挂了它也是 `true`，
   而终态照发 `completed`（§4.6.8）。真正回答"跑成什么样"的是
   `execution_status`（§4.6.1）。

**关于 `execution_status` 今天还没有。** 服务端尚未下发这个字段（契约标 🔨）。
契约 §4.6.8 的表明写着这一档"今天只能自己 reduce `steps[].success`"，所以
本模块在字段缺席时按 §4.6.1 给的判据从 `steps[]` 推导，并把推导来源记在
`status_source` 上。**推导不等于缺口关闭**：§4.6.2 提醒过自己 reduce 的口径
会漂，真正关闭的判据是 `status_source == "contract"`，测试里有一条 xfail
用例专门盯着它（`tests/test_contract_fixtures.py`）。`steps[]` 也拿不到时
按 `failed` 处置，回到第 1 条。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# ── 契约 §4.6.1 的闭集 ────────────────────────────────────────────────────────
SUCCESS = "success"
PARTIAL_SUCCESS = "partial_success"
NO_DATA = "no_data"
FAILED = "failed"
NOT_APPLICABLE = "not_applicable"

EXECUTION_STATUSES = frozenset({SUCCESS, PARTIAL_SUCCESS, NO_DATA, FAILED, NOT_APPLICABLE})

# 能当出处的两个终态（契约 §4.6.8 的表：「能不能当出处」那一行）。
# `no_data` 不在里面——"查到了，答案是没有"没有任何数值可以引用，
# 但它必须与 `failed` 分开，见 Verdict.execution_status 的用法。
_SOURCEABLE = frozenset({SUCCESS, PARTIAL_SUCCESS})

_ISO_4217 = re.compile(r"^[A-Z]{3}$")

# 契约 §4.5（v1.6 起）：`database` 的契约面是**形状**，不是闭集。
# 具体有哪几个库随部署配置走，新增标识符不算契约变更——所以这里校验的是形状，
# **不是**一张写死的名单。清单外但形状合法的标识符（`hr_db`）是常规路径，
# 必须照常采信；形状不合法（`"ERP"`）则说明这份载荷根本不是契约形状，
# 按不可信处置（契约仓库的 dp-bad-06 就是这条用例）。
_DATABASE_ID = re.compile(r"^[a-z][a-z0-9_]*_db$")


def strict_bool(value: object, *, missing: bool) -> bool:
    """
    严格布尔（契约 §4.6.7）。

    只接受 JSON 布尔。`"true"` / `"false"` / `0` / `1` / `None` / 缺失一律
    落到 `missing`——调用方给的那个**不利于信任**的方向，不做宽容转换。

    `missing` 没有默认值是故意的：每个字段的安全方向不同
    （`has_risk` 缺失是 True，`queried_database` 缺失是 False），
    写调用点时必须显式想一次该往哪边倒。
    """
    return value if type(value) is bool else missing


def _strict_int(value: object) -> int | None:
    """严格非负整数。类型不对返回 None，由调用点决定怎么处置。"""
    if type(value) is int and value >= 0:
        return value
    return None


@dataclass(frozen=True)
class Verdict:
    """一次委派结果的信任结论。"""

    execution_status: str
    """契约 §4.6.1 的业务终态。"""

    status_source: str
    """
    `execution_status` 从哪来：

    - `"contract"` —— 服务端下发的（§4.6.1 落地后的形态）；
    - `"derived"`  —— 本地按 §4.6.1 的判据从 `steps[]` 推导；
    - `"missing"`  —— 两者都没有，按 `failed` 处置。

    这个字段是 P0-5 缺口的进度计量：全线变成 `"contract"` 那天，
    调用方就不再依赖自己 reduce 的口径了。
    """

    usable_as_source: bool
    """能不能进 `grounding_sources`（= 能不能给金额/日期背书）。"""

    queried_database: bool
    has_risk: bool
    degraded: bool
    truncated: bool

    reasons: tuple[str, ...] = ()
    """判为不可作出处的原因，人读的，进日志。"""

    caveats: tuple[str, ...] = field(default=())
    """
    即使数据可用也必须遵守的表述约束，逐条写给 Agent。

    例：截断结果禁止任何"共有 N 条 / 全部 / 一共"的说法（§4.6.3）。
    """


def _steps_of(data: Mapping) -> list[Mapping] | None:
    """
    取 `steps[]`。**取不到与取到空列表是两回事**：

    - 取不到（字段缺失 / 不是数组）→ `None` → 按"不可信"处置；
    - 取到 `[]` → 空列表 → 契约 §4.6.1「查库路径却没有步骤」= `failed`。
    """
    steps = data.get("steps")
    if not isinstance(steps, list):
        return None
    if not all(isinstance(s, Mapping) for s in steps):
        return None  # 形状不对，和缺失同处置
    return steps


def derive_execution_status(data: Mapping) -> tuple[str, str]:
    """
    定出业务终态，返回 `(status, source)`。

    优先用服务端下发的 `execution_status`；没有就按契约 §4.6.1 的判据从
    `steps[]` 推导；`steps[]` 也没有就是 `failed`（§4.6.7：缺失 = 不可信）。
    """
    declared = data.get("execution_status")
    if isinstance(declared, str) and declared in EXECUTION_STATUSES:
        return declared, "contract"

    # 下游自己判断这问题不需要查库（对话路径）。这不是故障，也不是可信数据。
    if not strict_bool(data.get("queried_database"), missing=False):
        return NOT_APPLICABLE, "derived"

    steps = _steps_of(data)
    if steps is None:
        return FAILED, "missing"
    if not steps:
        # 说自己查了库，却一步都没有。契约 §4.6.1 明确把它算进 failed。
        return FAILED, "derived"

    ok = [strict_bool(s.get("success"), missing=False) for s in steps]
    if not any(ok):
        return FAILED, "derived"
    if not all(ok):
        return PARTIAL_SUCCESS, "derived"

    # 全成功：再看有没有行。零行是 no_data，不是 failed——
    # "查到了，答案是没有" 和 "没查成" 对客户是两句不同的话。
    rows = 0
    for s in steps:
        n = _strict_int(s.get("row_count"))
        # row_count 不是合法整数时按 0 计：往"没查到"那边倒，不往"查到了"那边倒。
        rows += n or 0
    return (SUCCESS if rows > 0 else NO_DATA), "derived"


def _malformed_database_ids(data: Mapping, steps: list[Mapping] | None) -> list[str]:
    """形状不合法的库标识符（契约 §4.5）。返回空列表表示都合法。"""
    seen: list[object] = []
    selected = data.get("selected_databases")
    if isinstance(selected, list):
        seen.extend(selected)
    for step in steps or ():
        seen.append(step.get("database"))
    return [
        repr(db) for db in seen
        if not (isinstance(db, str) and _DATABASE_ID.match(db))
    ]


def _is_truncated(data: Mapping, steps: list[Mapping] | None) -> bool:
    """
    结果有没有被截断（契约 §4.6.3）。

    顶层 `result_truncated` 是 ➕ 字段（服务端算得出但还没下发），有就用；
    没有就看 `steps[i].truncated`——那个今天在线上。两者都拿不到时按
    **截断**处置（§4.6.7），因为"不知道有没有截断"和"截断了"对表述的约束一样：
    都不能说"一共 N 条"。
    """
    top = data.get("result_truncated")
    if type(top) is bool:
        if top:
            return True
    if steps is None:
        return True
    if not steps:
        return False
    return any(strict_bool(s.get("truncated"), missing=True) for s in steps)


def assess(data: Mapping | None) -> Verdict:
    """
    对一份归一后的委派结果给出信任结论。

    入参是 `/query` 与 A2A 两条路径归一之后的同一种 dict——**两条路径必须
    走这一个函数**，否则回滚（契约 §6.2）会变成静默的信任口径切换。
    """
    data = data if isinstance(data, Mapping) else {}

    queried = strict_bool(data.get("queried_database"), missing=False)
    has_risk = strict_bool(data.get("has_risk"), missing=True)
    degraded = strict_bool(data.get("degraded"), missing=True)

    steps = _steps_of(data)
    status, source = derive_execution_status(data)
    truncated = _is_truncated(data, steps)
    malformed = _malformed_database_ids(data, steps) if status != NOT_APPLICABLE else []

    reasons: list[str] = []
    caveats: list[str] = []

    if not queried:
        reasons.append("queried_database 不是 true（字段缺失或类型不对时同此处置）")
    if status not in _SOURCEABLE:
        reasons.append(f"execution_status={status}（来源: {source}）")
    if has_risk:
        reasons.append("has_risk=true（字段缺失或类型不对时同此处置）")
    if degraded:
        reasons.append("degraded=true（字段缺失或类型不对时同此处置）")
    if malformed:
        # 不是"不认识这个库"——那是常规路径。是这个标识符**不符合契约形状**，
        # 说明载荷本身不是契约形状，别的字段也就不能当真。
        reasons.append(f"database 标识符不符合契约形状 §4.5: {', '.join(malformed)}")

    if status == PARTIAL_SUCCESS:
        caveats.append(
            "本次查询只有部分步骤成功：已成功那部分的数据是真的，未成功的部分"
            "**没有任何系统依据**，不得据此给出订单状态、日期、金额或库存数字。"
        )
    if truncated:
        caveats.append(
            "结果集被截断：禁止任何总量表述（\"共有 N 条\"\"全部\"\"一共\"），"
            "也不得基于它做求和、计数、比例类断言，只能说\"至少 N 条\"。"
        )
    if status == NO_DATA:
        caveats.append(
            "查询执行成功但零行：这是\"查到了，答案是没有记录\"，"
            "**不是**\"没查成\"，据实这样告诉客户，不要含糊成查询失败。"
        )

    return Verdict(
        execution_status=status,
        status_source=source,
        usable_as_source=(
            queried and status in _SOURCEABLE and not has_risk and not degraded and not malformed
        ),
        queried_database=queried,
        has_risk=has_risk,
        degraded=degraded,
        truncated=truncated,
        reasons=tuple(reasons),
        caveats=tuple(caveats),
    )


def normalize_delegation_result(
    data: Mapping | None,
    *,
    answer: str = "",
    steps_key: str = "steps",
    trace_id_fallback: str = "",
) -> dict:
    """
    把两条 transport 的载荷归一成同一种 dict，供 `assess()` 与上层消费。

    `steps_key` 存在是因为两条路径给这个数组起的名字不同：A2A 的 data part
    叫 `steps`（契约 §4.4），直连 `/query` 的响应叫 `step_logs`
    （`ai_sql_api/app.py` 的 `QueryResponse`）。**只有名字不同**，元素形状
    是同一个 `StepLogResponse`——所以归一到 `steps` 之后，下游判定完全一致。

    三个信任布尔在这里**不做转换**，原样带过去：转换发生在 `assess()` 里，
    只有一处。提前 `bool()` 一下正是原来那个 fail-open 缺口的形态。
    """
    data = data if isinstance(data, Mapping) else {}
    steps = data.get(steps_key)
    out = {
        "answer": (answer or str(data.get("answer", "") or "")).strip(),
        "queried_database": data.get("queried_database"),
        "has_risk": data.get("has_risk"),
        "degraded": data.get("degraded"),
        "degrade_reason": str(data.get("degrade_reason", "") or ""),
        "selected_databases": data.get("selected_databases") or [],
        "trace_id": str(data.get("trace_id") or trace_id_fallback or ""),
        "route": str(data.get("route", "") or ""),
        "steps": steps if isinstance(steps, list) else None,
    }
    # ➕/🔨 字段（§4.6）：服务端下发了就带上，没下发就别造。缺席时的处置在
    # assess() 里，不在这里——这里造一个默认值等于替服务端声明它没声明的事。
    for optional in ("execution_status", "result_truncated", "as_of", "as_of_source", "currency"):
        if optional in data:
            out[optional] = data[optional]
    return out


def declared_currency(data: Mapping | None) -> str | None:
    """
    数据自己声明的币种（契约 §4.6.5），没有就是 `None`。

    只接受 ISO 4217 三字母大写。**不要在这里回退到部署配置**——那个回退是
    §4.6.5 点名的反例，它让一个配置值伪装成从数据库查出来的事实。回退的
    去留由调用点决定，并且必须在措辞里说明来源。
    """
    value = (data or {}).get("currency")
    return value if isinstance(value, str) and _ISO_4217.match(value) else None
