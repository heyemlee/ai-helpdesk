from __future__ import annotations

import os
import sys

from ai_sql_agent.planning.cot_planner import CotPlanner
from ai_sql_agent.planning.thinking_client import ThinkingModelClient, ThinkingModelConfig


def build_demo_schema_context() -> str:
    """
    构造 Demo Schema 图文本。

    实际项目中，这里直接传入：
        schema_graph.to_prompt_context()
    """
    return """数据库：erp_db
表名：supplier
表格摘要：供应商表，记录供应商名称、供货品类与准时交付率。
字段：
- 字段名：on_time_rate
  数据类型：real
  字段含义：准时交付率，0-1 之间
  样例值：0.65，0.72，0.88，0.95
  取值范围：0-1
- 字段名：supplier_name
  数据类型：text
  字段含义：供应商名称
- 字段名：supplier_id
  数据类型：text
  字段含义：供应商编号

表名：purchase_order
表格摘要：采购单表，记录采购单与含税采购金额。
字段：
- 字段名：po_amt
  数据类型：real
  字段含义：采购金额（含税），ERP 缩写命名
  样例值：88000.00，158000.00，742000.00
- 字段名：supplier_id
  数据类型：text
  字段含义：供应商编号

表关联关系：
- purchase_order.supplier_id ↔ supplier.supplier_id；关联含义：通过供应商编号关联采购单与供应商信息"""


def main() -> None:
    """
    运行流式 Demo。

    不配置 DASHSCOPE_API_KEY 时走 Mock 流式输出。
    配置后调用真实思考模型流式输出。

    示例：
        export DASHSCOPE_API_KEY="你的APIKey"
        export DASHSCOPE_COT_MODEL="qwen-plus"
        python examples/cot_planning.py
    """
    planner = CotPlanner(
        thinking_client=ThinkingModelClient(
            ThinkingModelConfig(
                api_key=os.getenv("DASHSCOPE_API_KEY", ""),
                model=os.getenv("DASHSCOPE_COT_MODEL", "qwen-plus"),
                temperature=0.0,
                use_mock_when_no_api_key=True,
                mock_stream_delay=0.01,
            )
        )
    )

    user_query = "查询准时率低于0.9的供应商名称"
    schema_context = build_demo_schema_context()

    print("=" * 80)
    print("CoT Planning Stream Output")
    print("=" * 80)

    chunks = []

    for chunk in planner.plan_stream(
        user_query=user_query,
        schema_graph=schema_context,
    ):
        print(chunk, end="", flush=True)
        chunks.append(chunk)

    print()

    raw_output = "".join(chunks)

    print("\n" + "=" * 80)
    print("Parsed Steps")
    print("=" * 80)

    steps = planner.parse_steps(raw_output)

    for step in steps:
        print(f"步骤{step.step_no}:")
        print(f"数据库: {step.database}")
        print(f"处理对象: {step.processing_objects}")
        print(f"操作指令: {step.operation_instruction}")
        print(f"输出目标: {step.output_target}")

    if not steps:
        print("未解析到结构化步骤，请检查模型输出格式。", file=sys.stderr)


if __name__ == "__main__":
    main()
