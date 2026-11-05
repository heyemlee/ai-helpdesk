"""
A2A 线上格式快照：契约升级之前，帧的种类、顺序、字段集合一个都不许变。

契约（a2a-contract 的 a2a-streaming-contract.md）§4.2 / §4.3 / §4.4 / §4.5 规定了事件顺序、
进度事件的字段和终态的形状，`progress-event.schema.json` 还禁止进度事件带多余字段。
内核重构（运行日志、取消点）不能顺手在帧里加东西——要加，先走契约变更。

这里记的是**形状**：帧的种类与顺序、每层的键集合、stage / step / database 这些契约面的值、
展示文案。ID、时间戳、trace_id、结果行这类每次都不同或与格式无关的值不进快照。

有意的线上格式变更（随契约升级）才重录：

    UPDATE_WIRE_SNAPSHOT=1 python -m pytest tests/test_a2a_wire_snapshot.py
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.api.app import app
from ai_sql_agent.core import get_settings

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "a2a_wire_snapshot.json"

# 单步查询、跨库两步查询
QUESTIONS = (
    "查询准时率低于0.9的供应商名称",
    "查询实际成本超过预算的各个项目的采购总金额",
)

# 进度事件 metadata 里属于契约面的键（契约 §4.5 的附带字段）；其余键只记"存在"
_CONTRACT_METADATA = ("stage", "step", "database", "dialect", "with_memory", "short_path")


def _rpc(method: str, text: str) -> dict:
    return {
        "jsonrpc": "2.0", "id": "req-1", "method": method,
        "params": {"message": {
            "kind": "message", "role": "user", "messageId": "m1",
            # 每次一个新会话：同一会话里问过的问题会被路由到"解释上一条结果"
            "contextId": f"s_wire_{uuid.uuid4().hex[:8]}",
            "parts": [{"kind": "text", "text": text}],
            "metadata": {"user_id": "u_wire"},
        }},
    }


def _data_shape(data: dict) -> dict:
    return {
        "data_keys": sorted(data),
        "step_keys": sorted({key for step in data.get("steps") or [] for key in step}),
    }


def _artifact_shape(artifact: dict) -> dict:
    parts = artifact.get("parts") or []
    shape = {"artifact_keys": sorted(artifact), "parts": [part.get("kind") for part in parts]}
    for part in parts:
        if part.get("kind") == "data":
            shape.update(_data_shape(part["data"]))
    return shape


def _frame_shape(frame: dict) -> dict:
    result = frame["result"]
    kind = result["kind"]
    shape = {"envelope": sorted(frame), "kind": kind, "keys": sorted(result)}
    status = result.get("status") or {}
    if kind == "task":
        shape.update(state=status.get("state"), status_keys=sorted(status))
    elif kind == "status-update":
        meta = result.get("metadata") or {}
        shape.update(
            final=result.get("final"),
            state=status.get("state"),
            status_keys=sorted(status),
            metadata={
                key: (meta[key] if key in _CONTRACT_METADATA else "<present>")
                for key in sorted(meta)
            },
        )
        message = status.get("message")
        if message is not None:
            shape["message_keys"] = sorted(message)
            shape["text"] = [part.get("text") for part in message.get("parts") or []]
    elif kind == "artifact-update":
        shape["lastChunk"] = result.get("lastChunk")
        shape.update(_artifact_shape(result["artifact"]))
    return shape


def record_wire_shapes(client: TestClient) -> dict:
    shapes: dict = {"stream": [], "send": [], "query": []}
    for question in QUESTIONS:
        with client.stream("POST", "/a2a", json=_rpc("message/stream", question),
                           headers={"Accept": "text/event-stream"}) as response:
            response.read()
            frames = [
                json.loads(line[5:].strip())
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
        shapes["stream"].append({
            "question": question,
            "content_type": response.headers["content-type"].split(";")[0],
            "frames": [_frame_shape(frame) for frame in frames],
        })

        body = client.post("/a2a", json=_rpc("message/send", question)).json()
        task = body["result"]
        shapes["send"].append({
            "question": question,
            "envelope": sorted(body),
            "task_keys": sorted(task),
            "state": task["status"]["state"],
            "artifacts": [_artifact_shape(artifact) for artifact in task.get("artifacts") or []],
        })

        reply = client.post("/query", json={
            "query": question, "user_id": "u_wire", "session_id": f"s_wire_{uuid.uuid4().hex[:8]}",
        }).json()
        shapes["query"].append({"question": question, **_data_shape(reply)})
    return shapes


class A2AWireSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        settings = get_settings()
        self._saved_streaming = settings.a2a_streaming
        settings.a2a_streaming = True

    def tearDown(self):
        get_settings().a2a_streaming = self._saved_streaming

    def test_wire_shapes_match_the_recorded_snapshot(self):
        shapes = record_wire_shapes(self.client)

        if os.environ.get("UPDATE_WIRE_SNAPSHOT") == "1":
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT_PATH.write_text(
                json.dumps(shapes, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            self.skipTest(f"已重录 {SNAPSHOT_PATH.name}")

        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.maxDiff = None
        for transport in ("stream", "send", "query"):
            self.assertEqual(
                shapes[transport], expected[transport],
                f"{transport} 的线上格式与快照不同。随契约升级的有意变更才重录（见模块说明）。",
            )


if __name__ == "__main__":
    unittest.main()
