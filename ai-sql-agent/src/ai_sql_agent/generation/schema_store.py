from __future__ import annotations

import re
from itertools import combinations
from typing import Dict, Iterable, List, Set

from .objects import CotStep, LocalSchema, TableColumn, TableRelation, TableSchema


class LocalSchemaStore:
    """
    本地 Schema 存储。

    这里先用内存对象保存完整 Schema。
    后续可以替换为：
    - 字段级向量索引（pgvector）
    - 表关系索引
    - SchemaGraph 对象
    - 元数据服务 API

    SQL 生成阶段不会直接使用完整 Schema，而是根据 CoT 四元组中的处理对象
    裁剪出当前步骤需要的局部 Schema。
    """

    def __init__(
        self,
        tables: Dict[str, TableSchema],
        relations: List[TableRelation],
    ):
        self.tables = tables
        self.relations = relations

    @classmethod
    def build_demo_store(cls) -> "LocalSchemaStore":
        """
        构造 Demo Schema 存储。
        """
        tables = {
            "supplier": TableSchema(
                table_name="supplier",
                columns=[
                    TableColumn(
                        column_name="supplier_id",
                        description="供应商编号",
                        data_type="text",
                    ),
                    TableColumn(
                        column_name="supplier_name",
                        description="供应商名称",
                        data_type="text",
                    ),
                    TableColumn(
                        column_name="on_time_rate",
                        description="准时交付率，0-1 之间",
                        data_type="real",
                    ),
                ],
            ),
            "purchase_order": TableSchema(
                table_name="purchase_order",
                columns=[
                    TableColumn(
                        column_name="po_no",
                        description="采购单号",
                        data_type="text",
                    ),
                    TableColumn(
                        column_name="supplier_id",
                        description="供应商编号",
                        data_type="text",
                    ),
                    TableColumn(
                        column_name="po_amt",
                        description="采购金额（含税），ERP 缩写命名",
                        data_type="real",
                    ),
                ],
            ),
        }

        relations = [
            TableRelation(
                source_table="purchase_order",
                source_column="supplier_id",
                target_table="supplier",
                target_column="supplier_id",
                description="通过供应商编号关联采购单与供应商信息",
            )
        ]

        return cls(
            tables=tables,
            relations=relations,
        )


    @classmethod
    def from_schema_graph(cls, schema_graph: object) -> "LocalSchemaStore":
        """
        从 SchemaGraph 构建 SchemaStore。

        SchemaGraph 来自 Schema 检索阶段。
        SQL 生成阶段会先把这个 SchemaGraph 存下来，
        后续根据 CoT 四元组中的处理对象、操作指令和输出目标，
        再从中裁剪当前步骤需要的局部 Schema。
        """
        tables: Dict[str, TableSchema] = {}

        graph_tables = getattr(schema_graph, "tables", {}) or {}
        graph_columns = getattr(schema_graph, "columns", {}) or {}
        graph_relations = getattr(schema_graph, "relations", []) or []

        for table_name, _table in graph_tables.items():
            columns = []

            for col in graph_columns.get(table_name, []):
                columns.append(
                    TableColumn(
                        column_name=getattr(col, "column_name", ""),
                        description=getattr(col, "description", ""),
                        data_type=getattr(col, "data_type", ""),
                    )
                )

            tables[table_name] = TableSchema(
                table_name=table_name,
                columns=columns,
            )

        relations: List[TableRelation] = []

        for rel in graph_relations:
            relations.append(
                TableRelation(
                    source_table=getattr(rel, "source_table", ""),
                    source_column=getattr(rel, "source_column", ""),
                    target_table=getattr(rel, "target_table", ""),
                    target_column=getattr(rel, "target_column", ""),
                    description=getattr(rel, "description", ""),
                )
            )

        return cls(
            tables=tables,
            relations=relations,
        )

    def extract_local_schema(self, cot_step: CotStep) -> LocalSchema:
        """
        根据 CoT 四元组提取当前步骤对应的局部 Schema。

        提取逻辑：
        1. 从处理对象、操作指令、输出目标中正则提取 table.column。
        2. 根据字段找到所属表。
        3. 补充关联关系中的 join key 字段。
        4. 根据命中表两两组合，保留相关表关系。
        5. 返回局部 Schema。
        """
        mentioned_text = "；".join(
            [
                cot_step.processing_objects,
                cot_step.operation_instruction,
                cot_step.output_target,
            ]
        )

        table_columns = self._extract_table_columns(mentioned_text)
        table_names = self._extract_table_names(mentioned_text)

        for table_name, _ in table_columns:
            table_names.add(table_name)

        related_relations = self._find_relations_by_tables(table_names)

        for relation in related_relations:
            table_names.add(relation.source_table)
            table_names.add(relation.target_table)
            table_columns.add((relation.source_table, relation.source_column))
            table_columns.add((relation.target_table, relation.target_column))

        local_tables: Dict[str, TableSchema] = {}

        for table_name in sorted(table_names):
            full_table = self.tables.get(table_name)

            if not full_table:
                continue

            selected_columns = []

            for column in full_table.columns:
                if (table_name, column.column_name) in table_columns:
                    selected_columns.append(column)

            # 如果 CoT 只提到了表名，没有明确字段，就保留该表全部字段。
            if not selected_columns:
                selected_columns = full_table.columns

            local_tables[table_name] = TableSchema(
                table_name=table_name,
                columns=selected_columns,
            )

        return LocalSchema(
            tables=local_tables,
            relations=related_relations,
        )

    def _extract_table_columns(self, text: str) -> Set[tuple[str, str]]:
        """
        从文本中提取 table.column。
        """
        pattern = re.compile(
            r"(?P<table>[A-Za-z_][A-Za-z0-9_]*)\."
            r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)"
        )

        results: Set[tuple[str, str]] = set()

        for match in pattern.finditer(text):
            table_name = match.group("table")
            column_name = match.group("column")

            if table_name in self.tables:
                results.add((table_name, column_name))

        return results

    def _extract_table_names(self, text: str) -> Set[str]:
        """
        从文本中提取表名。
        """
        table_names: Set[str] = set()

        for table_name in self.tables:
            if re.search(rf"\b{re.escape(table_name)}\b", text):
                table_names.add(table_name)

        return table_names

    def _find_relations_by_tables(
        self,
        table_names: Iterable[str],
    ) -> List[TableRelation]:
        """
        根据表集合查询相关表关系。

        对命中的表名去重后做两两组合；
        如果关系两端表都在组合中，就保留该关系。
        """
        table_set = set(table_names)

        if len(table_set) < 2:
            return []

        pair_set = {
            tuple(sorted(pair))
            for pair in combinations(table_set, 2)
        }

        related_relations: List[TableRelation] = []

        for relation in self.relations:
            relation_pair = tuple(
                sorted([relation.source_table, relation.target_table])
            )

            if relation_pair in pair_set:
                related_relations.append(relation)

        return related_relations
