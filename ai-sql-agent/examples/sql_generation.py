from __future__ import annotations

import json
import os

from ai_sql_agent.generation.coder_client import CoderModelClient, CoderModelConfig
from ai_sql_agent.generation.cot_parser import CotStepParser
from ai_sql_agent.generation.schema_store import LocalSchemaStore
from ai_sql_agent.generation.sql_generator import SqlGenerator


def build_demo_cot_text() -> str:
    """
    构造 Demo CoT 四元组。

    这里模拟上一阶段 CoT Planning 的输出。
    """
    return """步骤1：
(
数据库: erp_db,
处理对象: supplier.on_time_rate，supplier.supplier_name,
操作指令: 在supplier表中筛选on_time_rate低于0.9的记录，输出supplier_name,
输出目标: supplier.supplier_name,
预期输出量级: 数条
)"""


def main() -> None:
    """
    运行 Demo。

    不配置 DASHSCOPE_API_KEY 时走 Mock。
    配置后调用真实 Coder 模型。

    示例：
        export DASHSCOPE_API_KEY="你的APIKey"
        export DASHSCOPE_CODER_MODEL="qwen-plus"
        python examples/sql_generation.py
    """
    cot_text = build_demo_cot_text()

    parser = CotStepParser()
    cot_step = parser.parse_one(cot_text)

    schema_store = LocalSchemaStore.build_demo_store()

    generator = SqlGenerator(
        schema_store=schema_store,
        coder_client=CoderModelClient(
            CoderModelConfig(
                api_key=os.getenv("DASHSCOPE_API_KEY", ""),
                model=os.getenv("DASHSCOPE_CODER_MODEL", "qwen-plus"),
                temperature=0.0,
                use_mock_when_no_api_key=True,
            )
        )
    )

    result = generator.generate(cot_step)

    print("=" * 80)
    print("Coder Prompt")
    print("=" * 80)
    print(result.prompt)

    print("\n" + "=" * 80)
    print("SQL")
    print("=" * 80)
    print(result.sql)

    print("\n" + "=" * 80)
    print("Execution Request")
    print("=" * 80)
    print(
        json.dumps(
            result.to_execution_request(),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
