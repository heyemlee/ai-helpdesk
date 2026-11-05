"""让 Schema 图适配 Token 预算——按相关性丢字段，而不是按字符位置切文本。

为什么需要这一层：原先的预算裁剪是对渲染好的整段文本做字符级截断的。
超预算时它砍掉的是「排在后面的字符」，而不是「最不相关的字段」——
一张表可能被从中间切断，规划模型随后写出「Schema 未提供完整表结构」，
再生成 `SELECT project_code FROM project WHERE 0`。

实测这条路径造成的损失是可测的：5 条召回缺失里 4 条的 gold 字段是被预算切掉的
（`retrieval_recall_hit=True` 而 `recall_hit=False`），不是检索没找到。

三档降级，先降精度、再丢字段，每一档都比字符截断更可控：

1. 完整渲染装得下 → 原样用；
2. 装不下 → 换精简渲染（丢别名/语义角色/业务用途，保留名称/类型/含义/取值域）；
3. 仍装不下 → **按相关性从末尾丢字段**，每张表保底若干字段、join key 永不丢弃。
"""

from __future__ import annotations

import copy
from typing import Dict, List, Tuple

from .token_budget import estimate_tokens

MIN_COLUMNS_PER_TABLE = 2
"""每张表保底保留的字段数。低于这个数，这张表在 Prompt 里就没有可用信息了。"""


def _relevance(column, position: int) -> tuple:
    """
    字段相关性，越大越该留。

    SchemaGraph 里的 ColumnSchema 不带检索分数（分数在 RetrievalHit 上），
    但**列表顺序就是检索排序**，所以位置本身是可用的相关性代理：
    靠前 = 更相关。有分数时优先用分数，没有就退回位置。
    """
    score = getattr(column, "rerank_score", None)
    if score is None:
        score = getattr(column, "rrf_score", None)
    return (float(score) if score is not None else 0.0, -position)


def _join_columns(graph) -> set:
    """参与表关联的字段，丢了就没法 join，任何情况下都要保留。"""
    keys = set()
    for relation in getattr(graph, "relations", None) or []:
        keys.add((relation.source_table, relation.source_column))
        keys.add((relation.target_table, relation.target_column))
    return keys


def _render(graphs: List, compact: bool) -> List[str]:
    return [g.to_prompt_context(compact=compact) for g in graphs if getattr(g, "tables", None)]


def _cost(sections: List[str]) -> int:
    return sum(estimate_tokens(section) for section in sections)


def _droppable(graphs: List) -> List[Tuple[tuple, int, str, int]]:
    """
    可丢弃字段清单：(相关性, 图序号, 表名, 表内下标)，按相关性升序——最不相关的先丢。

    join key 与每表保底名额不进这个清单。
    """
    candidates: List[Tuple[float, int, str, int]] = []
    for index, graph in enumerate(graphs):
        keys = _join_columns(graph)
        for table_name, columns in (getattr(graph, "columns", None) or {}).items():
            ranked = sorted(
                range(len(columns)),
                key=lambda i: _relevance(columns[i], i),
                reverse=True,
            )
            protected = set(ranked[:MIN_COLUMNS_PER_TABLE])
            for position in ranked:
                column = columns[position]
                if position in protected:
                    continue
                if (table_name, getattr(column, "column_name", "")) in keys:
                    continue
                candidates.append(
                    (_relevance(column, position), index, table_name, position)
                )
    candidates.sort(key=lambda item: item[0])  # 相关性升序：最不相关的先丢
    return candidates


def fit_graphs_to_budget(graphs: List, budget: int) -> Tuple[List[str], Dict[str, object]]:
    """
    把 Schema 图压进预算，返回 (按库拆开的上下文片段, 说明这次做了什么的字典)。

    调用方仍应把结果过一次 `fit_schema_sections` 兜底：这里是尽力而为，
    极端情况下（单个字段描述极长）仍可能超出。
    """
    graphs = [g for g in graphs if getattr(g, "tables", None)]
    info: Dict[str, object] = {"compact": False, "dropped": 0, "dropped_columns": []}
    if not graphs:
        return [], info

    sections = _render(graphs, compact=False)
    if _cost(sections) <= budget:
        return sections, info

    # 第二档：降精度
    info["compact"] = True
    sections = _render(graphs, compact=True)
    if _cost(sections) <= budget:
        return sections, info

    # 第三档：按相关性丢字段。改副本，不动检索结果本身。
    working = [copy.deepcopy(graph) for graph in graphs]
    candidates = _droppable(working)
    dropped: List[str] = []
    for _, index, table_name, position in candidates:
        columns = working[index].columns.get(table_name) or []
        if position >= len(columns):
            continue
        column = columns[position]
        columns[position] = None  # 先占位，避免下标错位
        dropped.append(
            f"{working[index].database}.{table_name}."
            f"{getattr(column, 'column_name', '?')}"
        )
        # 每丢 3 个重算一次，避免每丢一个就整段重渲染
        if len(dropped) % 3:
            continue
        for graph in working:
            for name, cols in graph.columns.items():
                graph.columns[name] = [c for c in cols if c is not None]
        sections = _render(working, compact=True)
        if _cost(sections) <= budget:
            break

    for graph in working:
        for name, cols in graph.columns.items():
            graph.columns[name] = [c for c in cols if c is not None]
    sections = _render(working, compact=True)
    info["dropped"] = len(dropped)
    info["dropped_columns"] = dropped
    return sections, info
