from __future__ import annotations

from typing import Iterator, List, Optional

from .objects import CotPlanningRequest, CotPlanningResult, CotPlanningStep, FewShotExample
from .prompt_builder import CotPromptBuilder
from .thinking_client import ThinkingModelClient


class CotPlanner:
    """
    CoT 规划服务。

    该服务位于 Schema 检索与 SQL 生成之间。
    """

    def __init__(
        self,
        prompt_builder: Optional[CotPromptBuilder] = None,
        thinking_client: Optional[ThinkingModelClient] = None,
    ):
        self.prompt_builder = prompt_builder or CotPromptBuilder()
        self.thinking_client = thinking_client or ThinkingModelClient()

    def plan(
        self,
        user_query: str,
        schema_graph: object,
        output_format_spec: str = "",
        few_shots: Optional[List[FewShotExample]] = None,
        extra_instructions: str = "",
    ) -> CotPlanningResult:
        """
        非流式生成 CoT 规划结果。

        extra_instructions 用于重新规划：把上一版计划的具体问题写进 Prompt，
        比不带任何反馈地重跑一次有用得多。
        """
        schema_context = self._schema_to_text(schema_graph)

        request = CotPlanningRequest(
            user_query=user_query,
            schema_context=schema_context,
            output_format_spec=output_format_spec,
            few_shots=few_shots or [],
            extra_instructions=extra_instructions,
        )

        prompt = self.prompt_builder.build(request)
        raw_output = self.thinking_client.generate(prompt)
        steps = self.parse_steps(raw_output)

        return CotPlanningResult(
            user_query=user_query,
            prompt=prompt,
            raw_output=raw_output,
            steps=steps,
        )

    def plan_stream(
        self,
        user_query: str,
        schema_graph: object,
        output_format_spec: str = "",
        few_shots: Optional[List[FewShotExample]] = None,
    ) -> Iterator[str]:
        """
        流式生成 CoT 规划结果。

        该方法只负责边生成边返回文本片段。
        如果需要解析最终 steps，可以在外部收集完整输出后调用 parse_steps()。
        """
        schema_context = self._schema_to_text(schema_graph)

        request = CotPlanningRequest(
            user_query=user_query,
            schema_context=schema_context,
            output_format_spec=output_format_spec,
            few_shots=few_shots or [],
        )

        prompt = self.prompt_builder.build(request)

        yield from self.thinking_client.generate_stream(prompt)

    def parse_steps(self, text: str) -> List[CotPlanningStep]:
        """
        解析模型输出中的步骤四元组。

        复用 ai_sql_agent.generation.CotStepParser，不再自己写一份正则。

        这里原先有一份独立实现，要求「步骤N：」前缀且字段之间必须是半角逗号。
        真实模型经常写成全角「，」、以「；」收尾，或在处理对象里写
        `material表（字段：a, b, c）` 这种自带逗号的括号内容——任意一种都会让
        整步匹配失败，链路表现为「未生成执行步骤」。同一个格式维护两份解析器，
        修了一份另一份还在漏，这本身就是缺陷来源。

        解析失败时不影响主流程，直接返回空列表。
        """
        from ai_sql_agent.generation.cot_parser import CotStepParser

        parsed = CotStepParser().parse_many(text)

        return [
            CotPlanningStep(
                step_no=index,
                database=step.database,
                processing_objects=step.processing_objects,
                operation_instruction=step.operation_instruction,
                output_target=step.output_target,
                expected_cardinality=step.expected_cardinality,
            )
            for index, step in enumerate(parsed, start=1)
        ]

    def _schema_to_text(self, schema_graph: object) -> str:
        """
        将 SchemaGraph 转换为文本。
        """
        if isinstance(schema_graph, str):
            return schema_graph

        if hasattr(schema_graph, "to_prompt_context"):
            return schema_graph.to_prompt_context()

        raise TypeError(
            "schema_graph 必须是字符串，或带有 to_prompt_context() 方法的对象。"
        )
