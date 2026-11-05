from __future__ import annotations

import json

from ai_sql_agent.pipeline import Text2SQLPipeline


def _print_result(result) -> None:
    print("=" * 80)
    print("1. 用户 Query")
    print("=" * 80)
    print(result.query)

    print("\n" + "=" * 80)
    print("2. Schema 检索关键词 / 三层检索结果")
    print("=" * 80)
    print("关键词：" + "，".join(result.keywords))
    print(f"候选数据库：{result.selected_databases}")
    print(f"候选表：{result.selected_tables}")

    print("\n" + "=" * 80)
    print("3. Schema 图")
    print("=" * 80)
    print(result.schema_context)

    print("\n" + "=" * 80)
    print("4. 规划步骤")
    print("=" * 80)
    print(result.cot_output)

    for index, log in enumerate(result.step_logs, start=1):
        print("\n" + "=" * 80)
        print(f"5.{index} 步骤 {index}（数据库：{log.database}）SQL")
        print("=" * 80)
        print(log.sql)

        if log.repair_attempts:
            print("\n--- 修复记录 ---")
            print(json.dumps(log.repair_attempts, ensure_ascii=False, indent=2))

        print("\n" + "=" * 80)
        print(f"6.{index} 步骤 {index} MCP 执行结果")
        print("=" * 80)
        print(
            json.dumps(
                log.execution_result,
                ensure_ascii=False,
                indent=2,
            )
        )


def main() -> None:
    """
    端到端 Demo：单库查询 + 跨库两步查询。

    执行：
        python examples/end_to_end.py
    """
    pipeline = Text2SQLPipeline()

    print("\n" + "#" * 80)
    print("# Demo 1：单库查询")
    print("#" * 80 + "\n")
    _print_result(pipeline.run("查询准时率低于0.9的供应商名称"))

    print("\n" + "#" * 80)
    print("# Demo 2：跨库两步查询（上一步结果回填）")
    print("#" * 80 + "\n")
    _print_result(pipeline.run("查询实际成本超过预算的项目的采购总金额"))


if __name__ == "__main__":
    main()
