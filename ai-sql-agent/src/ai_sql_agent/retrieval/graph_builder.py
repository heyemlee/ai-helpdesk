from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from .objects import ColumnSchema, SchemaGraph, SchemaHit, TableRelation, TableSchema


def build_schema_graph(
    hits: List[SchemaHit],
    tables: Dict[str, TableSchema],
    all_columns: List[ColumnSchema],
    relations: List[TableRelation],
    include_join_columns: bool = True,
    min_columns_per_table: int = 0,
    table_order: Optional[Sequence[str]] = None,
) -> SchemaGraph:
    """
    根据检索命中的字段构建 Schema 子图。

    输入：
    - hits: 检索命中的字段。
    - tables: 全量表 Schema。
    - all_columns: 全量字段 Schema。
    - relations: 全量表关系。
    - min_columns_per_table: 每张选中表在图中至少保留的字段数（0 表示不保底）。
    - table_order: 表的先后。三层检索传表级检索的排名；不传时按字段命中的先后。
      只影响先后，不影响选哪些表、哪些字段。

    输出：
    - 仅包含相关表、相关字段、相关关联关系的 SchemaGraph。

    为什么要 min_columns_per_table：字段级检索取的是**全局** top-k，
    多表关联查询里常出现一张表占满配额、另一张表只剩 join key 的情况——
    第二张表的输出字段和过滤字段都进不了图，SQL 必然写错。
    给每张选中表一个保底配额可以避免这种失衡。
    """

    # 不给排名时，表排在它最靠前的那个字段的位置（hits 已按检索得分排好）。
    # 不能拿 set 去重——字符串集合的遍历顺序随 PYTHONHASHSEED 变，同一个问题换一个
    # 进程，Schema 上下文里的表就换了顺序：提示词复现不了，前缀缓存也命不中。
    selected_tables = list(dict.fromkeys(hit.column.table_name for hit in hits))
    if table_order:
        # 字段命中的先后由精排定，而精排文本里拼着所属表的说明：说明里恰好带着问题用词的表，
        # 所有字段一起排到前面。本地精排下，「查询板材类物料的在库数量」里 stock_snapshot 的
        # 五个字段都排在 material 前面，这张表就压在了要输出物料名称的 material 前面。表级检索
        # 按整张表排，用它的排名；排名里没有的表（字段级回退检索带进来的）按命中先后排在后面。
        rank = {name: index for index, name in enumerate(table_order)}
        selected_tables.sort(key=lambda name: rank.get(name, len(rank)))

    selected_columns = defaultdict(dict)

    for hit in hits:
        col = hit.column
        selected_columns[col.table_name][col.column_name] = col

    # 每张选中表按检索得分补足保底字段
    if min_columns_per_table > 0:
        ranked_by_table = defaultdict(list)
        for hit in hits:
            ranked_by_table[hit.column.table_name].append(hit)

        for table_name in selected_tables:
            current = selected_columns[table_name]
            if len(current) >= min_columns_per_table:
                continue
            for column in all_columns:
                if len(current) >= min_columns_per_table:
                    break
                if column.table_name != table_name:
                    continue
                if column.column_name not in current:
                    current[column.column_name] = column

    selected_relations: List[TableRelation] = []

    for relation in relations:
        source_selected = relation.source_table in selected_tables
        target_selected = relation.target_table in selected_tables

        if source_selected and target_selected:
            selected_relations.append(relation)

            if include_join_columns:
                source_col = _find_column(
                    all_columns,
                    relation.source_table,
                    relation.source_column,
                )
                target_col = _find_column(
                    all_columns,
                    relation.target_table,
                    relation.target_column,
                )

                if source_col:
                    selected_columns[relation.source_table][relation.source_column] = source_col

                if target_col:
                    selected_columns[relation.target_table][relation.target_column] = target_col

    graph_tables = {
        table_name: tables[table_name]
        for table_name in selected_tables
        if table_name in tables
    }

    graph_columns = {
        table_name: list(column_map.values())
        for table_name, column_map in selected_columns.items()
    }

    database = ""
    if graph_tables:
        database = next(iter(graph_tables.values())).database

    return SchemaGraph(
        database=database,
        tables=graph_tables,
        columns=graph_columns,
        relations=selected_relations,
    )


def _find_column(
    columns: List[ColumnSchema],
    table_name: str,
    column_name: str,
) -> ColumnSchema | None:
    for column in columns:
        if column.table_name == table_name and column.column_name == column_name:
            return column

    return None
