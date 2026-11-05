from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ai_sql_agent.audit import AuditRecord
from ai_sql_agent.core import get_settings, get_trace_id, log_event
from ai_sql_agent.core.logging_setup import get_logger
from ai_sql_agent.core.run_context import RunCancelled, RunContext, current_run
from ai_sql_agent.core.trace import use_stage_sink
from ai_sql_agent.runs import runs_of
from ai_sql_agent.runs.disconnect import watch_disconnect

# ai_sql_agent.api 在导入本包（app.include_router），所以这里的反向导入必须延迟到调用时，
# 否则是循环导入。函数级 import 在这里不是坏味道，是方向使然。

_logger = get_logger("a2a")
router = APIRouter()

PROTOCOL_VERSION = "0.2"
CARD_VERSION = "1.1.0"          # 契约 §6.1：从 1.0.0 提到 1.1.0 作为「有了流式」的标记

# JSON-RPC 错误码。-32004 是 A2A 规范的 UnsupportedOperationError，
# 调用方读到它就退回 message/send（契约 §6.2 的回滚路径）。
E_PARSE, E_INVALID_REQUEST, E_METHOD_NOT_FOUND, E_INVALID_PARAMS = -32700, -32600, -32601, -32602
E_UNSUPPORTED = -32004
E_INTERNAL = -32603
# 限流、配额、流式并发准入满了：HTTP 429 + -32029（契约 v1.13 §3.2 / §5.5）。
# v1.13 之前用的是 -32001，可它在 A2A 规范里是 TaskNotFoundError——将来支持任何
# tasks/* 方法，同一个码就会有两种意思。-32029 落在服务端自定义的 -32000..-32099 里，
# 也避开了规范已经占用的 -32001..-32007。
E_RATE_LIMITED = -32029

# ── 契约 §4.5：stage 映射表 ──────────────────────────────────────────────────
#
# 契约面是右边这一列，不是 pipeline 内部的名字。内部把 sql_generation 改名不影响
# 契约；改这张表就是一次契约变更。memory_context 刻意不发（内部管道，对用户没有
# 展示价值）。
#
# normalization 从 2026-09-04 起是真 stage（`text2sql_pipeline` 里那条裸
# log_event 换成了 `stage()`），九个契约 stage 现在全发——契约 §4.5 的缺口框
# 说的「契约说全发、实际不全发」到此关闭。
_STAGE_MAP = {
    "normalization": "normalization",
    "routing": "routing",
    "retrieval": "retrieval",
    "planning": "planning",
    "sql_generation": "generation",
    "execution": "execution",
    "repair": "repair",
    "validation": "validation",
    "data_qa": "answering",
}
_STAGE_TEXT = {
    "normalization": "正在理解问题",
    "routing": "正在判断问题类型",
    "retrieval": "正在检索知识库",
    "planning": "正在规划查询步骤",
    "generation": "正在生成查询",
    "execution": "正在查询数据库",
    "repair": "查询出错，正在重试",
    "validation": "正在校验结果",
    "answering": "正在组织回答",
}
# 进度事件只带进入 stage 时就已知的字段（契约 §4.5「附带字段」的诚实说明）。
_STAGE_FIELDS = ("step", "database", "dialect", "with_memory", "short_path")


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, http_status: int = 200,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.code, self.message, self.http_status, self.headers = code, message, http_status, headers or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_response(req_id: Any, err: JsonRpcError) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": req_id, "error": {"code": err.code, "message": err.message}}
    return JSONResponse(body, status_code=err.http_status, headers=err.headers)


# ── Agent Card ────────────────────────────────────────────────────────────────

DATABASE_ID_PATTERN = "^[a-z][a-z0-9_]*_db$"
"""
契约 §4.5（v1.6 起）：`database` 的契约面是**形状**，不是钉死的枚举。

改这个正则是一次契约变更。清单（下面那个函数）则不是：新增一个标识符不算变更，
删除 / 改名才算——风险不对称，流程就不对称。
"""


def active_database_manifest(app=None) -> list:
    """
    当前生效的业务库清单。

    契约 §4.5 要求这份清单是**可拉取的**，别让调用方靠猜、靠读我方启动日志。
    它不是代码里的常量，是部署配置的产物：语义层目录（AI_SQL_SEMANTIC_DIR）里
    放几个 yaml 就有几个库。所以这里从**运行时真正加载的那份语义层**读，
    不是从一张手写表读——手写表和部署对不上正是 v1.6 修的那个问题。
    """
    pipeline = getattr(getattr(app, "state", None), "pipeline", None)
    if pipeline is None:
        from ai_sql_agent.semantic import active_semantic_layer

        return sorted(active_semantic_layer().database_names())
    return sorted(database.name for database in pipeline.databases)


def build_agent_card(base_url: str, app=None) -> Dict[str, Any]:
    settings = get_settings()
    return {
        "name": "ai-sql-agent",
        "description": "把自然语言问题翻成只读 SQL 去业务库里查，返回答案与执行的 SQL。",
        "url": f"{base_url.rstrip('/')}/a2a",
        "version": CARD_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            # 由开关决定（契约 §6.2）。关 = message/stream 返回 -32004。
            "streaming": bool(settings.a2a_streaming),
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [{
            "id": "text2sql",
            "name": "业务数据查询",
            "description": "订单、生产、库存、账款等业务问题，查真实数据后回答。",
            "tags": ["sql", "erp", "orders"],
        }],
        # 契约 §4.5（v1.6）：形状 + 一份可拉取的生效清单。
        # 调用方拿它做中文映射；**清单外的标识符不得崩溃**，退化成显示原始值即可，
        # 因为新增一个库是运维动作，不走契约变更。
        "databases": active_database_manifest(app),
        "databaseIdPattern": DATABASE_ID_PATTERN,
    }


@router.get("/.well-known/agent.json", include_in_schema=False)
def agent_card(request: Request) -> Dict[str, Any]:
    return build_agent_card(str(request.base_url), request.app)


@router.get("/a2a/databases", include_in_schema=True)
def database_manifest(request: Request) -> Dict[str, Any]:
    """
    生效中的业务库清单（契约 §4.5）。

    Agent Card 里已经带了同一份，这个端点是给「只想拉清单、不想解析整张 Card」
    的调用方用的。两处同源，不会各说各话。
    """
    return {
        "databases": active_database_manifest(request.app),
        "pattern": DATABASE_ID_PATTERN,
        "note": (
            "新增标识符不算契约变更，调用方必须容忍不认识的标识符"
            "（退化为显示原始值）；删除或改名算，走契约 §6。"
        ),
    }


# ── 请求解析与准入（流打开之前）──────────────────────────────────────────────

@dataclass
class ParsedMessage:
    query: str
    session_id: str
    user_id: str
    message_id: str


def _parse_message(params: Any) -> ParsedMessage:
    """
    `params.message` 的解析规则，send 与 stream 完全一致（契约 §3.1）：
    parts 里的 text 拼接、contextId 当会话、metadata.user_id 当身份。
    """
    if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
        raise JsonRpcError(E_INVALID_PARAMS, "params.message 缺失或不是对象")
    msg = params["message"]
    parts = msg.get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    query = "\n".join(t for t in texts if t).strip()
    if not query:
        raise JsonRpcError(E_INVALID_PARAMS, "message.parts 里没有任何 text part")
    meta = msg.get("metadata") or {}
    return ParsedMessage(
        query=query,
        session_id=str(msg.get("contextId") or "").strip() or uuid.uuid4().hex[:12],
        user_id=str(meta.get("user_id") or "").strip() or "anonymous",
        message_id=str(msg.get("messageId") or uuid.uuid4().hex),
    )


def _admit(request: Request, user_id: str):
    """
    鉴权 + 限流，**一律发生在流打开之前**（契约 §3.2 / §5.5）。
    失败返回普通 JSON-RPC 错误，不是 SSE——调用方不该为了知道「我没通过鉴权」
    而去解析一个事件流。
    """
    from ai_sql_agent.api.security import require_api_key

    try:
        record = require_api_key(request)
        principal = request.app.state.auth.resolve_principal(record, user_id)
        request.app.state.limiter.check(principal)
        return principal
    except HTTPException as exc:
        headers = {k: v for k, v in (exc.headers or {}).items()}
        if exc.status_code in (401, 403):
            code = E_INVALID_REQUEST
        elif exc.status_code == 429:
            code = E_RATE_LIMITED
        else:
            code = E_INTERNAL
        raise JsonRpcError(code, str(exc.detail), http_status=exc.status_code, headers=headers) from exc


# ── 事件构造（契约 §4）───────────────────────────────────────────────────────

def _envelope(req_id: Any, result: Dict[str, Any]) -> str:
    return "data: " + json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                                 ensure_ascii=False) + "\n\n"


def _task_frame(task_id: str, context_id: str, state: str) -> Dict[str, Any]:
    return {"kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": state, "timestamp": _now()}}


def _progress_frame(task_id: str, context_id: str, stage: str, fields: Dict[str, Any],
                    trace_id: str) -> Dict[str, Any]:
    text = _STAGE_TEXT.get(stage, stage)
    db = fields.get("database")
    if stage in ("generation", "execution") and db:
        text = f"{text}（{db}）"
    metadata = {"stage": stage, "trace_id": trace_id}
    for key in _STAGE_FIELDS:
        if key in fields and fields[key] is not None:
            metadata[key] = fields[key]
    return {
        "kind": "status-update", "taskId": task_id, "contextId": context_id, "final": False,
        "status": {"state": "working", "timestamp": _now(),
                   "message": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                               "taskId": task_id, "contextId": context_id,
                               "parts": [{"kind": "text", "text": text}]}},
        "metadata": metadata,
    }


def _artifact(task_id: str, summary, trace_id: str) -> Dict[str, Any]:
    """
    结果载荷：文本 part（答案）+ data part。send 与 stream **完全同构**（契约 §4.4）。

    data part 的字段集来自 `ResultSummary.data_part()`，send 与 stream 都经本函数
    调它——这两条的同构不是靠两边都记得改，是靠只有一份。

    `/query` 不走这里，也不调 `data_part()`：它同样先 `summarize_result()` 得到
    `ResultSummary`，再在 `ai_sql_agent.api.app` 的 `query()` 里逐字段填
    `QueryResponse`。那一侧的对齐靠 `tests/test_business_outcome.py` 守着。
    """
    return {
        "artifactId": uuid.uuid4().hex, "name": "answer",
        "parts": [
            {"kind": "text", "text": summary.answer},
            {"kind": "data", "data": {**summary.data_part(), "trace_id": trace_id}},
        ],
    }


def _artifact_frame(task_id: str, context_id: str, artifact: Dict[str, Any]) -> Dict[str, Any]:
    return {"kind": "artifact-update", "taskId": task_id, "contextId": context_id,
            "artifact": artifact, "lastChunk": True}


def _terminal_frame(task_id: str, context_id: str, state: str, trace_id: str,
                    error: str = "") -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"trace_id": trace_id}
    if error:
        metadata["error"] = error
    frame = {"kind": "status-update", "taskId": task_id, "contextId": context_id, "final": True,
             "status": {"state": state, "timestamp": _now()}, "metadata": metadata}
    return frame


# ── 执行 ─────────────────────────────────────────────────────────────────────

def _run_and_audit(request: Request, principal, parsed: ParsedMessage, started: float):
    """同步跑 pipeline 并写审计。一次 stream = 一次 /query = 一条审计（契约 §5.5）。"""
    from ai_sql_agent.api.result_summary import (
        apply_summary_to_audit,
        run_outcome,
        summarize_result,
        write_audit,
    )

    run = current_run()
    audit = AuditRecord(trace_id=get_trace_id(), caller=principal.caller,
                        user_id=principal.user_id, session_id=parsed.session_id, query=parsed.query,
                        run_id=run.run_id if run is not None else "")
    try:
        result = request.app.state.service.run(
            user_id=principal.user_id, session_id=parsed.session_id, query=parsed.query,
            enable_long_term=False, access=principal.policy,
        )
    except Exception as exc:
        audit.success = False
        if isinstance(exc, RunCancelled):
            # 取消不是故障：499（客户端关闭请求），配额照计（契约 §5.5）
            audit.status_code, audit.error = 499, str(exc)
        else:
            audit.status_code = 400 if isinstance(exc, ValueError) else 500
            audit.error = f"{type(exc).__name__}: {exc}"
        write_audit(request.app, audit, started, principal)
        raise
    summary = summarize_result(result)
    apply_summary_to_audit(summary, audit)
    write_audit(request.app, audit, started, principal)
    if run is not None:
        run.set_outcome(**run_outcome(summary))
    return summary


def _start_run(request: Optional[Request], principal, parsed: ParsedMessage, task_id: str,
               entry: str) -> Optional[RunContext]:
    """
    登记这次 run，run_id 就用 taskId（值仍是 uuid4 hex，线上看不出区别）。

    先登记、后执行：在发首帧之前登记，调用方拿到 taskId 立刻取消也找得到。
    没有 RunManager 时（单测直接调 `_stream_events`、request 为 None）返回 None，一切照旧。
    """
    runs = runs_of(getattr(request, "app", None))
    if runs is None:
        return None
    return runs.start(entry=entry, caller=principal.caller, user_id=principal.user_id,
                      session_id=parsed.session_id, query=parsed.query, run_id=task_id)


def _running(request: Optional[Request], run: Optional[RunContext]):
    runs = runs_of(getattr(request, "app", None))
    return runs.running(run) if runs is not None and run is not None else nullcontext()


def _run_bound(request: Request, run: Optional[RunContext], principal, parsed: ParsedMessage,
               started: float):
    """在 run 里跑 `_run_and_audit`（message/send 的工作线程入口）。"""
    with _running(request, run):
        return _run_and_audit(request, principal, parsed, started)


def _task_result(task_id: str, context_id: str, state: str, artifact: Optional[Dict[str, Any]],
                 trace_id: str, error: str = "") -> Dict[str, Any]:
    """message/send 的返回：一个完整 Task。"""
    task = {"kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": state, "timestamp": _now()},
            "artifacts": [artifact] if artifact else [],
            "metadata": {"trace_id": trace_id, **({"error": error} if error else {})}}
    return task


async def _handle_send(request: Request, req_id: Any, params: Any) -> JSONResponse:
    parsed = _parse_message(params)
    principal = _admit(request, parsed.user_id)
    task_id, trace_id, started = uuid.uuid4().hex, get_trace_id(), time.perf_counter()
    run = _start_run(request, principal, parsed, task_id, "a2a_send")
    work = asyncio.to_thread(_run_bound, request, run, principal, parsed, started)
    runs = runs_of(request.app)
    if run is not None and runs is not None and get_settings().cancel_on_disconnect:
        # 断开即取消（默认关，契约 §5.4）：调用方走了就在下一个检查点停
        work = watch_disconnect(request, work,
                                lambda: runs.cancel(run.run_id, "client_disconnected"))
    try:
        summary = await work
    except ValueError as exc:
        raise JsonRpcError(E_INVALID_PARAMS, str(exc)) from exc
    except RunCancelled as exc:
        # 契约 v1.13 §4.4：被取消就是 canceled，不是 failed。调用方自己断开时这份响应发不出去，
        # 能收到它的只有「被本连接以外的途径取消」（服务端取消接口、运维、关停）这一种情况。
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": _task_result(
            task_id, parsed.session_id, "canceled", None, trace_id, error=str(exc))})
    except Exception as exc:
        log_event(_logger, logging.ERROR, "a2a.send_failed", error=f"{type(exc).__name__}: {exc}")
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": _task_result(
            task_id, parsed.session_id, "failed", None, trace_id, error=str(exc) or type(exc).__name__)})
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": _task_result(
        task_id, parsed.session_id, "completed", _artifact(task_id, summary, trace_id), trace_id)})


# ── 流式的执行资源（契约 §5.5 的准入 + 背压）──────────────────────────────────
#
# 复现过的饥饿：生产者用 `asyncio.to_thread`、消费者用
# `run_in_executor(None, events.get)`，两者**抢同一个默认线程池**。把默认池限成
# 2 线程、开 2 条流，两个消费者就把池占满，pipeline 启动数为 0。
#
# 这里改两件事，缺一不可：
#   1. pipeline 走**专用**有界线程池，不再和别人抢默认池；
#   2. 消费者**根本不占线程**——事件从工作线程用 `call_soon_threadsafe` 推进
#      `asyncio.Queue`，事件循环自己就能等，不需要一个线程站在那里阻塞式 get。
# 只做 1 不做 2 的话，消费者仍然会把默认池吃光，症状换个地方出现。

_executor_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None
_executor_size = 0

_admissions: "weakref.WeakKeyDictionary[Any, _Admission]" = weakref.WeakKeyDictionary()


class _Admission:
    """
    入口并发准入的计数器。

    刻意**不用 `asyncio.Semaphore`**：它没有非阻塞获取，而
    `if locked(): reject else: await acquire()` 在两步之间有竞争——真挤上了就变成
    排队，而排队正是这里要避免的（契约 §5.5 要求满了在流打开之前返回 429）。
    事件循环是单线程的，一个整数计数器既够用又没有那个缝。
    """

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.active = 0

    def try_acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


def _stream_executor() -> ThreadPoolExecutor:
    """跑同步 pipeline 的专用线程池。池大小变了就重建，方便测试与热调。"""
    global _executor, _executor_size

    size = max(1, get_settings().a2a_stream_worker_threads)
    with _executor_lock:
        if _executor is None or _executor_size != size:
            old = _executor
            _executor = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="a2a-stream"
            )
            _executor_size = size
            if old is not None:
                # 不等在跑的活干完：它们跑完自然退出，这里只是不再收新活
                old.shutdown(wait=False)
        return _executor


def _admission() -> "_Admission":
    """
    入口并发准入，**按事件循环各持一个**（测试里每个 TestClient 一个循环，
    计数不该串台）。上限变了就换一个新的。
    """
    loop = asyncio.get_running_loop()
    limit = max(1, get_settings().a2a_stream_max_concurrent)
    admission = _admissions.get(loop)
    if admission is None or admission.limit != limit:
        admission = _Admission(limit)
        _admissions[loop] = admission
    return admission


def shutdown_stream_executor() -> None:
    """进程退出时收摊；由 ai_sql_agent.api.app 的 shutdown 调用。"""
    global _executor, _executor_size

    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None
            _executor_size = 0


async def _stream_events(request: Request, req_id: Any, principal, parsed: ParsedMessage,
                         task_id: str, permit: "_Admission") -> AsyncIterator[str]:
    """
    事件顺序（契约 §4.2，**保证**）：
        1× task → N× status-update(working) → 1× artifact-update → 1× status-update(final)
    流中途出错不靠断连表示失败：补一个 failed 终态再关（§5.2）。
    不发心跳、不发空事件（§3.4）：静默本身就是信息。
    """
    trace_id, context_id, started = get_trace_id(), parsed.session_id, time.perf_counter()
    loop = asyncio.get_running_loop()

    # 有界队列 = 背压。以前是无界 `queue.Queue`：客户端读得慢时事件只在内存里堆着，
    # 堆了多少谁都不知道。满了就丢**进度**事件——契约 §4.2 明说「不要对事件条数
    # 做任何假设」，进度是附加物；终态和载荷绝不丢，见 `_push`。
    events: "asyncio.Queue[Tuple[str, Any, Any]]" = asyncio.Queue(
        maxsize=max(8, get_settings().a2a_stream_queue_size)
    )
    dropped = {"count": 0}
    closed = threading.Event()

    def _push(item: Tuple[str, Any, Any], *, droppable: bool) -> None:
        def _put() -> None:
            try:
                events.put_nowait(item)
            except asyncio.QueueFull:
                if droppable:
                    dropped["count"] += 1
                    return
                # 终态挤不进去就腾位置：丢一条进度换一个终态，永远划算
                try:
                    events.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    events.put_nowait(item)
                except asyncio.QueueFull:  # pragma: no cover - 理论上到不了
                    pass

        if closed.is_set():
            return
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # 循环已经关了：客户端早断开了，没人接收，静默丢掉即可
            pass

    def sink(name: str, fields: Dict[str, Any]) -> None:
        stage = _STAGE_MAP.get(name)
        if stage is not None:              # memory_context 等内部 stage 刻意不发
            _push(("stage", stage, fields), droppable=True)

    # 先登记、后执行：首帧发出之前 run 已经在登记表里。
    # 落库是一次很短的同步写入，和 `_admit` 读配额一样直接在事件循环里做：放进默认线程池
    # 会和 message/send 的 pipeline 抢线程，正是 §5.5 那次饥饿的老路。
    run_ctx = _start_run(request, principal, parsed, task_id, "a2a_stream")

    def work():
        # contextvars 不会自动跟着 run_in_executor 走（asyncio.to_thread 会，
        # 这里不是）。trace_id、用量上下文、授权作用域都在 context 里，必须显式带过去。
        with use_stage_sink(sink), _running(request, run_ctx):
            return _run_and_audit(request, principal, parsed, started)

    context = contextvars.copy_context()

    async def run() -> None:
        try:
            summary = await loop.run_in_executor(
                _stream_executor(), context.run, work
            )
            _push(("done", summary, None), droppable=False)
        except Exception as exc:
            _push(("error", None, exc), droppable=False)

    worker = asyncio.create_task(run())

    # 首帧也要在 try 里：客户端恰好在首帧之后断开时，生成器在这个 yield 上被关掉——
    # 放在 try 外面的话 finally 不会跑，并发名额永远不还（满 8 次之后新流全被 429），
    # 断开处理也不会发生。
    try:
        yield _envelope(req_id, _task_frame(task_id, context_id, "submitted"))
        while True:
            kind, a, b = await events.get()
            if kind == "stage":
                yield _envelope(req_id, _progress_frame(task_id, context_id, a, b, trace_id))
            elif kind == "done":
                yield _envelope(req_id, _artifact_frame(task_id, context_id, _artifact(task_id, a, trace_id)))
                yield _envelope(req_id, _terminal_frame(task_id, context_id, "completed", trace_id))
                return
            elif isinstance(b, RunCancelled):
                # 契约 v1.13 §4.4：流还开着的时候被本连接以外的途径取消（服务端取消接口、
                # 运维、关停）发 canceled。本连接自己断开时走不到这里——那时已经发不出去了。
                yield _envelope(req_id, _terminal_frame(task_id, context_id, "canceled", trace_id,
                                                        error=str(b)))
                return
            else:
                err = str(b) or type(b).__name__
                log_event(_logger, logging.ERROR, "a2a.stream_failed", error=f"{type(b).__name__}: {err}")
                yield _envelope(req_id, _terminal_frame(task_id, context_id, "failed", trace_id, error=err))
                return
    finally:
        # 客户端断开会让生成器被关掉，这里是唯一保证跑到的收尾点。
        closed.set()
        worker.cancel()
        permit.release()
        if dropped["count"]:
            log_event(_logger, logging.WARNING, "a2a.progress_dropped",
                      trace_id=trace_id, dropped=dropped["count"])
        if not worker.done():
            # cancel() 停得掉那个 await，停不掉已经在跑的同步线程；要停下 pipeline，
            # 得给 run 置取消标记，它在下一个检查点停（SQL 立即中断，在途的模型调用跑完）。
            #
            # 契约 v1.13 §5.4：调用方断开后我方在下一个检查点停下，开关
            # AI_SQL_CANCEL_ON_DISCONNECT 默认开。关掉它就回到 v1.12 的行为——SQL 跑完、
            # 审计照写、配额照扣，只是没人接收结果。两种情况都记一条，
            # 免得「客户端断开后服务端还在烧钱」变成一件没人知道的事。
            runs = runs_of(getattr(request, "app", None))
            if run_ctx is not None and runs is not None and get_settings().cancel_on_disconnect:
                runs.cancel(run_ctx.run_id, "client_disconnected")
                log_event(_logger, logging.INFO, "a2a.stream_cancelled",
                          trace_id=trace_id, run_id=run_ctx.run_id, user_id=principal.user_id)
            else:
                log_event(_logger, logging.INFO, "a2a.stream_abandoned",
                          trace_id=trace_id, user_id=principal.user_id,
                          run_id=run_ctx.run_id if run_ctx is not None else "",
                          note="客户端已断开；断开即取消被关掉了（契约 §5.4），后台继续跑完")


async def _handle_stream(request: Request, req_id: Any, params: Any):
    if not get_settings().a2a_streaming:
        # 开关关着：和 Card 的 streaming:false 一致，调用方据此退回 send（§6.2）
        raise JsonRpcError(E_UNSUPPORTED, "message/stream 未启用（AI_SQL_A2A_STREAMING=false）")
    parsed = _parse_message(params)
    principal = _admit(request, parsed.user_id)          # 流打开之前

    # 入口并发准入（§5.5）：满了就在**流打开之前**返回 429，不排队。
    # 排队只会把「拒绝」变成「全都超时」，而调用方 90s 硬上限一到照样全丢——
    # 不如现在就说清楚，它还能改发 send。
    permit = _admission()
    if not permit.try_acquire():
        raise JsonRpcError(
            E_RATE_LIMITED,
            f"并发流已达上限（{permit.limit}），请稍后重试。",
            http_status=429,
            headers={"Retry-After": "1"},
        )

    task_id = uuid.uuid4().hex
    return StreamingResponse(
        _stream_events(request, req_id, principal, parsed, task_id, permit),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/a2a", include_in_schema=True)
async def a2a_endpoint(request: Request):
    req_id: Any = None
    try:
        try:
            body = await request.json()
        except Exception as exc:
            raise JsonRpcError(E_PARSE, f"请求体不是合法 JSON: {exc}", http_status=400) from exc
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            raise JsonRpcError(E_INVALID_REQUEST, "不是合法的 JSON-RPC 2.0 请求", http_status=400)
        req_id, method, params = body.get("id"), body["method"], body.get("params")

        if method == "message/send":
            return await _handle_send(request, req_id, params)
        if method == "message/stream":
            return await _handle_stream(request, req_id, params)
        if method in ("tasks/cancel", "tasks/resubscribe", "tasks/get",
                      "tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get"):
            # 契约 §7：暂不提供。服务端有取消点和运行记录了，但调用方用不上：断开本身就会
            # 取消（§5.4），控制台走服务端自己的 POST /runs/{id}/cancel。
            raise JsonRpcError(E_UNSUPPORTED, f"{method} 不支持")
        raise JsonRpcError(E_METHOD_NOT_FOUND, f"未知 method: {method}")
    except JsonRpcError as err:
        return _error_response(req_id, err)
