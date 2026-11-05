from __future__ import annotations

import re
from typing import Dict, List

from ai_sql_agent.core.model_text import plain_text

from .objects import CotStep


class CotStepParser:
    """
    CoT 四元组解析器。

    从模型输出中提取每一步的：数据库、处理对象、操作指令、输出目标，
    以及可选的预期输出量级（供执行后的基数校验使用）。

    **按标签定位，不按分隔符切分。** 早期实现用一条大正则要求字段之间是
    半角逗号，真实模型经常写成全角「，」、以「；」收尾、或最后一个字段不带
    逗号——任意一处不匹配，整步就解析不出来，表现为「未生成执行步骤」。
    实测这一条就吃掉了约三成的评测样本。改为先定位标签、再取标签之间的
    内容后，分隔符写法就不再影响解析。
    """

    LABELS = ("数据库", "处理对象", "操作指令", "输出目标", "预期输出量级")

    _LABEL_PATTERN = re.compile(
        r"(?P<label>" + "|".join(LABELS) + r")\s*[:：]",
    )

    # 字段值尾部可能残留的分隔符与括号
    _TRAILING = " \t\r\n,，;；、)）"

    # 块边界：一步的最后一个字段后面往往跟着 ")" 和下一个 "步骤N："，
    # 按标签取值时会把它们一起吞进来，这里在第一处边界截断。
    _BLOCK_END = re.compile(r"[)）]\s*$|[)）]\s*\n|\n\s*步骤\s*\d+", re.M)

    def parse_one(self, text: str) -> CotStep:
        """解析单个 CoT 四元组。"""
        steps = self.parse_many(text)
        if not steps:
            raise ValueError("未能从 CoT 文本中解析出四元组。")
        return steps[0]

    def parse_many(self, text: str) -> List[CotStep]:
        """
        解析多个 CoT 步骤。

        先去掉 Markdown 记号（`**数据库**：`、`- 输出目标：`、`### 步骤2`）：标签和冒号之间
        夹着 `**` 时按标签找不到，整份规划会解析成零步。
        """
        text = plain_text(text)
        matches = list(self._LABEL_PATTERN.finditer(text))
        if not matches:
            return []

        # 逐个标签取值：值 = 本标签结束 → 下一个标签开始
        fields: List[tuple[str, str]] = []
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            raw = text[start:end]
            boundary = self._BLOCK_END.search(raw)
            if boundary is not None:
                raw = raw[: boundary.start()]
            value = raw.strip().strip(self._TRAILING).strip()
            fields.append((match.group("label"), value))

        # 每遇到「数据库」开启新的一步
        steps: List[CotStep] = []
        current: Dict[str, str] = {}
        for label, value in fields:
            if label == "数据库" and current:
                step = self._build(current)
                if step is not None:
                    steps.append(step)
                current = {}
            current[label] = value

        if current:
            step = self._build(current)
            if step is not None:
                steps.append(step)

        return steps

    def _build(self, fields: Dict[str, str]) -> CotStep | None:
        """字段齐不齐都尽量成步；数据库与输出目标缺一不可。"""
        database = fields.get("数据库", "")
        output_target = fields.get("输出目标", "")
        if not database or not output_target:
            return None

        return CotStep(
            database=database,
            processing_objects=fields.get("处理对象", ""),
            operation_instruction=fields.get("操作指令", ""),
            output_target=output_target,
            expected_cardinality=fields.get("预期输出量级", ""),
        )
