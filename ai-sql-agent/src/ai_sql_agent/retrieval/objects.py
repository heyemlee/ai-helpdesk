from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class IndexTextBundle:
    """字段在不同检索阶段使用的索引文本。"""

    keyword_text: str = ""  # 关键词索引文本，用于 BM25 召回
    vector_text: str = ""   # 向量索引文本，用于语义召回
    rerank_text: str = ""   # 精排索引文本，用于 Rerank


@dataclass
class TableSchema:
    """表级 Schema，描述数据表的基础信息和业务含义。"""

    database: str
    table_name: str
    description: str = ""  # 表业务描述
    aliases: List[str] = field(default_factory=list)  # 表业务别名
    primary_keys: List[str] = field(default_factory=list)  # 主键字段


@dataclass
class ColumnSchema:
    """字段级 Schema，是 Schema 检索的基本单元。"""

    database: str
    table_name: str
    column_name: str
    data_type: str
    nullable: bool = True

    description: str = ""  # 字段业务含义
    aliases: List[str] = field(default_factory=list)  # 字段业务别名

    table_description: str = ""  # 所属表业务描述
    table_aliases: List[str] = field(default_factory=list)  # 所属表别名

    samples: List[str] = field(default_factory=list)  # 字段样例值
    value_range: str = ""  # 字段取值范围
    data_distribution: str = ""  # 字段数据分布

    business_usage: str = ""  # 字段业务用途
    semantic_role: str = ""  # 字段语义角色，如 metric、dimension、filter、time、join_key

    is_primary_key: bool = False
    foreign_key_ref: Optional[str] = None  # 外键目标，格式如 table.column

    index_texts: IndexTextBundle = field(default_factory=IndexTextBundle)  # 三级索引文本

    @property
    def full_name(self) -> str:
        """字段全限定名。"""
        return f"{self.database}.{self.table_name}.{self.column_name}"


@dataclass
class TableRelation:
    """表间关联关系，用于构建 SchemaGraph。"""

    database: str
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    relation_type: str = "foreign_key"  # 关系类型，如 foreign_key、business_relation
    description: str = ""  # 关系业务含义


@dataclass
class FieldDocument:
    """字段级检索文档，是实际参与召回和排序的文档单元。"""

    doc_id: str
    column: ColumnSchema
    keyword_text: str  # 关键词召回文本
    vector_text: str   # 向量召回文本
    rerank_text: str   # 精排文本


@dataclass
class SchemaHit:
    """Schema 检索命中结果，记录字段及各阶段得分。"""

    doc_id: str
    score: float
    column: ColumnSchema

    keyword_rank: Optional[int] = None  # 关键词召回排名
    vector_rank: Optional[int] = None   # 向量召回排名
    rrf_score: float = 0.0              # RRF 融合得分
    rerank_score: Optional[float] = None  # Rerank 得分


@dataclass
class SchemaGraph:
    """面向当前 Query 构建的 Schema 子图，作为 CoT 和 SQL 生成上下文。"""

    database: str
    tables: Dict[str, TableSchema]  # 相关表集合
    columns: Dict[str, List[ColumnSchema]]  # 按表分组的相关字段
    relations: List[TableRelation]  # 相关表间关系

    def to_prompt_context(self, compact: bool = False) -> str:
        """
        转换为可放入 Prompt 的 Schema 上下文。

        compact=True 时每个字段只保留「名称 / 类型 / 含义 / 取值域」，
        丢掉别名、语义角色、数据分布、业务用途。

        为什么需要这个档位：完整渲染每个字段约 60~100 token，36 个字段就顶满
        3200 的 Schema 预算，超出部分被字符级截断悄悄切掉。实测 5 条召回缺失里
        **4 条是预算切的、不是检索没找到**（`retrieval_recall_hit=True` 而
        `recall_hit=False`），后果是规划模型写出「字段信息未提供」然后生成
        `SELECT project_code FROM project WHERE 0`。

        取舍依据：字段进不进得来 > 字段描述得多详细。
        取值域优先于别名保留——用户按取值提问（「一车间」「delayed」）时，
        模型需要知道库里真实存的是什么，这比多两个别名重要得多。
        """
        lines: List[str] = []

        lines.append(f"数据库：{self.database}")
        lines.append("")

        for table_name, table in self.tables.items():
            lines.append(f"表名：{table_name}")

            if table.description:
                lines.append(f"表格摘要：{table.description}")

            if table.aliases:
                lines.append(f"表别名：{', '.join(table.aliases)}")

            if table.primary_keys:
                lines.append(f"主键：{', '.join(table.primary_keys)}")

            lines.append("字段：")

            for col in self.columns.get(table_name, []):
                lines.append(f"- 字段名：{col.column_name}")
                lines.append(f"  数据类型：{col.data_type}")

                if col.description:
                    lines.append(f"  字段含义：{col.description}")

                if not compact and col.aliases:
                    lines.append(f"  字段别名：{', '.join(col.aliases)}")

                if not compact and col.semantic_role:
                    lines.append(f"  字段角色：{col.semantic_role}")

                if col.samples:
                    limit = 4 if compact else 8
                    lines.append(f"  样例值：{', '.join(col.samples[:limit])}")

                if col.value_range:
                    lines.append(f"  取值范围：{col.value_range}")

                if not compact and col.data_distribution:
                    lines.append(f"  数据分布：{col.data_distribution}")

                if not compact and col.business_usage:
                    lines.append(f"  业务用途：{col.business_usage}")

            lines.append("")

        if self.relations:
            lines.append("表关联关系：")

            fan_out = False
            for rel in self.relations:
                description = f"；关联含义：{rel.description}" if rel.description else ""
                cardinality = self._cardinality_of(rel)
                if not cardinality.startswith("一对一"):
                    fan_out = True
                lines.append(
                    f"- {rel.source_table}.{rel.source_column} "
                    f"↔ {rel.target_table}.{rel.target_column}"
                    f"（{cardinality}）"
                    f"{description}"
                )

            if fan_out:
                lines.append(
                    "注意：沿「一对多」或「多对多」JOIN 之后，「一」那侧的行会被复制。"
                    "输出列只来自「一」那侧时必须加 DISTINCT，否则结果里会出现重复行。"
                )

        return "\n".join(lines)

    def _cardinality_of(self, rel: TableRelation) -> str:
        """
        用主键判定关联的基数方向。

        判据只有一条：关联列是不是对侧表的**完整**主键。是 → 那一侧每行唯一，
        另一侧可以挂多行。判不出来（复合主键、主键缺失、关联列不是主键）
        一律退到「多对多」——**保守方向是提醒去重，不是假设不会重复**。

        为什么要写进 Prompt：渲染用的 `↔` 是对称符号，模型看不出哪一侧会放大。
        2026-09-08 真实模型基线里，cross_db 与 multi_join 合计 46 条失败，
        其中 23 条是沿一对多 JOIN 之后没有去重（gold 有 DISTINCT、预测没有）。
        """
        source = self.tables.get(rel.source_table)
        target = self.tables.get(rel.target_table)
        source_is_pk = bool(source and source.primary_keys == [rel.source_column])
        target_is_pk = bool(target and target.primary_keys == [rel.target_column])

        if source_is_pk and target_is_pk:
            return "一对一"
        if source_is_pk:
            return f"一对多：{rel.source_table} 一行对应 {rel.target_table} 多行"
        if target_is_pk:
            return f"一对多：{rel.target_table} 一行对应 {rel.source_table} 多行"
        return f"多对多：{rel.source_table} 与 {rel.target_table} 两侧都可能重复"