from __future__ import annotations

from .objects import ColumnSchema, FieldDocument


class FieldDocumentBuilder:
    """字段检索文档构建器，用于生成字段级三类索引文本。"""

    @staticmethod
    def build(column: ColumnSchema) -> FieldDocument:
        """构建字段级检索文档，优先使用预配置索引文本。"""
        index_texts = getattr(column, "index_texts", None)

        keyword_text = ""
        vector_text = ""
        rerank_text = ""

        if index_texts is not None:
            keyword_text = getattr(index_texts, "keyword_text", "") or ""
            vector_text = getattr(index_texts, "vector_text", "") or ""
            rerank_text = getattr(index_texts, "rerank_text", "") or ""

        if not keyword_text:
            keyword_text = FieldDocumentBuilder._build_keyword_text(column)

        if not vector_text:
            vector_text = FieldDocumentBuilder._build_vector_text(column)

        if not rerank_text:
            rerank_text = FieldDocumentBuilder._build_rerank_text(column)

        return FieldDocument(
            doc_id=column.full_name,
            column=column,
            keyword_text=keyword_text,
            vector_text=vector_text,
            rerank_text=rerank_text,
        )

    @staticmethod
    def _build_keyword_text(column: ColumnSchema) -> str:
        """构建 BM25 关键词召回文本。"""
        parts = [
            column.database,
            column.table_name,
            column.column_name,
            column.data_type,
            column.description,
            " ".join(column.aliases),
            column.table_description,
            " ".join(column.table_aliases),
            " ".join(column.samples[:5]),
        ]

        return "\n".join(part for part in parts if part)

    @staticmethod
    def _build_vector_text(column: ColumnSchema) -> str:
        """构建 Embedding 语义召回文本。"""
        parts = [
            f"字段名：{column.column_name}",
            f"所属表：{column.table_name}",
            f"所属表业务含义：{column.table_description}",
            f"字段含义：{column.description}",
            f"字段别名：{', '.join(column.aliases)}" if column.aliases else "",
        ]

        business_usage = getattr(column, "business_usage", "")
        semantic_role = getattr(column, "semantic_role", "")

        if business_usage:
            parts.append(f"业务用途：{business_usage}")

        if semantic_role:
            parts.append(f"字段角色：{semantic_role}")

        return "\n".join(part for part in parts if part)

    @staticmethod
    def _build_rerank_text(column: ColumnSchema) -> str:
        """构建 Rerank 精排文本。"""
        parts = [
            f"数据库：{column.database}",
            f"表名：{column.table_name}",
            f"表业务描述：{column.table_description}",
            f"表别名：{', '.join(column.table_aliases)}" if column.table_aliases else "",
            f"字段名：{column.column_name}",
            f"字段类型：{column.data_type}",
            f"字段业务描述：{column.description}",
            f"字段别名：{', '.join(column.aliases)}" if column.aliases else "",
            f"是否主键：{column.is_primary_key}",
            f"外键引用：{column.foreign_key_ref or '无'}",
            f"样例值：{', '.join(column.samples)}" if column.samples else "",
        ]

        value_range = getattr(column, "value_range", "")
        data_distribution = getattr(column, "data_distribution", "")
        business_usage = getattr(column, "business_usage", "")
        semantic_role = getattr(column, "semantic_role", "")

        if value_range:
            parts.append(f"取值范围：{value_range}")

        if data_distribution:
            parts.append(f"数据分布：{data_distribution}")

        if business_usage:
            parts.append(f"业务用途：{business_usage}")

        if semantic_role:
            parts.append(f"字段角色：{semantic_role}")

        return "\n".join(part for part in parts if part)