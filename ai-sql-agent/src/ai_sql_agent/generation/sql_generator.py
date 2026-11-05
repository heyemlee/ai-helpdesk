from __future__ import annotations

from .coder_client import CoderModelClient
from .objects import CotStep, SqlGenerationRequest, SqlGenerationResult
from .prompt_builder import SqlPromptBuilder
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
    ) -> SqlGenerationResult:
        """
        根据 CoT 步骤生成 SQL。

        sql_dialect 由 Pipeline 按目标库注入，决定 Prompt 里的方言约束；
        previous_results 用于跨库多步执行时回填上一步结果集；
        certified_examples 是用户确认过正确的相似查询，作为写法参考注入。
        """
        local_schema = self.schema_store.extract_local_schema(cot_step)

        request = SqlGenerationRequest(
            cot_step=cot_step,
            local_schema=local_schema,
            sql_dialect=sql_dialect,
            previous_results=previous_results,
            certified_examples=certified_examples,
        )

        prompt = self.prompt_builder.build(request)
        raw_output = self.coder_client.generate_sql(prompt)
        sql = self.coder_client.clean_sql(raw_output)

        return SqlGenerationResult(
            database=cot_step.database,
            prompt=prompt,
            raw_output=raw_output,
            sql=sql,
        )
