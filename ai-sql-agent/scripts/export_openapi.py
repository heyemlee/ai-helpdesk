"""
把 SQL Agent 的 OpenAPI 规格写进 ai-sql-agent/openapi.json（进 git）。

控制台（frontend/）的 TypeScript 类型从这份文件生成（frontend/scripts/gen-sql-types.mjs）。
tests/test_openapi_snapshot.py 断言它和代码一致：改了接口没重新导出，SQL Agent 这一项 CI 就不过；
重新导出后前端的类型跟着重生成，接口变了的地方前端类型检查会报出来——两边在同一个 PR 里对齐。

    PYTHONPATH=src:. .venv/bin/python scripts/export_openapi.py        # 在 ai-sql-agent/ 下
    node ../frontend/scripts/gen-sql-types.mjs                          # 再生成前端类型

规格只取决于代码：不读 .env，不连库，不调模型。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    from ai_sql_agent.api.app import app

    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    SNAPSHOT.write_text(render(), encoding="utf-8")
    print(f"写入 {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
