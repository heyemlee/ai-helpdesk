"""
录控制台（frontend/ 的数据页面）用的测试夹具：在进程内起 SQL Agent，按页面的调用顺序请求一遍，
把响应原样存进 frontend/src/test/fixtures/sql/。

为什么录而不是手写：数据浏览、运行记录这几组接口的响应在 OpenAPI 里只是 `object`（处理函数
返回 dict），手写的夹具会悄悄和服务端走样，页面和测试一起"通过"一个不存在的格式。

全程离线：
- 用内置的演示库和演示元数据（AI_SQL_SEMANTIC_DIR 清空），不碰任何真实业务库；
- 模型凭据全部清空、页面保存的模型连接关掉，链路走 Mock，不发一次模型请求；
- 审计、运行记录、对话记忆都写进临时目录，跑完就删；
- 不读 ai-sql-agent/.env（关掉 pydantic-settings 的 env_file）。

    PYTHONPATH=src:. .venv/bin/python scripts/record_console_fixtures.py   # 在 ai-sql-agent/ 下

时间戳、trace_id、run_id 每次录都不一样，夹具重录后测试应当照样通过——测试只认形状和关系
（比如历史里的 run_id 能在运行记录里找到），不认具体的值。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "src" / "test" / "fixtures" / "sql"
KEY = "console-fixture-key"


def _isolate(tmp: Path) -> None:
    (tmp / "keys.json").write_text(json.dumps({
        "keys": [{"key": KEY, "caller": "console", "user_id": "console",
                  "roles": ["admin"], "is_admin": True}],
        "policies": {"admin": {"allow_tables": ["*"]}},
    }), encoding="utf-8")
    for name in ("AI_SQL_OPENAI_API_KEY", "OPENAI_API_KEY", "AI_SQL_ANTHROPIC_API_KEY",
                 "ANTHROPIC_API_KEY", "AI_SQL_PGVECTOR_DSN"):
        os.environ[name] = ""
    os.environ.update({
        "AI_SQL_MODEL_SETTINGS_ENABLED": "false",
        "AI_SQL_AUTH_MODE": "api_key",
        "AI_SQL_API_KEYS_FILE": str(tmp / "keys.json"),
        "AI_SQL_AUDIT_DB_PATH": str(tmp / "audit.db"),
        "AI_SQL_MEMORY_DB_PATH": str(tmp / "memory.db"),
        "AI_SQL_RUNS_DB_PATH": str(tmp / "runs.db"),
        "AI_SQL_SEMANTIC_DIR": "",
    })
    from ai_sql_agent.core.settings import SqlAgentSettings

    SqlAgentSettings.model_config["env_file"] = None
    from ai_sql_agent.core import reload_settings

    reload_settings()


def _save(name: str, payload) -> None:
    path = OUT / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}")


def _ok(response, status: int = 200):
    if response.status_code != status:
        raise SystemExit(f"{response.request.method} {response.request.url} → {response.status_code}: "
                         f"{response.text[:300]}")
    return response


def _stream(client, question: str, headers: dict) -> str:
    body = {
        "jsonrpc": "2.0", "id": "fixture-1", "method": "message/stream",
        "params": {"message": {
            "kind": "message", "role": "user", "messageId": "fixture-message-1",
            "contextId": "s-fixture", "parts": [{"kind": "text", "text": question}],
            "metadata": {"user_id": "console"},
        }},
    }
    with client.stream("POST", "/a2a", json=body,
                       headers={**headers, "Accept": "text/event-stream"}) as response:
        _ok(response)
        return "".join(response.iter_text())


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sql-console-fixtures-"))
    try:
        _isolate(tmp)
        from fastapi.testclient import TestClient

        from ai_sql_agent.api.app import app

        OUT.mkdir(parents=True, exist_ok=True)
        auth = {"X-API-Key": KEY}
        with TestClient(app) as client:
            print("录制到", OUT.relative_to(ROOT))
            _save("health.json", _ok(client.get("/health")).json())
            _save("readyz.json", _ok(client.get("/readyz")).json())
            _save("whoami.json", _ok(client.get("/whoami", headers=auth)).json())
            _save("catalog.json", _ok(client.get("/catalog", headers=auth)).json())
            _save("table.json", _ok(client.get("/catalog/erp_db/sales_order", headers=auth)).json())
            _save("rows.json", _ok(client.post("/catalog/erp_db/sales_order/rows", headers=auth,
                                               json={"page": 1, "page_size": 25})).json())
            _save("rows-filtered.json", _ok(client.post(
                "/catalog/erp_db/sales_order/rows", headers=auth,
                json={"page": 1, "page_size": 25, "search": "",
                      "filters": [{"column": "order_amt", "op": "gt", "value": 100000}],
                      "sort": {"column": "order_amt", "direction": "desc"}})).json())
            _save("profile.json", _ok(client.post("/catalog/erp_db/sales_order/profile",
                                                  headers=auth, json={})).json())
            _save("rows-error.json", client.post(
                "/catalog/erp_db/sales_order/rows", headers=auth,
                json={"page": 1, "page_size": 25,
                      "filters": [{"column": "no_such_column", "op": "eq", "value": "x"}]}).json())

            # 两次提问：一次走 /query（拿 X-Run-Id），一次走 A2A 流（taskId 就是 run_id）
            query = _ok(client.post("/query", headers=auth, json={
                "query": "查询准时率低于0.9的供应商名称", "user_id": "console", "session_id": "s-fixture"}))
            _save("query.json", {"run_id": query.headers.get("X-Run-Id", ""), "response": query.json()})
            run_id = query.headers.get("X-Run-Id", "")
            _save("run.json", _ok(client.get(f"/runs/{run_id}", headers=auth)).json())
            _save("run-events.json", _ok(client.get(f"/runs/{run_id}/events", headers=auth)).json())
            _save("a2a-stream.sse", _stream(client, "List the sales orders signed this year", auth))

            _save("history.json", _ok(client.get("/history", headers=auth,
                                                 params={"limit": 50})).json())
            _save("feedback-stats.json", _ok(client.get("/feedback/stats", headers=auth)).json())
            _save("schema-status.json", _ok(client.get("/admin/schema/status", headers=auth)).json())
            _save("unauthorized.json", client.get("/history").json())
            _save("run-not-found.json", client.get("/runs/0000000000000000", headers=auth).json())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
