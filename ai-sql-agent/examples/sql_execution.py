from __future__ import annotations

import json
from pathlib import Path

from ai_sql_agent.execution import MCPRouter, SQLiteMCPExecutor

from ai_sql_agent.pipeline.demo_data import create_erp_demo_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "runtime_data" / "erp_demo.db"


def main() -> None:
    """
    最小 SQL 执行环境 Demo。

    这个 Demo 不依赖大模型，也不依赖 Schema 检索，
    只用于验证：
    1. SQLite 测试库可以创建。
    2. MCP 路由可以把 SQL 发到 erp_db 执行。
    3. 当前示例 SQL 可以查出结果。
    """
    db_path = create_erp_demo_database(DB_PATH)

    router = MCPRouter()
    router.register_executor(
        database="erp_db",
        executor=SQLiteMCPExecutor(
            database="erp_db",
            db_path=db_path,
            readonly=True,
        ),
    )

    sql = """
    SELECT supplier.supplier_name, purchase_order.po_amt
    FROM purchase_order
    JOIN supplier
      ON purchase_order.supplier_id = supplier.supplier_id
    WHERE supplier.on_time_rate < 0.9;
    """

    request = {
        "database": "erp_db",
        "sql": sql,
    }

    result = router.execute(request)

    print("=" * 80)
    print("MCP 执行请求")
    print("=" * 80)
    print(json.dumps(request, ensure_ascii=False, indent=2))

    print("\n" + "=" * 80)
    print("MCP 执行结果")
    print("=" * 80)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
