"""
开发用 MCP server（scripts/dev_mcp_server.py）：

- 只连开发库：指向生产库时拒绝，报错里不带密码；
- 连接是只读的：写语句由数据库拒绝；
- 走真实的 stdio 协议：客户端列工具、调工具，和 Claude Code 连上来是同一条路。

全部离线，不调模型。要库的那几条在设置了 TEST_POSTGRES_DSN 时跑。
"""
import importlib.util
import json
import os
import pathlib
import sys
import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from app.core.lexical import index_text
from app.events.store import PgEventStore
from app.events.types import Event
from app.storage.pg import Database
from app.storage.vector_store import PgStore

TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()
SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "dev_mcp_server.py"

TOOLS = ["check_turns", "get_turn", "get_turn_events", "list_turns", "search_knowledge"]


def _load_server():
    spec = importlib.util.spec_from_file_location("scripts_dev_mcp_server", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server()


@asynccontextmanager
async def _client(dsn: str):
    """真的起一个 server 进程，走 stdio。环境只给 AIHD_DEV_DSN，外面的 POSTGRES_DSN 带不进去。"""
    params = StdioServerParameters(command=sys.executable, args=[str(SCRIPT)], env={"AIHD_DEV_DSN": dsn})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call(session: ClientSession, tool: str, **args) -> tuple[bool, str]:
    result = await session.call_tool(tool, args)
    return result.isError, result.content[0].text


# ── 只连开发库 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dsn", [
    "postgresql://u:s3cret@127.0.0.1:5433/anything",   # docker-compose.yml 发布的端口
    "postgresql://u:s3cret@127.0.0.1:25432/aihelpdesk",  # docker-compose.yml 默认的库名
])
def test_refuses_the_production_database(monkeypatch, dsn):
    monkeypatch.setenv("AIHD_DEV_DSN", dsn)
    with pytest.raises(ToolError, match="生产库") as err:
        server.dev_dsn()
    assert "s3cret" not in str(err.value)


def test_postgres_dsn_is_not_a_fallback(monkeypatch):
    """backend/.env 的 POSTGRES_DSN 配的是生产库，没配 AIHD_DEV_DSN 时不能退回去用它。"""
    monkeypatch.delenv("AIHD_DEV_DSN", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@127.0.0.1:25432/dev")
    with pytest.raises(ToolError, match="AIHD_DEV_DSN"):
        server.dev_dsn()


def test_dsn_keeps_its_parameters_and_turns_on_read_only(monkeypatch):
    monkeypatch.setenv("AIHD_DEV_DSN", "postgresql://u:p@127.0.0.1:25432/drill?sslmode=disable")
    dsn = server.dev_dsn()
    assert "sslmode=disable" in dsn
    assert dsn.endswith("default_transaction_read_only=on")


def test_clipped_shortens_nested_strings_and_says_how_much_is_missing():
    clipped = server._clipped({"a": ["x" * 10, {"b": "y" * 3}], "n": 5}, 4)
    assert clipped["a"][0].startswith("xxxx…") and "还有 6 字" in clipped["a"][0]
    assert clipped["a"][1]["b"] == "yyy"
    assert clipped["n"] == 5
    assert server._clipped("x" * 10, 0) == "x" * 10


# ── stdio：不要库的那部分 ─────────────────────────────────────────────────────


async def test_tools_are_listed_and_all_marked_read_only():
    async with _client("postgresql://u:s3cret@127.0.0.1:1/dev") as session:
        tools = (await session.list_tools()).tools
    assert sorted(t.name for t in tools) == TOOLS
    assert all(t.annotations.readOnlyHint for t in tools)


async def test_production_and_unreachable_databases_are_tool_errors_without_the_password():
    async with _client("postgresql://u:s3cret@127.0.0.1:5433/aihelpdesk") as session:
        is_error, text = await _call(session, "list_turns")
    assert is_error and "生产库" in text and "s3cret" not in text

    # 库没起来（演练栈没开）是常态：server 照常活着，工具报错并说怎么起
    async with _client("postgresql://u:s3cret@127.0.0.1:1/dev") as session:
        is_error, text = await _call(session, "get_turn", turn_id="x")
    assert is_error and "连不上开发库 127.0.0.1:1/dev" in text and "deploy/drill" in text
    assert "s3cret" not in text


async def test_unknown_event_type_is_refused_before_touching_the_database():
    async with _client("postgresql://u:p@127.0.0.1:1/dev") as session:
        is_error, text = await _call(session, "get_turn_events", turn_id="x", types=["typo_event"])
    assert is_error and "typo_event" in text and "model_call" in text


# ── 真实的库 ─────────────────────────────────────────────────────────────────


def _event(turn_id: str, seq: int, instance: str, conv_id: str, type_: str, ts: float, data=None, body=None) -> Event:
    return Event(turn_id=turn_id, seq=seq, instance=instance, conv_id=conv_id, request_id="r", type=type_,
                 agent="", step=None, call_id="", ts=ts, t_ms=float(seq), v=1, data=json.dumps(data or {}),
                 body=json.dumps(body) if body is not None else None)


def _turn(turn_id: str, instance: str, conv_id: str, ts: float, outcome: str) -> list[Event]:
    return [
        _event(turn_id, 0, instance, conv_id, "turn_started", ts, {"transport": "chat"},
               {"user_id": "u", "message": f"{conv_id} 的问题"}),
        _event(turn_id, 1, instance, conv_id, "agent_started", ts + 1, {"agent_type": "general"},
               {"system": "s" * 5000}),
        _event(turn_id, 2, instance, conv_id, "turn_completed", ts + 2,
               {"outcome": outcome, "agent_type": "general", "handoff": outcome == "handoff", "latency_ms": 12.0}),
    ]


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_writes_are_refused_by_the_database(monkeypatch):
    setup = Database(TEST_PG_DSN)
    await setup.connect(embedding_dim=256)
    await setup.close()

    # DSN 里写了相反的值也一样：只读那一项排在最后
    separator = "&" if "?" in TEST_PG_DSN else "?"
    monkeypatch.setenv("AIHD_DEV_DSN", f"{TEST_PG_DSN}{separator}default_transaction_read_only=off")
    async with server._connected() as db:
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await db.execute("DELETE FROM turn_events WHERE turn_id = 'nothing'")


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_tools_read_recorded_turns_over_stdio():
    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=256)
    # 共用的测试库里可能有别的数据：实例名和关键词每次换一个
    tag = uuid.uuid4().hex[:8]
    instance, other = f"staff-{tag}", f"customer-{tag}"
    chunk_id, keyword = f"chunk-{tag}", f"probe{tag}"
    now = time.time()
    older, newer = f"turn-a-{tag}", f"turn-b-{tag}"
    try:
        store = PgEventStore(db, instance)
        await store.insert(_turn(older, instance, "conv-a", now - 60, "handoff"))
        await store.insert(_turn(newer, instance, "conv-b", now - 30, "answered"))
        await PgEventStore(db, other).insert(_turn(f"turn-c-{tag}", other, "conv-a", now - 30, "answered"))
        from evidence_fixtures import PUBLISHED
        await PgStore(db).kb_doc_add(dict(id=f"doc-{tag}",title="探针文档",content=f"这一片里有 {keyword}",
            content_hash=tag,created_at=now,policy=PUBLISHED,policy_revision="fixture-v1"))
        await PgStore(db).kb_add([{
            "id": chunk_id, "title": "探针文档", "content": f"这一片里有 {keyword}", "chunk_index": 0,
            "total_chunks": 1, "ingested_at": now, "embedding": [0.1] * 256,
            "tokens": index_text("探针文档", f"这一片里有 {keyword}"), "doc_id": f"doc-{tag}",
        }])

        async with _client(TEST_PG_DSN) as session:
            # 最近的轮次，新的在前，带上用户原话；database 里不带账号密码
            is_error, text = await _call(session, "list_turns", instance=instance, hours=1)
            assert not is_error
            listed = json.loads(text)
            assert [t["turn_id"] for t in listed["turns"]] == [newer, older]
            assert listed["turns"][1]["message"] == "conv-a 的问题"
            assert "@" not in listed["database"]

            is_error, text = await _call(session, "list_turns", instance=instance, hours=1, outcome="handoff")
            assert [t["turn_id"] for t in json.loads(text)["turns"]] == [older]

            is_error, text = await _call(session, "list_turns", instance=instance, conv_id="conv-b")
            assert [t["turn_id"] for t in json.loads(text)["turns"]] == [newer]

            # 实例隔离：同名会话、别的实例的轮次都读不到
            is_error, text = await _call(session, "list_turns", instance=other, conv_id="conv-b")
            assert json.loads(text)["turns"] == []
            is_error, text = await _call(session, "get_turn", turn_id=older, instance=other)
            assert is_error and "没有这一轮" in text

            is_error, text = await _call(session, "get_turn", turn_id=older, instance=instance)
            assert not is_error and json.loads(text)["complete"] is True

            # 按类型筛，长字符串默认截短，max_chars=0 看全文
            is_error, text = await _call(session, "get_turn_events", turn_id=older, instance=instance,
                                         types=["agent_started"], max_chars=100)
            events = json.loads(text)
            assert events["total_events"] == 3
            assert [e["type"] for e in events["events"]] == ["agent_started"]
            assert "还有 4900 字" in events["events"][0]["body"]["system"]
            is_error, text = await _call(session, "get_turn_events", turn_id=older, instance=instance,
                                         types=["agent_started"], max_chars=0)
            assert json.loads(text)["events"][0]["body"]["system"] == "s" * 5000

            is_error, text = await _call(session, "check_turns", instance=instance, hours=1)
            report = json.loads(text)
            assert report["turns"] == 2 and report["complete"] == 2
            assert report["no_terminal"] == [] and report["seq_gaps"] == []

            is_error, text = await _call(session, "search_knowledge", query=keyword)
            results = json.loads(text)["results"]
            assert [r["chunk_id"] for r in results] == [chunk_id]
            assert results[0]["doc_id"] == f"doc-{tag}"
    finally:
        await db.execute("DELETE FROM turn_events WHERE instance = ANY($1::text[])", [instance, other])
        await db.execute("DELETE FROM policy_chunks WHERE id = $1", chunk_id)
        await db.close()
