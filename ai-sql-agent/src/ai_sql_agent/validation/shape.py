"""输出形状检查：不调模型、不看结果，只比对「问题要什么」与「SQL 给什么」。

实测静默错误的头号形态就是形状不符——问「台面类供应商的采购金额」，
模型答一个 `SUM(po_amt)`。数字本身算得没错，但用户以为看到的是每一单的金额，
拿到的是总额，而且全程没有任何迹象提示这一点。21 条静默错误里 4 条是这一类。

两条检查都是静态的（只看 SQL 的结构与计划文本），成本接近零，因此可以对每条查询都跑。
"""

from __future__ import annotations

import re
from typing import Optional

from .cardinality import parse_expectation
from .objects import AnomalyType, ValidationFinding

_AGGREGATE = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.I)
_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.I)

_AGGREGATION_INTENT = (
    "统计", "合计", "总共", "一共", "总数", "总额", "总金额", "总量", "总数量",
    "平均", "多少", "占比", "比例", "排名", "最多", "最少", "各", "每个", "每家",
    "每张", "分别", "汇总", "求和", "计数",
)
"""问句里出现这些词，说明用户确实要一个汇总/分组结果。"""

_AGGREGATION_INTENT_EN = re.compile(
    r"\b(?:totals?|sum|summari[sz]e[ds]?|average|avg|count|tally|"
    r"how\s+many|how\s+much|number\s+of|altogether|overall|"
    r"percent(?:age)?|proportion|share|ratio|rank(?:ed|ing)?|highest|lowest|top|"
    r"each|every|per|respectively)\b"
    r"|(?<!\bat\s)\b(?:most|least)\b",
    re.I,
)
"""
上面那张词表的英文对应。只有中文词表时，英文问句永远命不中任何一个词——
于是「What is the total …」这种明明白白要合计的问题，只要 SQL 里有 SUM 且没分组，
就会被报成「没让汇总却汇总了」。面向英文用户时这条提示几乎每问必报，
而一条每问必报的提示等于没有提示（见 `check_unrequested_aggregation` 里关于护栏线的说明）。

英文按**整词**匹配，不能像中文那样做子串匹配：`sum` 不该命中 summary，
`per` 不该命中 person，`top` 不该命中 countertop。`at least` / `at most` 是筛选条件
（「至少 0.9」），不是「最多 / 最少」，单独排除。
"""


def wants_aggregation(query: str) -> bool:
    return any(word in query for word in _AGGREGATION_INTENT) or bool(
        _AGGREGATION_INTENT_EN.search(query)
    )


def is_structurally_single_row(sql: str) -> bool:
    """
    SQL 结构上只可能返回一行：**最外层查询**有聚合函数且没有 GROUP BY。

    只看最外层，是因为聚合出现在别处并不折叠结果。「每种物料取最新一张快照」的
    标准写法是 `WHERE snapshot_date = (SELECT MAX(snapshot_date) …)`：全文里有 MAX、
    没有 GROUP BY，但外层一行不少。按全文搜聚合函数会把它报成「结构上只能返回 1 行」，
    而它实际返回了 8 行——一条和眼前的结果自相矛盾的提示，比不提示更伤信任。

    解析不了的 SQL 退回按全文判断：宁可照旧多报，也不因为解析失败把提示悄悄关掉。
    """
    verdict = _outer_query_collapses(sql)
    if verdict is None:
        return bool(_AGGREGATE.search(sql)) and not _GROUP_BY.search(sql)
    return verdict


_MAX_SOURCE_DEPTH = 8


def _outer_query_collapses(sql: str) -> Optional[bool]:
    """最外层查询是否把结果折叠成一行；解析不了返回 None。"""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001 - 解析器不认识的写法：交给调用方按文本判断
        return None
    if tree is None:
        return None

    ctes = {cte.alias_or_name.lower(): cte.this for cte in tree.find_all(exp.CTE)}
    aggregates = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)

    def own_aggregate(select) -> bool:
        """这一层查询自己的聚合：不算子查询里的，不算窗口函数，不算多参数的 MAX/MIN。"""
        for node in select.find_all(*aggregates):
            # SQLite 里 MAX(a, b) / MIN(a, b) 是逐行取大取小的标量函数，不折叠行
            if isinstance(node, (exp.Max, exp.Min)) and node.args.get("expressions"):
                continue
            parent = node.parent
            windowed = False
            while parent is not None and not isinstance(parent, exp.Select):
                if isinstance(parent, exp.Window):
                    windowed = True
                    break
                parent = parent.parent
            if not windowed and parent is select:
                return True
        return False

    def collapses(node, depth: int = 0) -> bool:
        while isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.Select) or depth > _MAX_SOURCE_DEPTH:
            return False  # UNION 之类的集合运算：每个分支各出各的行
        if node.args.get("group"):
            return False
        if own_aggregate(node):
            return True
        # 自己没有聚合：只有原样转述**唯一一个**单行来源时，结果才仍然是一行
        # （`WITH total AS (SELECT SUM(x) …) SELECT * FROM total`）。一旦 JOIN 了别的表就不是了。
        source = node.args.get("from")
        if source is None or node.args.get("joins"):
            return False
        source = source.this
        if isinstance(source, exp.Subquery):
            return collapses(source.this, depth + 1)
        if isinstance(source, exp.Table) and not source.args.get("db"):
            inner = ctes.get(source.name.lower())
            if inner is not None:
                return collapses(inner, depth + 1)
        return False

    return collapses(tree)


def check_unrequested_aggregation(query: str, sql: str) -> Optional[ValidationFinding]:
    """
    用户没要求汇总，SQL 却聚合了。

    只报 warn 不阻断：把明细汇总成一个数**有时**正是用户想要的，
    只是他没说清楚。提示的价值在于让他看一眼是不是这个意思，
    而不是替他判定对错。
    """
    if not sql.strip() or wants_aggregation(query):
        return None

    # 只报**整表折叠**（最外层聚合且无 GROUP BY）。按维度分组时，模型多半是按用户
    # 提到的实体在分组，那通常正是他要的；而无分组聚合是把一张明细拍成一个数——
    # 用户以为看到的是一张列表，拿到的是一个数字。
    #
    # 实测这条限制把精度从 38% 提到 71%：10 条误报里 8 条是分组聚合，
    # 去掉它们只漏 1 条真阳。误报率一旦超过护栏线，提示会被当噪音忽略，
    # 那时真正的静默错误也跟着漏过去——所以宁可少报。
    if not is_structurally_single_row(sql):
        return None

    detail = (
        "问题里没有出现「统计/合计/平均/每个」这类汇总要求，"
        "但 SQL 做了聚合且没有分组——明细被折叠成了一个数。"
        "如果你要的是每一条明细，这个结果回答的不是同一个问题。"
    )
    return ValidationFinding(
        anomaly=AnomalyType.AGGREGATION_UNREQUESTED,
        detail=detail,
        severity="warn",
    )


def check_shape_against_plan(expected_label: str, sql: str) -> Optional[ValidationFinding]:
    """
    计划声明的预期量级与 SQL 的**结构性**行数矛盾。

    与基数校验的区别：基数校验比的是「实际跑出多少行」，会被数据量影响；
    这里比的是「这条 SQL 结构上最多能出几行」，与数据无关，因此更确定。
    计划说要出数条、SQL 却是个无分组聚合，那不是数据少，是 SQL 写错了形状。
    """
    expectation = parse_expectation(expected_label)
    if expectation is None or not sql.strip():
        return None
    if expectation.high <= 1:
        return None
    if not is_structurally_single_row(sql):
        return None
    return ValidationFinding(
        anomaly=AnomalyType.SHAPE_CONTRADICTS_PLAN,
        detail=(
            f"计划预期输出「{expectation.label}」（至少 {max(expectation.low, 2)} 行），"
            "但这条 SQL 有聚合且没有分组，结构上只能返回 1 行。"
            "多半是漏了 GROUP BY，或者本来就不该聚合。"
        ),
        severity="warn",
    )
