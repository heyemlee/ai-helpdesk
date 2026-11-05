from __future__ import annotations

from typing import List

from .objects import ColumnSchema, FieldDocument


class SchemaIndexTextBuilder:
    """
    三级索引文本构建器。

    对每个字段构建三类文本：
    - keyword_text：用于关键词召回，尽量短，偏词面匹配。
    - vector_text：用于 Embedding 召回，偏语义概要。
    - rerank_text：用于 Rerank 精排，信息更完整。
    """

    def build(self, column: ColumnSchema) -> FieldDocument:
        """
        将 ColumnSchema 转换为 FieldDocument。
        """
        keyword_text = column.index_texts.keyword_text or self.build_keyword_text(column)
        vector_text = column.index_texts.vector_text or self.build_vector_text(column)
        rerank_text = column.index_texts.rerank_text or self.build_rerank_text(column)

        return FieldDocument(
            doc_id=column.full_name,
            column=column,
            keyword_text=keyword_text,
            vector_text=vector_text,
            rerank_text=rerank_text,
        )

    def build_many(self, columns: List[ColumnSchema]) -> List[FieldDocument]:
        """
        批量构建字段索引文档。
        """
        return [self.build(column) for column in columns]

    def build_keyword_text(self, column: ColumnSchema) -> str:
        """
        构建关键词索引文本。
        """
        parts = [
            column.database,
            column.table_name,
            column.column_name,
            column.data_type,
            column.description,
            " ".join(column.aliases),
            " ".join(column.table_aliases),
            " ".join(column.samples[:5]),
            column.business_usage,
            column.semantic_role,
        ]

        return "\n".join(part for part in parts if part)

    def build_vector_text(self, column: ColumnSchema) -> str:
        """
        构建向量索引文本。
        """
        parts = [
            f"字段名：{column.column_name}",
            f"所属表：{column.table_name}",
        ]

        if column.table_description:
            parts.append(f"表格含义：{column.table_description}")

        if column.table_aliases:
            parts.append(f"表别名：{', '.join(column.table_aliases)}")

        if column.description:
            parts.append(f"字段含义：{column.description}")

        if column.aliases:
            parts.append(f"字段别名：{', '.join(column.aliases)}")

        if column.business_usage:
            parts.append(f"业务用途：{column.business_usage}")

        if column.semantic_role:
            parts.append(f"字段角色：{column.semantic_role}")

        return "。".join(parts)

    def build_rerank_text(self, column: ColumnSchema) -> str:
        """
        构建 Rerank 索引文本。
        """
        parts = [
            f"数据库：{column.database}",
            f"表名：{column.table_name}",
            f"字段名：{column.column_name}",
            f"数据类型：{column.data_type}",
        ]

        if column.table_description:
            parts.append(f"表格摘要：{column.table_description}")

        if column.table_aliases:
            parts.append(f"表别名：{', '.join(column.table_aliases)}")

        if column.description:
            parts.append(f"字段含义：{column.description}")

        if column.aliases:
            parts.append(f"字段别名：{', '.join(column.aliases)}")

        if column.semantic_role:
            parts.append(f"字段角色：{column.semantic_role}")

        if column.value_range:
            parts.append(f"取值范围：{column.value_range}")

        if column.data_distribution:
            parts.append(f"数据分布：{column.data_distribution}")

        if column.business_usage:
            parts.append(f"业务用途：{column.business_usage}")

        if column.samples:
            parts.append(f"样例值：{', '.join(column.samples[:8])}")

        if column.is_primary_key:
            parts.append("主键信息：该字段是主键")

        if column.foreign_key_ref:
            parts.append(f"外键信息：该字段关联 {column.foreign_key_ref}")

        return "\n".join(parts)
