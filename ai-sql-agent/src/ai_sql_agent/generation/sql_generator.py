from __future__ import annotations

from .coder_client import CoderModelClient
from .objects import CotStep, SqlGenerationRequest, SqlGenerationResult, TableSchema
from .prompt_builder import SqlPromptBuilder
from .projection import RawProjectionError, is_business_identifier, projection_issues
from .schema_store import LocalSchemaStore


class SqlGenerator:
    """
    SQL 生成服务。

    输入：
    - CoT 四元组
    - SchemaStore 中提取的局部 Schema

    输出：
    - SQL
    - database + sql 执行请求
    """

    def __init__(
        self,
        schema_store: LocalSchemaStore,
        prompt_builder: SqlPromptBuilder | None = None,
        coder_client: CoderModelClient | None = None,
    ):
        self.schema_store = schema_store
        self.prompt_builder = prompt_builder or SqlPromptBuilder()
        self.coder_client = coder_client or CoderModelClient()

    def generate(
        self,
        cot_step: CotStep,
        sql_dialect: str = "sqlite",
        previous_results: str = "",
        certified_examples: str = "",
        user_query: str = "",
    ) -> SqlGenerationResult:
        """
        根据 CoT 步骤生成 SQL。

        sql_dialect 由 Pipeline 按目标库注入，决定 Prompt 里的方言约束；
        previous_results 用于跨库多步执行时回填上一步结果集；
        certified_examples 是用户确认过正确的相似查询，作为写法参考注入。
        """
        local_schema = self.schema_store.extract_local_schema(cot_step)
        if user_query:
            # Preserve business target columns for the already-authorized,
            # already-selected tables even when the plan omitted them. This
            # exposes real schema columns, never values copied from a query.
            for name, table in local_schema.tables.items():
                names = {c.column_name for c in table.columns}
                columns = [c for c in self.schema_store.tables[name].columns
                           if c.column_name not in names and is_business_identifier(c)]
                if columns:
                    local_schema.tables[name] = TableSchema(name, [*table.columns, *columns])

        request = SqlGenerationRequest(
            cot_step=cot_step,
            local_schema=local_schema,
            sql_dialect=sql_dialect,
            previous_results=previous_results,
            certified_examples=certified_examples,
            user_query=user_query,
        )

        prompt = self.prompt_builder.build(request)
        raw_output = self.coder_client.generate_sql(prompt)
        sql = self.coder_client.clean_sql(raw_output)

        issues = projection_issues(sql, cot_step, local_schema, sql_dialect, user_query=user_query)
        if issues:
            # One semantic correction, charged as a normal model call. It does
            # not enable transport retries or relax the later SQL validators.
            prompt += ("\n# 输出字段检查未通过\n" + "\n".join(issues)
                       + "\n保留原筛选条件与数量/日期口径，直接返回原始列；不得用常量或 JSON 字符串代替。"
                       + "只能使用已提供的表、列和关联关系；重新输出完整的一条 SQL。\n"
                       + "待纠正的 SQL：\n" + sql)
            raw_output = self.coder_client.generate_sql(prompt)
            sql = self.coder_client.clean_sql(raw_output)
            issues = projection_issues(sql, cot_step, local_schema, sql_dialect, user_query=user_query)
            if issues:
                raise RawProjectionError(sql, issues)

        return SqlGenerationResult(
            database=cot_step.database,
            prompt=prompt,
            raw_output=raw_output,
            sql=sql,
        )
