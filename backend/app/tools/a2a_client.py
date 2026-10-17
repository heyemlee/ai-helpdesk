"""
ai-sql-agent 的 A2A 客户端：`message/send` 与 `message/stream`。

契约在 a2a-contract 仓库的 `a2a-streaming-contract.md`（v1.13，tag v1.13 /
commit be35cfb）。这里实现的是契约里「调用方」那一半，几条硬规则原样照搬：

- **唯一的成功判据**（§5.1）：收到 `final:true` 且 `state:"completed"` 的终态事件。
  只收到 artifact 没收到终态、流正常结束但没终态、终态是 failed 或 canceled
  （v1.13 新增）、连接断了——一律不算成功。「半个结果被当成成功」这条路要彻底堵死。
- **超时按路径分档**（§1）：stream 收到任何事件就续命、静默 20s 判死、总长 90s 硬顶；
  send 固定预算、**没有静默规则**——send 全程零事件是设计不是故障。
- **`-32004` 降级**（§6.2）：服务端流式没开时在**流打开之前**返回 -32004，这时改发
  send 不构成配额放大（一个令牌都还没扣）。**流中途**的任何失败都不得重发 send
  ——那时配额已经扣了、模型已经跑了，重发就是实打实的两份（§5.4）。
- **先看 Content-Type**（§3.2）：是 application/json 就是鉴权/限流/参数错，按错误处理，
  不要进 SSE 解析循环。
- **不对事件条数做任何假设**（§4.2）：走没走短路径、修没修 SQL，条数都不一样。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.evidence import normalize_delegation_result

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None | Awaitable[None]]

# 契约 §1 A：流式的自适应预算
STREAM_SILENCE_S = 20.0
STREAM_HARD_S = 90.0
# 契约 §5.4：Agent Card 发现是唯一的 backoff
CARD_TTL_S = 30.0

# 契约 §4.5：stage 值与库标识符都是契约面，调用方自己做中文映射，
# 不要去解析服务端的默认文案字符串——那一列是给没做映射的客户端兜底的，不保证稳定。
STAGE_LABEL = {
    "routing": "正在判断问题类型",
    "retrieval": "正在检索知识库",
    "planning": "正在规划查询步骤",
    "generation": "正在生成查询",
    "execution": "正在查询",
    "repair": "查询出错，正在重试",
    "answering": "正在组织回答",
    # normalization / validation：契约建议不展示（内部动作，用户看不出差别）
}
# 契约 §4.5 在 v1.6 改了：`database` 的契约面是**形状**（`^[a-z][a-z0-9_]*_db$`）
# 加一份可拉取的生效清单，不再是钉死的枚举。两个后果：
#
# 1. **新增标识符不算契约变更。** 所以"遇到不认识的库"是**常规路径不是异常
#    路径**：照常采信，映射退化成显示原始标识符即可（见 progress_label）。
#    `prod_db` / `svc_db` 当初是本仓库单方面加的，v1.6 已追认为合规。
# 2. `mes_db` / `pm_db` 已被契约标为**退役**——`mes_db` 是 `prod_db` 的旧名，
#    `pm_db` 的职责本就在 `erp_db`，两个都**从未在任何部署里存在过**。
#    映射保留着没坏处（benchmark 夹具和历史 trace 里还有），
#    但别指望运行时会出现，也**不要**拿它们去断言线上会发出什么。
#
# 映射表必须覆盖 schema 的 `$defs["known"]`，用例：
# tests/test_contract_fixtures.py::test_chinese_labels_cover_every_known_identifier
DATABASE_LABEL = {
    "erp_db": "ERP 订单库", "wms_db": "WMS 库存库",
    "prod_db": "生产库", "svc_db": "售后服务库",
    # 退役，仅为历史 trace 与 benchmark 夹具保留
    "mes_db": "MES 生产库", "pm_db": "PM 项目库",
}


class A2AError(Exception):
    """A2A 调用失败（非成功判据的一切情况）。"""


class StreamUnsupported(A2AError):
    """服务端在流打开之前返回 -32004：可以改发 send，不算重试。"""


def progress_label(stage: str, database: str | None = None) -> str | None:
    """契约 §4.5 的展示建议：返回 None 表示这个 stage 不展示。"""
    label = STAGE_LABEL.get(stage)
    if label is None:
        return None
    if stage in ("generation", "execution") and database:
        return f"{label}（{DATABASE_LABEL.get(database, database)}）"
    return label


class A2AClient:
    def __init__(self, base_url: str, api_key: str | None = None, *,
                 send_timeout_s: float = 45.0,
                 stream_silence_s: float = STREAM_SILENCE_S,
                 stream_hard_s: float = STREAM_HARD_S):
        self._base = base_url.rstrip("/")
        self._api_key = api_key or None
        self._send_timeout_s = send_timeout_s
        self._silence_s = stream_silence_s
        self._hard_s = stream_hard_s
        self._card: dict[str, Any] | None = None
        self._card_at = 0.0

    # ── Agent Card ────────────────────────────────────────────────────────

    async def card(self) -> dict[str, Any]:
        """30s 缓存（契约 §5.4）。拿不到 Card 时当作不支持流式，走 send。"""
        if self._card is not None and time.monotonic() - self._card_at < CARD_TTL_S:
            return self._card
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self._base}/.well-known/agent.json", headers=self._headers())
                r.raise_for_status()
                self._card = r.json()
        except Exception as ex:
            logger.warning("A2A Agent Card 获取失败，按不支持流式处理: %s", ex)
            self._card = {"capabilities": {"streaming": False}}
        self._card_at = time.monotonic()
        return self._card

    async def streaming_supported(self) -> bool:
        card = await self.card()
        return bool((card.get("capabilities") or {}).get("streaming"))

    # ── message/send ──────────────────────────────────────────────────────

    async def send(self, question: str, user_id: str, session_id: str) -> dict[str, Any]:
        body = self._rpc("message/send", question, user_id, session_id)
        async with httpx.AsyncClient(timeout=self._send_timeout_s) as c:
            r = await c.post(f"{self._base}/a2a", headers=self._headers(), json=body)
        payload = self._parse_json(r)
        task = payload.get("result") or {}
        state = ((task.get("status") or {}).get("state") or "").lower()
        if state != "completed":
            err = ((task.get("metadata") or {}).get("error")) or f"state={state or 'unknown'}"
            raise A2AError(f"message/send 未完成: {err}")
        artifacts = task.get("artifacts") or []
        if not artifacts:
            raise A2AError("message/send 完成但没有 artifact")
        return self._normalize(artifacts[0], task.get("metadata") or {})

    # ── message/stream ────────────────────────────────────────────────────

    async def stream(self, question: str, user_id: str, session_id: str,
                     on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        body = self._rpc("message/stream", question, user_id, session_id)
        headers = {**self._headers(), "Accept": "text/event-stream"}
        deadline = time.monotonic() + self._hard_s
        artifact: dict[str, Any] | None = None
        terminal: dict[str, Any] | None = None
        last_repair_step = 0

        # 连接建立也算进硬上限；读每一行的等待是静默判据
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._silence_s, connect=10.0)) as c:
            async with c.stream("POST", f"{self._base}/a2a", headers=headers, json=body) as r:
                ctype = (r.headers.get("content-type") or "").lower()
                if "text/event-stream" not in ctype:
                    # §3.2：流打开之前的错误，普通 JSON。-32004 → 可以改发 send
                    raw = await r.aread()
                    self._raise_from_json(raw, r.status_code, before_stream=True)

                lines = r.aiter_lines()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise A2AError(f"message/stream 超过总时长硬上限 {self._hard_s}s")
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=min(self._silence_s, remaining))
                    except StopAsyncIteration:
                        break
                    except TimeoutError as ex:
                        raise A2AError(f"message/stream 静默超过 {self._silence_s}s") from ex
                    if not line.startswith("data:"):
                        continue            # 注释行 / 空行；契约说服务端不发心跳，但容忍
                    try:
                        frame = json.loads(line[5:].strip())
                    except json.JSONDecodeError as ex:
                        raise A2AError(f"事件不是合法 JSON: {ex}") from ex
                    if "error" in frame:
                        raise A2AError(f"流中收到错误: {frame['error']}")
                    ev = frame.get("result") or {}
                    kind = ev.get("kind")

                    if kind == "status-update" and ev.get("final"):
                        terminal = ev
                        break
                    if kind == "artifact-update":
                        artifact = ev.get("artifact")
                    elif kind == "status-update":
                        meta = ev.get("metadata") or {}
                        stage = meta.get("stage")
                        # §4.5：repair 的 step 必须严格递增，否则是服务端 bug
                        if stage == "repair":
                            step = int(meta.get("step") or 0)
                            if step <= last_repair_step:
                                logger.warning("A2A repair 事件 step 未递增（%s ≤ %s），按异常记录",
                                               step, last_repair_step)
                            last_repair_step = max(last_repair_step, step)
                        if on_progress and stage:
                            label = progress_label(stage, meta.get("database"))
                            if label:
                                out = on_progress({"stage": stage, "label": label,
                                                   "step": meta.get("step"),
                                                   "database": meta.get("database"),
                                                   "trace_id": meta.get("trace_id")})
                                if asyncio.iscoroutine(out):
                                    await out

        # §5.1：唯一的成功判据
        if terminal is None:
            raise A2AError("流结束但没有终态事件")
        state = ((terminal.get("status") or {}).get("state") or "").lower()
        if state != "completed":
            err = (terminal.get("metadata") or {}).get("error") or state
            raise A2AError(f"终态为 {state}: {err}")
        if artifact is None:
            raise A2AError("终态 completed 但没有收到 artifact")
        return self._normalize(artifact, terminal.get("metadata") or {})

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    @staticmethod
    def _rpc(method: str, question: str, user_id: str, session_id: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": f"req-{int(time.time() * 1000)}", "method": method,
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": f"m-{int(time.time() * 1000)}",
                                       "contextId": session_id,
                                       "parts": [{"kind": "text", "text": question}],
                                       "metadata": {"user_id": user_id}}}}

    def _parse_json(self, r: httpx.Response) -> dict[str, Any]:
        try:
            payload = r.json()
        except Exception as ex:
            raise A2AError(f"响应不是 JSON（HTTP {r.status_code}）") from ex
        if "error" in payload:
            self._raise_error(payload["error"], r.status_code, before_stream=True)
        if r.status_code >= 400:
            raise A2AError(f"HTTP {r.status_code}")
        return payload

    def _raise_from_json(self, raw: bytes, status: int, before_stream: bool) -> None:
        try:
            payload = json.loads(raw or b"{}")
        except Exception as ex:
            raise A2AError(f"HTTP {status}，且响应不是 JSON") from ex
        self._raise_error(payload.get("error") or {"code": 0, "message": f"HTTP {status}"}, status, before_stream)

    @staticmethod
    def _raise_error(err: dict[str, Any], status: int, before_stream: bool) -> None:
        code, msg = err.get("code"), err.get("message", "")
        if before_stream and code == -32004:
            raise StreamUnsupported(msg)
        raise A2AError(f"JSON-RPC 错误 {code}（HTTP {status}）: {msg}")

    @staticmethod
    def _normalize(artifact: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        """
        归一成与直连 /query 客户端一致的字段，上层不用区分走了哪条路。

        **三个信任布尔在这里不做 bool() 转换。** 原先这里写的是
        `bool(data.get("has_risk", False))`——那是契约 §9.1 记的那个缺口本身：
        字段缺失被翻译成"无风险"（fail-open），而 `bool("false")` 又是真。
        校验只在 `core.evidence` 里做一次，三个承载者共用（§4.4.1），见那个模块的说明。
        """
        text, data = "", {}
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                text = part.get("text", "") or text
            elif part.get("kind") == "data":
                data = part.get("data") or {}
        return normalize_delegation_result(
            data, answer=text,
            trace_id_fallback=str(metadata.get("trace_id") or ""),
        )


def a2a_client_from_env(base_url: str, api_key: str | None) -> A2AClient:
    return A2AClient(
        base_url, api_key,
        # 契约 §1 上线顺序：流式真正承接流量之前 send 维持今天的预算；上线后改 90
        send_timeout_s=float(os.getenv("SQL_AGENT_A2A_SEND_TIMEOUT_S", "45")),
        stream_silence_s=float(os.getenv("SQL_AGENT_A2A_SILENCE_S", str(STREAM_SILENCE_S))),
        stream_hard_s=float(os.getenv("SQL_AGENT_A2A_HARD_S", str(STREAM_HARD_S))),
    )
