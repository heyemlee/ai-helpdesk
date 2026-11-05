"""
ai-sql-agent/openapi.json 要和代码一致。

控制台的 TypeScript 类型是从这份快照生成的（frontend/scripts/gen-sql-types.mjs，前端那边另有
一条用例断言生成结果和快照一致）。改了接口却没重新导出，这里失败：

    PYTHONPATH=src:. .venv/bin/python scripts/export_openapi.py
    node ../frontend/scripts/gen-sql-types.mjs
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OpenApiSnapshotTest(unittest.TestCase):
    def test_the_committed_spec_matches_the_code(self):
        spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
        self.assertEqual(
            committed,
            module.render(),
            "openapi.json 和代码对不上：在 ai-sql-agent/ 下跑 scripts/export_openapi.py，"
            "再跑 node ../frontend/scripts/gen-sql-types.mjs 重生成前端类型",
        )


if __name__ == "__main__":
    unittest.main()
