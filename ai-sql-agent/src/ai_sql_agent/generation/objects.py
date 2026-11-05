from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class CotStep:
    """
    CoT 四元组中的单个步骤。

    数据库只用于后续执行路由；
    SQL 生成 Prompt 中只放处理对象、操作指令和输出目标。
    """

    database: str
    processing_objects: str
    operation_instruction: str
    output_target: str
    expected_cardinality: str = ""
    """
    预期输出量级（如「单条」「数条」「数十条」「数百条以上」）。

    执行后由基数校验拿它与实际行数比对：偏离过大通常意味着 join 扇出
    导致的重复计数——这类错误 SQL 能跑通、有结果，但结果是错的。
    """


@dataclass
class TableColumn:
    """局部 Schema 中的字段信息。"""

    column_name: str
    description: str = ""
    data_type: str = ""


@dataclass
class TableSchema:
    """局部 Schema 中的表信息。"""

    table_name: str
    columns: List[TableColumn] = field(default_factory=list)


@dataclass
class TableRelation:
    """局部 Schema 中的表关联关系。"""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    description: str = ""

    @property
    def relation_text(self) -> str:
        """表关联关系文本。"""
        return (
            f"{self.source_table}.{self.source_column} "
            f"↔ {self.target_table}.{self.target_column}"
        )

    @property
    def join_condition(self) -> str:
        """SQL Join 条件。"""
        return (
            f"{self.source_table}.{self.source_column} = "
            f"{self.target_table}.{self.target_column}"
        )


@dataclass
class LocalSchema:
    """
    当前 CoT 步骤对应的局部 Schema。

    局部 Schema 由完整 Schema 图裁剪得到，只保留当前步骤涉及的表、字段和关联关系。
    """

    tables: Dict[str, TableSchema] = field(default_factory=dict)
    relations: List[TableRelation] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """转换为 SQL 生成 Prompt 中使用的 Schema 文本。"""
        lines: List[str] = []

        for table in self.tables.values():
            lines.append(f"表名：{table.table_name}")
            lines.append("字段：")

            for column in table.columns:
                description = column.description or "无描述"
                data_type = column.data_type or "unknown"
                lines.append(
                    f"- {column.column_name}：{description}，{data_type}"
                )

            lines.append("")

        if self.relations:
            lines.append("表关联关系：")

            for relation in self.relations:
                lines.append(relation.relation_text)

            lines.append("")

        return "\n".join(lines).strip()


@dataclass
class SqlGenerationRequest:
    """SQL 生成请求。"""

    cot_step: CotStep
    local_schema: LocalSchema

    sql_dialect: str = "sqlite"
    """目标库方言名（见 ai_sql_agent.execution.dialects），决定 Prompt 中注入的语法约束。"""

    previous_results: str = ""
    """
    上一步执行结果（序列化文本）。

    跨库多步执行时，下一步 SQL 无法在当前库内 Join 上一步的表，
    需要把上一步结果集回填进 Prompt，由模型将其改写为字面量条件。
    """

    certified_examples: str = ""
    """
    已被用户确认正确的相似查询（渲染好的 Prompt 段落）。

    来自反馈回路：用户点「对」的查询会成为同类问题的参考写法。
    与 few-shot 的区别是它来自这套库上真实答对过的查询，
    表名、口径、join 路径都是这家公司的，不是通用示例。
    """


@dataclass
class SqlGenerationResult:
    """SQL 生成结果。"""

    database: str
    prompt: str
    raw_output: str
    sql: str

    def to_execution_request(self) -> Dict[str, str]:
        """
        转换为数据库执行请求。

        后续可以通过 MCP 路由到对应 database 的执行 API。
        """
        return {
            "database": self.database,
            "sql": self.sql,
        }
