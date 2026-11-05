from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class FewShotExample:
    """CoT 规划 Few-shot 示例。"""

    user_query: str
    schema_context: str
    output: str


@dataclass
class CotPlanningRequest:
    """CoT 规划请求。"""

    user_query: str
    schema_context: str
    output_format_spec: str = ""
    few_shots: List[FewShotExample] = field(default_factory=list)
    extra_instructions: str = ""
    """追加的纠正指令。重新规划时把上一版计划的具体问题写进来。"""


@dataclass
class CotPlanningStep:
    """结构化 CoT 四元组中的一个步骤。"""

    step_no: int
    database: str
    processing_objects: str
    operation_instruction: str
    output_target: str
    expected_cardinality: str = ""


@dataclass
class CotPlanningResult:
    """CoT 规划结果。"""

    user_query: str
    prompt: str
    raw_output: str
    steps: List[CotPlanningStep] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """转换为后续 SQL Coder 模型可使用的规划上下文。"""
        return self.raw_output
