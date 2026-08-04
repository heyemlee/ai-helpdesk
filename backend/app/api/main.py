"""
ai-helpdesk 智能客服系统 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
# ── httpx / httpx2 别名（必须在任何 import httpx 之前）────────────────────────
#
# anthropic 1.x 把 HTTP 层换成了 httpx2（httpx 原作者维护的同 API 分支）。
# 而 OTel 的 HTTPXClientInstrumentor 打的是 httpx 的补丁——不接起来的话，
# 它会照常安装成功、照常不报错，只是**再也看不到 LLM 的请求**：Tempo 里那一段
# 调用链凭空消失，而"追踪没坏、只是少了一层"是最难被发现的一类退化。
#
# alias_httpx() 让全进程的 `import httpx` 解析到 httpx2，埋点因此重新覆盖到
# SDK 的流量；同时项目自己直连 Embedding / Rerank / webhook 的那几处 httpx
# 调用也一并走 httpx2，不用逐个文件改导入。
#
# 它要求在 httpx / httpcore 被导入之前调用，所以位置必须是这里——第一行可执行语句。
import httpx2 as _httpx2

try:
    _httpx2.alias_httpx()
except RuntimeError as _alias_ex:  # pragma: no cover - 只在导入顺序被破坏时发生
    import warnings

    warnings.warn(
        f"httpx2.alias_httpx() 调用过晚（{_alias_ex}）：httpx 已被先一步导入。"
        "服务功能不受影响，但 OTel 的 httpx 埋点将看不到 LLM SDK 的请求，"
        "Tempo 里会缺少这一段调用链。检查是否有新的顶层 import 排到了它前面。",
        RuntimeWarning,
        stacklevel=2,
    )

import asyncio
import ipaddress
import json
import logging
import os
import pathlib
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any

# backend/ 目录：data/、skills/ 的默认位置都相对它解析。
# 不再往 sys.path 塞路径——`app` 是正规包，从 backend/ 启动（uvicorn app.api.main:app）
# 或经 pytest.ini 的 pythonpath 都能直接 import。
_ROOT = str(pathlib.Path(__file__).resolve().parents[2])

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, Security, UploadFile
from fastapi import Request as HttpRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

from app.core import evidence, llm_guard  # noqa: E402
from app.core.evidence import assess  # noqa: E402
from app.core.llm_utils import resolve_llm_config  # noqa: E402
from app.core.preflight import (  # noqa: E402
    TOKEN_ISSUER_CIDRS_VAR,
    is_production,
    outer_tool_timeout_s,
    run_startup_checks,
    token_issuer_networks,
)
from app.core.request_context import (  # noqa: E402
    current_request_id,
    new_request_id,
    request_id_var,
    setup_logging,
)

# LOG_FORMAT=json 输出单行 JSON（采集到 ELK/Datadog）；默认文本便于本地阅读。
# 两种格式都带 request_id，一条日志能定位到一次请求。
setup_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FORMAT", "text"))
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   ai-helpdesk  v2.0  ║
   ║   智能客服 AI 系统   ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None
_knowledge_base = None
_sql_agent    = None
_recognizer   = None
_user_auth    = None
_rate_limiter = None
_ticket_store = None
_ticket_notifier = None
_shared_state = None
_infra_redis  = None
_conv_log     = None
_pg_database  = None
_reranker     = None
_embedder     = None
_llm_info: dict[str, str] = {}

# 后台任务引用集合。asyncio 只持有 Task 的弱引用，不留强引用的话
# 任务可能在完成前被 GC 掉，用户画像会静默不更新。
_background_tasks: set = set()


def _spawn_background(coro) -> None:
    """派发不阻塞响应的后台任务，并持有引用直到完成。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# 跨会话记忆（情景记忆 + 用户画像）的开关，默认开。这两级只按 user_id 区分，
# 而没开令牌鉴权时 user_id 取自请求体，所以面向客户的实例要关掉
# （docker-compose.yml 的 ai-helpdesk-customer）。
# 关的写法不止认 "false"：这是个关着才安全的开关，把 0 / off 当成"开"会在
# 没有任何报错的情况下放行。当前取值 /health 里看得到。
_CROSS_SESSION_MEMORY = os.getenv("ENABLE_CROSS_SESSION_MEMORY", "true").strip().lower() not in {
    "0", "false", "no", "off",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator
    global _skill_manager, _knowledge_base, _recognizer, _sql_agent

    print(BANNER, flush=True)

    # 安全与契约的启动断言，排在所有装配之前：配错了就别起来。
    # 放在 lifespan 里而不是模块顶层，是因为它要的是"这个进程要开始服务了"
    # 这个时刻——导入 api.main 的场合还包括测试和脚本，那些不该被拦。
    run_startup_checks()

    import redis.asyncio as aioredis

    from app.agents.agent_orchestrator import AgentOrchestrator
    from app.core.answer_policy import AnswerPolicy
    from app.core.auth import RateLimiter, UserTokenAuth
    from app.core.embedder import Embedder
    from app.core.intent_recognizer import IntentRecognizer
    from app.core.reranker import Reranker
    from app.core.shared_state import SharedState
    from app.core.skill_loader import SkillManager
    from app.evaluation.evaluator import EndToEndEvaluator
    from app.memory.conversation_memory import MemoryManager
    from app.monitor.performance_monitor import PerformanceMonitor
    from app.storage.conversation_log import ConversationLog, PgConversationLog
    from app.storage.pg import Database
    from app.storage.vector_store import LocalStore, PgStore, check_embedding_signature
    from app.tickets.notifier import TicketNotifier
    from app.tickets.ticket_store import PgTicketStore, TicketStore
    from app.tools.knowledge_base import KnowledgeBase
    from app.tools.sql_agent_client import SQLAgentClient
    from app.tools.tool_manager import Tool, ToolManager

    global _user_auth, _rate_limiter, _ticket_store, _shared_state, _infra_redis
    global _conv_log, _pg_database, _reranker, _ticket_notifier, _embedder

    cfg = resolve_llm_config()
    logger.info(
        f"LLM 供应商: {cfg['provider']}  模型: {cfg['model']}  "
        f"base_url: {cfg.get('base_url', '(官方)')}"
    )
    # /health 里给工作台"模型信息"用。只放供应商和模型名——base_url 是内部
    # 网关地址，/health 在对外入口上是匿名可读的。
    _llm_info.update(provider=cfg["provider"], model=cfg["model"])

    # 基础设施 Redis 连接：限流计数、熔断状态、路由惩罚共享。
    # 与 MemoryManager 的连接分开建，记忆和基础设施状态互不干扰。
    _infra_redis = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True
    )
    _shared_state = SharedState(_infra_redis)

    # 用户令牌：配置 AI_HELPDESK_AUTH_SECRET 后 user_id 从令牌推导，
    # 不再信任请求体——没有它任何人都能冒用别人的 user_id 读取记忆上下文。
    _user_auth = UserTokenAuth(secret=os.getenv("AI_HELPDESK_AUTH_SECRET", ""))
    if _user_auth.enabled:
        logger.info("用户令牌鉴权已开启：/chat 从 X-User-Token 推导 user_id")
    else:
        logger.warning("用户令牌鉴权未开启：user_id 信任请求体，仅适用于本地联调")

    _rate_limiter = RateLimiter(
        redis_client=_infra_redis,
        limit_per_window=int(os.getenv("AI_HELPDESK_RATE_LIMIT_PER_MINUTE", "0")),
    )
    if _rate_limiter.enabled:
        logger.info(f"限流已开启：每用户每分钟 {_rate_limiter.limit} 次")

    # ── 存储装配：配置 POSTGRES_DSN 时全部收敛进 Postgres（pgvector 做向量），
    #    未配置时退化为 SQLite 单文件（demo / 单测零依赖）。Redis 不参与选择。
    embedder = _embedder = Embedder.from_env()
    postgres_dsn = os.getenv("POSTGRES_DSN", "").strip()
    if postgres_dsn:
        _pg_database = Database(postgres_dsn)
        await _pg_database.connect(embedding_dim=embedder.dim)
        vector_store = PgStore(_pg_database)
        _ticket_store = PgTicketStore(_pg_database)
        _conv_log = PgConversationLog(_pg_database)
        logger.info(f"存储后端: Postgres+pgvector（向量 {embedder.dim} 维，Embedding {embedder.backend}）")
    else:
        vector_store = LocalStore(
            db_path=os.getenv("VECTOR_STORE_DB_PATH", str(pathlib.Path(_ROOT) / "data" / "vector_store.db"))
        )
        _ticket_store = TicketStore(
            db_path=os.getenv("TICKET_DB_PATH", str(pathlib.Path(_ROOT) / "data" / "tickets.db"))
        )
        _conv_log = ConversationLog(
            db_path=os.getenv("CONVERSATION_LOG_DB_PATH", str(pathlib.Path(_ROOT) / "data" / "conversations.db"))
        )
        logger.warning("存储后端: SQLite 本地兜底（未配置 POSTGRES_DSN，仅适用于 demo/联调）")
    # 库里的向量得是当前这个 Embedding 模型算的。换过模型而没重建向量的，在这里停下
    # ——维度相同的话后面一路都不会报错，只是语义检索返回的东西和问题无关。
    await check_embedding_signature(vector_store, embedder)

    # 工单通知：outbox 投递器。先落待发送记录再发，失败按退避重试，
    # 到上限则升级（ERROR + ticket_notify_failures_total）。
    # 后台循环负责把进程重启前积压的 pending 捡回来——没有它，
    # "记录落下了但没人再去发"和原来的丢通知没有区别。
    _ticket_notifier = TicketNotifier(
        _ticket_store,
        os.getenv("TICKET_WEBHOOK_URL", ""),
        failures_counter=TICKET_NOTIFY_FAILURES,
        pending_gauge=TICKET_NOTIFY_PENDING,
    )
    if _ticket_notifier.enabled:
        _spawn_background(_ticket_notifier.run_forever())
        logger.info("工单通知已启用（outbox 投递，失败重试并升级）")
    else:
        logger.info("工单通知未配置（TICKET_WEBHOOK_URL 为空），转人工只落库")

    # 意图识别器：Orchestrator 和 Evaluator 共用同一个实例，
    # 这样 /health 里的规则通道命中率统计反映的是线上真实流量。
    _recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        enable_fast_path=os.getenv("ENABLE_INTENT_FAST_PATH", "true").lower() != "false",
        # 和知识库、情景记忆用同一个 Embedder：同一份配置、同一个冷却状态，
        # 远端挂了不会三处各自再撞一次超时。
        embedder=embedder,
    )
    logger.info(f"意图 Embedding 后端: {_recognizer.embedding_backend}")

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("AI_HELPDESK_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("AI_HELPDESK_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # 出站答案策略：拦截无依据的价格/时效承诺
    answer_policy = AnswerPolicy(
        enabled=os.getenv("ENABLE_ANSWER_POLICY", "true").lower() != "false"
    )

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
        # 每种 Agent 起几个实例。>1 时 Monitor 的在线表现反馈才真正影响路由选择。
        replicas=int(os.getenv("AI_HELPDESK_AGENT_REPLICAS", "2")),
        replica_models=[
            m.strip() for m in os.getenv("AI_HELPDESK_AGENT_REPLICA_MODELS", "").split(",") if m.strip()
        ] or None,
        policy=answer_policy,
        recognizer=_recognizer,
    )

    # 记忆管理器（Redis 工作记忆 + pgvector/SQLite 情景记忆与画像）
    _memory = MemoryManager(
        store=vector_store,
        embedder=embedder,
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        cross_session=_CROSS_SESSION_MEMORY,
    )
    if not _CROSS_SESSION_MEMORY:
        logger.info("跨会话记忆已关闭（ENABLE_CROSS_SESSION_MEMORY）：情景记忆和用户画像不读也不写，只留当前会话的记忆")

    # 工具管理器 + RAG 知识库（pgvector / SQLite 双后端的混合检索）
    _reranker = Reranker.from_env()
    _tool_manager = ToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        shared_state=_shared_state,
        reranker=_reranker,
    )
    _knowledge_base = KnowledgeBase(store=vector_store, embedder=embedder)
    kb = _knowledge_base
    await kb.ensure_seeded()
    # 升级到混合检索前写入的片段没有关键词索引，启动时补一次（幂等，通常是空操作）
    await kb.backfill_lexical()
    # 加文档表之前写入的片段没有文档归属，启动时归档一次（同样幂等）；
    # 再把分片已被清空的文档记录收掉，免得列表里摆着检索不到的文档。
    await kb.ensure_documents()
    await kb.prune_empty_documents()
    kb_stats = await kb.document_stats()
    logger.info(
        f"知识库已加载: {kb_stats['total_documents']} 篇文档 / {kb_stats['total_chunks']} 个片段"
        + (f"（其中 {kb_stats['legacy_documents']} 篇由历史分片归档）"
           if kb_stats["legacy_documents"] else "")
        + f"（后端 {kb.backend}，"
        + ("dense+lexical RRF 融合" if embedder.semantic else "关键词优先、本地向量只补位")
        + f"，重排 {_reranker.backend}）"
    )

    def knowledge_fallback(params: dict[str, Any], context: dict[str, Any] | None, error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的语义检索。请稍后重试，或转人工客服确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="搜索知识库（稠密向量 + 关键词双路混合检索，RRF 融合）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))

    # 业务数据委派：订单/交期/库存这类问题的答案在 ERP 等业务库里，不在知识库里。
    # 委派给 ai-sql-agent（另一个服务）去查。未配置 SQL_AGENT_URL 时整块不注册，
    # 行为与加它之前完全一致——这是个可选增强项，不能变成新的启动依赖。
    _sql_agent = SQLAgentClient.from_env()
    if _sql_agent.enabled:
        def sql_agent_fallback(params: dict[str, Any], context: dict[str, Any] | None, error: str):
            # 查不到数据不是错误，是"这轮没有数据可用"。返回空 answer，
            # 让上层照常走知识库 + SOP 的回答路径，而不是把下游故障捅给客户。
            logger.warning(f"业务数据委派不可用，本轮按无数据处理: {error}")
            return {"answer": "", "queried_database": False, "unavailable": True, "error": error}

        _tool_manager.register(Tool(
            name="order_data_query",
            description="向 ai-sql-agent 查询订单/生产/库存/账款等业务数据（只读）",
            handler=_sql_agent.handler,
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "user_id": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["query"],
            },
            # 不缓存：订单状态是会变的，命中缓存等于给客户一个过期的交期。
            cache_ttl=0.0,
            # 和客户端自身超时对齐，留 2s 给连接建立。45s 的由来见
            # sql_agent_client 模块开头：25s 会掉掉约四分之一的查询。
            # 算式在 core.preflight 里，与启动断言"外层 > send 预算"共用同一份，
            # 免得改了这里而断言还在检查另一个数（契约 §1③）。
            timeout_s=outer_tool_timeout_s(),
            fallback=sql_agent_fallback,
        ))
        logger.info(f"业务数据委派已启用: {_sql_agent.endpoint}")
    else:
        logger.info("业务数据委派未配置（SQL_AGENT_URL 为空），订单类问题只走知识库")

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
        shared_state=_shared_state,
    )
    await _monitor.start()

    # 评测器
    _evaluator = EndToEndEvaluator(
        orchestrator=_orchestrator,
        recognizer=_recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("EVAL_BASELINE_PATH", "/app/data/eval/baseline.json"),
        release=os.getenv("AI_HELPDESK_RELEASE", app.version),
        # 评测走和 /chat 完全同一套检索，否则测出来的分数和线上不是一回事
        grounding_provider=_eval_grounding,
    )

    if _API_KEY:
        logger.info("鉴权已开启：写操作端点需要 X-API-Key%s", "，/chat 同样受保护" if _PROTECT_CHAT else "")
    else:
        logger.warning(
            "鉴权未开启：/skills/reload、/knowledge/upload、/eval/promote 等端点对任何人开放。"
            "公网部署请设置 AI_HELPDESK_API_KEY。"
        )

    logger.info("ai-helpdesk 已就绪")
    yield

    await _monitor.stop()
    # 优雅停机：等后台任务（用户画像更新等）收尾，滚动发布不丢写入。
    # 设上限防止个别任务挂死阻塞退出。
    if _background_tasks:
        logger.info(f"等待 {len(_background_tasks)} 个后台任务完成…")
        try:
            await asyncio.wait_for(
                asyncio.gather(*_background_tasks, return_exceptions=True),
                timeout=float(os.getenv("SHUTDOWN_DRAIN_TIMEOUT_S", "10")),
            )
        except TimeoutError:
            logger.warning("后台任务未在时限内完成，放弃等待")
    if _ticket_store is not None:
        await _ticket_store.close()
    if _conv_log is not None:
        await _conv_log.close()
    if _pg_database is not None:
        await _pg_database.close()
    if _memory is not None:
        await _memory.close()
    if _infra_redis is not None:
        await _infra_redis.aclose()
    logger.info("ai-helpdesk 已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ai-helpdesk 智能客服",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

# CORS 白名单从环境变量读取，默认放开是为了本地联调；
# 部署时用 CORS_ALLOW_ORIGINS=https://console.example.com 收敛。
_cors_origins = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


from app.core.metrics import (  # noqa: E402
    RATE_LIMIT_HITS,
    TICKET_NOTIFY_FAILURES,
    TICKET_NOTIFY_PENDING,
    UNMATCHED_PATH,
    observe_http,
)
from app.core.telemetry import setup_telemetry  # noqa: E402
from app.tools.knowledge_base import document_excerpt, should_use_knowledge  # noqa: E402
from app.tools.tool_manager import PASSAGE_CHARS  # noqa: E402

# OTel：配置 OTEL_EXPORTER_OTLP_ENDPOINT 后启用 FastAPI/httpx/redis/asyncpg
# 自动埋点 + 业务 span；未配置时零开销。
setup_telemetry(app)


@app.middleware("http")
async def request_id_middleware(request: HttpRequest, call_next):
    """
    请求追踪：复用网关传入的 X-Request-ID，没有则生成。
    写入 contextvar 供日志过滤器读取，并在响应头原样返回，
    让调用方拿着同一个 id 来报障。同时在这里记录 HTTP QPS/延迟指标。
    """
    import time as _time
    rid = request.headers.get("X-Request-ID") or new_request_id()
    token = request_id_var.set(rid)
    t0 = _time.monotonic()
    status = 500
    observed = False

    def _observe(path_template: str) -> None:
        nonlocal observed
        if observed or path_template in ("/metrics", "/health"):  # 探活不计入业务指标
            return
        observed = True
        observe_http(request.method, path_template, status, _time.monotonic() - t0)

    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        request_id_var.reset(token)
        route = request.scope.get("route")
        # **没匹配到路由时不能退回原始 URL。** 那是无界的：扫描器打一串不存在的
        # 路径就能让时序数量无限增长（`path` 是标签，标签值不回收）。
        path_template = getattr(route, "path", None) or UNMATCHED_PATH

    # 流式响应（/chat/stream）到这里**还一个字节都没发**：call_next 返回的是
    # 一个 StreamingResponse 对象，body 要等下面这个迭代器被消费才产生。
    # 在这里记时间，量到的是"把响应对象造出来花了多久"（毫秒级），
    # 不是这条 SSE 跑完花了多久（十几秒）——p95 面板上那条线看着漂亮，
    # 其实测的是另一件事，而且把 /chat/stream 的真实延迟整个藏了起来。
    # 所以把 body 迭代器包一层，在它真正结束时才观测。
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is not None:
        async def _timed_body():
            try:
                async for chunk in body_iterator:
                    yield chunk
            finally:
                # 客户端中途断开也会走到这里：那一段时间也是真的花掉了
                _observe(path_template)

        response.body_iterator = _timed_body()
    else:
        _observe(path_template)

    response.headers["X-Request-ID"] = rid
    return response


# ── 鉴权 ──────────────────────────────────────────────────────────────────────
# 写操作和管理端点需要 X-API-Key。AI_HELPDESK_API_KEY 未设置时鉴权关闭，
# 方便本地联调；生产部署必须设置，否则 /skills/reload、/knowledge/upload、
# /eval/promote 这类会改变服务行为的接口对任何人开放。
_API_KEY = os.getenv("AI_HELPDESK_API_KEY", "").strip()
# 是否同时保护 /chat。默认不保护：demo 场景希望打开就能聊；
# 但 /chat 每次都会真实调用 LLM，公网暴露时应该设为 true。
_PROTECT_CHAT = os.getenv("AI_HELPDESK_PROTECT_CHAT", "false").strip().lower() in {"1", "true", "yes", "on"}

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """
    写操作/管理端点的鉴权依赖。未配置 AI_HELPDESK_API_KEY 时放行（demo 模式）。

    **生产模式下不放行，返回 503。** 正常情况下这行代码永远不会执行到——
    `core.preflight.check_security_config()` 会让缺 key 的生产进程根本起不来。
    留着它是因为 lifespan 不是唯一的启动路径：直接把 `app` 交给一个不跑
    lifespan 的 ASGI 容器（测试里就是这么用的）会绕过那次校验，而绕过之后
    原来的 `return` 是**放行**。一个防护的缺席不能被解释成"不需要防护"。
    """
    if not _API_KEY:
        if is_production():
            raise HTTPException(
                503, "服务端未配置 AI_HELPDESK_API_KEY，生产模式下拒绝服务此端点"
            )
        return
    if not api_key or not secrets.compare_digest(api_key, _API_KEY):
        raise HTTPException(401, "缺少或错误的 X-API-Key")


def require_admin_key(api_key: str | None = Security(_api_key_header)) -> None:
    """
    **永不放行**的管理鉴权，给能签发身份的端点用。

    `require_api_key` 在未配置 key 时放行是 demo 模式的便利，对大多数端点
    可以接受：最坏是本地联调环境里有人重载了一次 skills。但 `/auth/token`
    不一样——它按请求体里的 `user_id` **签发身份**。漏配 key 时放行意味着
    "配了 AI_HELPDESK_AUTH_SECRET、漏配 AI_HELPDESK_API_KEY"这个组合下，
    未认证请求可以为任意用户拿到有效令牌，再拿它去读那个用户的记忆上下文
    ——两个开关分别看都像是打开的，合起来是全开。

    所以这个依赖不看 APP_ENV：没配 key 就没有管理身份可言，一律 503。
    生产模式漏配根本起不到这里（`core.preflight`），这条是给那个校验被绕过
    的场合兜底的（不跑 lifespan 的 ASGI 容器、APP_ENV 配错）。
    """
    if not _API_KEY:
        raise HTTPException(
            503,
            "服务端未配置 AI_HELPDESK_API_KEY，无法认定管理身份，此端点不可用",
        )
    if not api_key or not secrets.compare_digest(api_key, _API_KEY):
        raise HTTPException(401, "缺少或错误的 X-API-Key")


# `/auth/token` 的来源白名单，模块级读一次（与 _API_KEY 同一套做法）。
# 生产模式下它不能为空，否则 core.preflight 会让进程起不来。
_TOKEN_ISSUER_NETWORKS = token_issuer_networks()


def require_token_issuer_source(request: HttpRequest) -> None:
    """
    `/auth/token` 的来源约束：只有白名单里的对端能调到。

    **这是一个过渡形态的补丁，不是修复。** 真正该签发令牌的是登录系统——
    本仓库里不存在那个组件，对接方案写在 `docs/auth-token-integration.md`。
    在它出现之前，管理 key 只能证明"调用方是我们的基础设施"，证明不了
    "这个 user_id 真的登录了"，所以再收一道**可达范围**：这个端点只应该
    被网关调到。

    **判据是直连对端，不是 `X-Forwarded-For`。** XFF 是一个请求头，任何人都
    能写；把它当来源用，等于把白名单交给调用方自己填。只有直连对端是传输层
    事实。代价是白名单里要写网关的地址而不是最终客户端的——那正是想要的语义。

    白名单为空 = 不限制（demo 模式）。生产模式下为空进程根本起不来
    （`core.preflight.check_token_issuer_binding`），所以这里的空只会出现在
    本地联调，或者 lifespan 被绕过的场合——后者由 `is_production()` 兜住。
    """
    if not _TOKEN_ISSUER_NETWORKS:
        if is_production():
            raise HTTPException(
                503,
                f"服务端未配置 {TOKEN_ISSUER_CIDRS_VAR}，"
                "生产模式下 /auth/token 不可用",
            )
        return

    peer = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        # 拿不到对端地址（某些 ASGI 容器、Unix socket）时按拒绝处置：
        # 无法确认来源和确认了不合法，对这个端点是同一件事。
        logger.warning("/auth/token 无法确定调用方地址（peer=%r），拒绝", peer)
        raise HTTPException(403, "无法确定调用方来源，此端点仅对网关开放") from None

    if not any(address in network for network in _TOKEN_ISSUER_NETWORKS):
        logger.warning("/auth/token 被白名单外的来源调用: %s", peer)
        raise HTTPException(403, "此端点仅对网关开放")


def protect_chat(api_key: str | None = Security(_api_key_header)) -> None:
    """/chat 的可选鉴权，由 AI_HELPDESK_PROTECT_CHAT 控制。"""
    if not _PROTECT_CHAT:
        return
    require_api_key(api_key)


class TokenRequest(BaseModel):
    user_id: str
    ttl_s: int | None = None


@app.post(
    "/auth/token",
    tags=["Auth"],
    dependencies=[Depends(require_admin_key), Depends(require_token_issuer_source)],
)
async def issue_user_token(body: TokenRequest):
    """
    为指定用户签发 HMAC 令牌。未配置 AI_HELPDESK_AUTH_SECRET 时返回 409。

    **鉴权用 `require_admin_key` 而不是 `require_api_key`**：后者在未配置
    key 时放行，而这个端点按请求体里的 `user_id` 签发身份，放行 = 任何人
    可以拿到任何人的令牌。见 `require_admin_key` 的说明。

    **这仍然是一个过渡形态。** 它把签发权收敛到"持有管理 key 的运维/网关"，
    但管理 key 是一把**共享的**钥匙，它证明的是"调用方是我们的基础设施"，
    不是"这个 user_id 真的登录了"。真正该做这件事的是登录系统：由它认证用户
    之后再来换令牌。**本仓库里不存在那个组件**，对接方案（谁签发、user_id
    取哪个 claim、令牌生命周期怎么对齐）写在 `docs/auth-token-integration.md`。

    在它出现之前，"只暴露给网关"这条**不再是文档约定**：
    `require_token_issuer_source` 按 `AI_HELPDESK_TOKEN_ISSUER_CIDRS` 校验直连
    对端，生产模式下这个变量没配或配成 0.0.0.0/0 进程直接起不来。
    约定不会在配错的时候报错，断言会。
    """
    from app.core.auth import AuthError
    if _user_auth is None or not _user_auth.enabled:
        raise HTTPException(409, "未配置 AI_HELPDESK_AUTH_SECRET，令牌鉴权未开启")
    try:
        token = _user_auth.issue(body.user_id, ttl_s=body.ttl_s)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user_id": body.user_id, "token": token}


async def resolve_user_id(req: "ChatRequest", user_token: str | None) -> str:
    """从令牌推导可信 user_id；鉴权未开启时保持旧行为。超限抛 429。"""
    from app.core.auth import AuthError
    try:
        user_id = _user_auth.verify(user_token, req.user_id) if _user_auth else (req.user_id or "anonymous")
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    if _rate_limiter is not None and not await _rate_limiter.check(user_id):
        # 计数器不带 user_id（无界标签会炸时序基数，见 core/metrics.py），
        # "是谁"记进日志——日志有轮转，指标没有。
        RATE_LIMIT_HITS.inc()
        logger.warning("限流拦截: user_id=%s 每分钟上限 %s", user_id, _rate_limiter.limit)
        raise HTTPException(429, f"请求过于频繁：每分钟最多 {_rate_limiter.limit} 次")
    return user_id


async def _create_handoff_ticket(result, user_id: str, conv_id: str, message: str) -> str | None:
    """转人工时落工单。工单创建失败不能影响用户拿到回复，只记录错误。"""
    if _ticket_store is None or not result.handoff:
        return None
    try:
        ticket = await _ticket_store.create(
            conv_id=conv_id,
            user_id=user_id,
            reason=result.handoff_reason,
            request_id=current_request_id(),
            intent=result.intent.value if result.intent else "",
            answer_confidence=result.answer_confidence,
            last_message=message,
        )
    except Exception:
        logger.exception("转人工工单创建失败")
        return None
    if _ticket_notifier is not None and _ticket_notifier.enabled:
        # **先落待发送记录再发**（TicketNotifier.enqueue 内部做的第一件事）。
        # 反过来的话，发送与落记录之间进程挂掉，这次通知就没有任何痕迹——
        # 那正是原来的形态：一个 fire-and-forget task，连 HTTP 状态都不看。
        _spawn_background(_ticket_notifier.enqueue(ticket))
    return ticket.id


def _notifications_enabled() -> bool:
    return _ticket_notifier is not None and _ticket_notifier.enabled


def _ticket_fields(result, ticket_id: str | None) -> dict[str, Any]:
    """
    响应里和工单有关的三个字段。**落库和通知分开报**，工作台照着显示：

    - `ticket_status`：`open` 表示工单真的建出来了；`not_created` 表示这一轮
      判了转人工但工单没建成（存储故障），需要有人手工跟进；没转人工为空。
    - `ticket_notification`：`queued` 表示通知已进待发送队列——**不等于送达**，
      送没送到要看工单详情里的投递记录；`not_configured` 表示这个部署没配
      通知接收方，除了这张工单没有任何人被告知。

    "有同事收到了""会回电"这类说法在这里都没有依据，所以一个都不给。
    """
    if not result.handoff:
        return {"ticket_id": None, "ticket_status": "", "ticket_notification": ""}
    if not ticket_id:
        return {"ticket_id": None, "ticket_status": "not_created", "ticket_notification": ""}
    return {
        "ticket_id": ticket_id,
        "ticket_status": "open",
        "ticket_notification": "queued" if _notifications_enabled() else "not_configured",
    }


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str
    user_id:     str = "anonymous"
    conv_id:     str | None = None


class ChatResponse(BaseModel):
    conv_id:     str
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: list[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: list[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    entities: dict[str, list[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: dict[str, float] = Field(default_factory=dict)
    # 意图识别走的通道：fast_path（纯规则，零 LLM 调用）或 ensemble（三路融合）
    intent_strategy: str = "ensemble"
    intent_latency_ms: float = 0.0
    # 转人工与答案策略
    handoff: bool = False
    handoff_reason: str = ""
    needs_clarification: bool = False
    answer_confidence: float = 1.0
    policy_violations: list[dict[str, str]] = Field(default_factory=list)
    # 模型服务不可用，这一轮没有作答。**不是转人工**：handoff 为 False、没有工单。
    # unavailable_reason 取值见 core/llm_guard 的 REASON_*。
    service_unavailable: bool = False
    unavailable_reason: str = ""
    # 请求追踪 id（与响应头 X-Request-ID 一致）；转人工时创建的工单 id
    request_id: str = ""
    ticket_id: str | None = None
    # 工单落没落库、通知进没进队列，见 _ticket_fields
    ticket_status: str = ""
    ticket_notification: str = ""
    # 本轮真的放进上下文的知识片段，每条带文档和分片 id，可以追回原文。
    # knowledge_used 只说"用没用"，这里说"用了哪几段"。
    knowledge_sources: list[dict[str, Any]] = Field(default_factory=list)
    # 检索和业务数据委派这一轮实际走了哪条路、结果如何（见 _prepare_request）
    knowledge: dict[str, Any] = Field(default_factory=dict)
    business_data: dict[str, Any] = Field(default_factory=dict)


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    tickets = await _ticket_store.stats() if _ticket_store else {}
    conversations = await _conv_log.stats() if _conv_log else {}
    return {
        "status": "ok",
        "agents": _orchestrator.get_stats(),
        "routing": _orchestrator.routing_config(),
        "tickets": tickets,
        # 投递状态要能被看见：`failed` 不为零意味着有客户被告知转人工、
        # 而值班的人没收到通知——这是"工单落库了"看不出来的那一半。
        "ticket_notifications": (
            await _ticket_store.notify_stats() if _ticket_store else {}
        ),
        "conversations": conversations,
        "storage_backend": _knowledge_base.backend if _knowledge_base else "unknown",
        "retrieval": {
            # 本地 n-gram 向量没有语义，不参与排序（见 knowledge_base.lexical_first_fuse）
            "mode": ("hybrid_dense_lexical_rrf" if _embedder is None or _embedder.semantic
                     else "lexical_first_dense_fill"),
            "rerank": _reranker.backend if _reranker else "llm_listwise",
            # available 为 false：远端 Embedding 刚失败过、还在冷却期，这期间检索只走
            # 关键词一路、文档导入会被拒绝。读的是进程里记下的状态，不为这个接口去调它。
            "embedding": _embedder.status() if _embedder else None,
        },
        "auth": {
            "user_token": _user_auth.enabled if _user_auth else False,
            "rate_limit_per_minute": _rate_limiter.limit if _rate_limiter else 0,
        },
        # 面向客户的实例这里应当是 false，部署后看这一项核对。
        "memory": {
            "cross_session": _memory.cross_session if _memory else False,
        },
        # available / unavailable_reason 来自凭证不可用保护（core/llm_guard）：读的是
        # 进程里记下的状态，不为了回答这个接口去调一次模型。
        "llm": {**_llm_info, **_llm_status()},
        # 这个实例接了什么。工作台据此说明"能查什么"：没接业务数据委派的实例，
        # 订单和生产进度就只能答知识库里的规则，不能答某一单的实时状态；
        # 没配通知接收方的实例，转人工只落工单、不会有人被通知。
        "capabilities": {
            "business_data": bool(_sql_agent is not None and _sql_agent.enabled),
            "ticket_notifications": _notifications_enabled(),
        },
    }


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"], dependencies=[Depends(require_api_key)])
async def reload_skills():
    """
    运行时重新扫描 Skill 目录，不需要重启服务。

    同时**解除模型凭证不可用保护**（core/llm_guard）：凭证或额度处理好之后，
    调一次这个接口，下一条真实请求就会重新出网去试；还不行的话那一次调用会
    重新触发保护。这是"不重启就恢复"的唯一入口，沿用这个已有的受保护接口，
    不另开一个——也不做定时探测。
    """
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return {**_skill_manager.summary(), "llm_guard_reset": llm_guard.reset_all()}


def _llm_status() -> dict[str, Any]:
    status = getattr(_orchestrator, "llm_status", None)
    if not callable(status):
        return {"available": True, "unavailable_reason": "", "retry_after_s": None}
    return status()


def _model_unavailable_reason() -> str:
    """模型服务此刻是不是被保护挡着（不出网）。是就返回原因。"""
    reason = getattr(_orchestrator, "llm_unavailable_reason", None)
    return reason() if callable(reason) else ""


# ── 准备阶段的进度 ────────────────────────────────────────────────────────────
#
# 意图识别、检索改写、重排各是一次模型往返，加起来十几秒到一分钟。这段时间流里
# 以前什么都不发——只有委派查库走 A2A 时，下游的阶段才会透传上来。于是只问知识的
# 问题（最常见的一类）在出第一个字之前，页面上只有一句不动的"正在处理"，
# 看不出是在干活还是卡住了。
#
# 阶段名带 `helpdesk.` 前缀，和下游透传的阶段名（契约 §4.5）分开，两边各改各的
# 不会撞。`label` 是给不认识这个阶段的旧前端看的；新前端按 stage 自己出文案，
# 跟着界面语言走。
STAGE_UNDERSTANDING = "helpdesk.understanding"
STAGE_GATHERING = "helpdesk.gathering"
_STAGE_LABEL = {
    STAGE_UNDERSTANDING: "正在理解问题",
    STAGE_GATHERING: "正在查找回答需要的资料",
}


def _report_stage(progress_cb, stage: str, **detail: Any) -> None:
    """把"现在在做哪一步"放进进度队列。非流式没有队列（progress_cb 为 None），什么都不做。"""
    if progress_cb is None:
        return
    try:
        progress_cb({"stage": stage, "label": _STAGE_LABEL.get(stage, ""), **detail})
    except Exception:
        # 进度是增强项：报不出去不能连累这一轮回答
        logger.debug("进度上报失败: %s", stage, exc_info=True)


def _will_query_business_data(message: str, intent=None, entities=None) -> bool:
    """这一轮会不会去委派查业务数据。和 `_build_order_data_context` 开头的三道门是同一套判断。"""
    from app.tools.sql_agent_client import should_query_business_data

    if _tool_manager is None or _sql_agent is None or not _sql_agent.enabled:
        return False
    return bool(should_query_business_data(message, intent=intent, entities=entities))


async def _prepare_request(req: "ChatRequest", conv_id: str, user_id: str, progress_cb=None):
    """
    /chat 和 /chat/stream 共用的准备阶段：
      读取三级记忆 → 意图识别 → 按需 RAG → 组装编排请求

    返回 (编排请求, 意图结果, 是否用到知识库, 依据记录)。
    抽出来是为了让流式和非流式共用同一套上下文构造——两条链路一旦漂移，
    就会出现"流式和非流式对同一个问题给出不同答案"的问题。

    依据记录是 `{"knowledge": {...}, "business_data": {...}}`：这一轮检索到了
    哪几段知识、业务数据委派有没有发起、结果算不算数。它们原样进响应，
    工作台显示的"回答依据"和学习模式的讲解都只来自这里——前端自己推断不出
    这一轮有没有查库，推断出来的也不能当事实显示。
    """
    from app.agents.agent_orchestrator import Request as OrcReq

    _report_stage(progress_cb, STAGE_UNDERSTANDING)

    # 1. 读取记忆上下文
    mem_ctx = await _memory.get_context(user_id, conv_id, query=req.message)

    # 2. 对话历史，用于意图识别上下文
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    intent_result = await _orchestrator.recognize_intent(req.message, history=history)

    knowledge_trace: dict[str, Any] = {}
    data_trace: dict[str, Any] = {}
    outage = _model_unavailable_reason()
    if outage:
        # 模型服务不可用：这一轮作答不了（编排器会直接返回"不可用"，或按规则转人工），
        # 检索和委派查库拿回来的东西没有人读。尤其是委派——它会让下游为一个注定
        # 用不上的答案把自己的模型跑一遍。
        knowledge_text, knowledge_used, order_text, order_is_evidence = "", False, "", False
        knowledge_trace.update(attempted=False, mode="", sources=[], reranked=False,
                               corrected=False, evidence_ok=None, degraded=False)
        data_trace.update(enabled=bool(_sql_agent is not None and _sql_agent.enabled),
                          attempted=False, outcome="model_unavailable", reason=outage)
    else:
        # 接下来要查什么，动手之前就定得下来：两个门控都是同步的纯判断，和下面两个
        # 构建函数里用的是同一对函数。两路是并发跑的，所以合成一条进度报出去，
        # 不拆成两条——拆开的话先报的那条会被界面当成"已经做完了"。
        searching = _tool_manager is not None and should_use_knowledge(
            req.message, intent=intent_result.intent)
        querying = _will_query_business_data(
            req.message, intent=intent_result.intent, entities=intent_result.entities)
        if searching or querying:
            _report_stage(progress_cb, STAGE_GATHERING, knowledge=searching, order_data=querying)

        # 规则通道命中说明意图已经确定，检索这边也跟着走短链路：
        # 直接检索，跳过"查询改写 + LLM 重排"两次额外的 LLM 往返。
        # 知识检索和业务数据查询互不依赖，并发跑。串起来的话查库那 17.7s（契约 §2）
        # 会整段叠在检索之后，用户等待时间白白多出一截。
        (knowledge_text, knowledge_used), (order_text, order_is_evidence) = await asyncio.gather(
            _build_knowledge_context(
                req.message,
                intent=intent_result.intent,
                fast_path=intent_result.fast_path,
                trace=knowledge_trace,
            ),
            _build_order_data_context(
                req.message,
                intent=intent_result.intent,
                entities=intent_result.entities,
                user_id=user_id,
                conv_id=conv_id,
                progress_cb=progress_cb,
                trace=data_trace,
            ),
        )
    context_parts = [mem_ctx.to_prompt_text()]
    if knowledge_text:
        context_parts.append(knowledge_text)
    if order_text:
        context_parts.append(order_text)
    full_context = "\n\n".join(part for part in context_parts if part)

    orch_req = OrcReq(
        message=req.message,
        user_id=user_id,
        conv_id=conv_id,
        # 与响应头 X-Request-ID 同源，编排器日志和 API 日志能互相对上
        request_id=current_request_id(),
        context=full_context,
        history=history,
        entities=intent_result.entities,
        intent=intent_result.intent,
        intent_group=intent_result.intent_group,
        urgency=intent_result.urgency,
        intent_confidence=intent_result.confidence,
        # 意图那一路模型没答上来时，这个意图是降级凑出来的——编排器据此不去追问
        intent_degraded=bool(getattr(intent_result, "llm_failed", False)),
        # 只有检索到的知识和**真的查回来的**业务数据才算"出处"，记忆和历史不能
        # 作为价格/时效的依据。业务数据必须在这里——否则真实交期会被答案策略
        # 当成无依据承诺替换掉。反过来，委派失败时那段降级提示进的是上下文而
        # 不是这里：它是指令不是证据，`order_is_evidence` 就是这个开关。
        # 见 _build_order_data_context 的说明。
        grounding_sources=[
            t for t in (knowledge_text, order_text if order_is_evidence else "") if t
        ],
    )
    evidence_trace = {"knowledge": knowledge_trace, "business_data": data_trace}
    return orch_req, intent_result, knowledge_used, evidence_trace


def _evidence_fields(evidence_trace: dict[str, Any]) -> dict[str, Any]:
    """依据记录 → 响应字段。/chat 的响应体和 /chat/stream 的 meta 事件用同一份。"""
    knowledge = dict(evidence_trace.get("knowledge") or {})
    return {
        "knowledge_sources": knowledge.pop("sources", []),
        "knowledge": knowledge,
        "business_data": dict(evidence_trace.get("business_data") or {}),
    }


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(protect_chat)])
async def chat(req: ChatRequest, x_user_token: str | None = Header(None, alias="X-User-Token")):
    """
    主对话接口。完整流程：
      身份解析/限流 → 记忆读取 → 意图识别 → Agent 路由 → 执行 → 工单 → 记忆写入
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    from app.memory.conversation_memory import MsgRole

    user_id = await resolve_user_id(req, x_user_token)
    conv_id = req.conv_id or str(uuid.uuid4())
    orch_req, intent_result, knowledge_used, evidence_trace = await _prepare_request(
        req, conv_id, user_id
    )

    # 3. 执行
    result = await _orchestrator.run(orch_req)

    # 4. 转人工 → 落工单（失败不影响响应）
    ticket_id = await _create_handoff_ticket(result, user_id, conv_id, req.message)

    # 5. 写入记忆 + 审计留档（审计写入不阻塞响应）
    await _memory.add_message(user_id, conv_id, MsgRole.USER, req.message)
    await _memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)
    _spawn_background(_log_turn(conv_id, user_id, req.message, result.response,
                                result.agent_type.value, current_request_id()))

    # 6. 异步更新用户画像（不阻塞响应）。没答上来的轮次不提炼：那一轮的"助手发言"
    #    是一句故障提示，里面没有客户的任何偏好，提炼只是再花一次模型调用。
    if not result.service_unavailable:
        _spawn_background(_memory.update_profile(user_id, conv_id))

    return ChatResponse(
        conv_id=conv_id,
        request_id=current_request_id(),
        **_ticket_fields(result, ticket_id),
        **_evidence_fields(evidence_trace),
        response=result.response,
        intent=result.intent.value if result.intent else "other",
        intent_group=intent_result.intent_group,
        agent_type=result.agent_type.value,
        agent_types=[agent_type.value for agent_type in result.agent_types],
        primary_agent=result.primary_agent.value if result.primary_agent else result.agent_type.value,
        supporting_agents=[agent_type.value for agent_type in result.supporting_agents],
        routing_reason=result.routing_reason,
        routing_confidence=result.routing_confidence,
        escalated=result.escalated,
        latency_ms=round(result.latency_ms, 1),
        knowledge_used=knowledge_used,
        entities=intent_result.entities,
        intent_confidence=round(intent_result.confidence, 4),
        intent_source_scores=intent_result.source_scores,
        intent_strategy=intent_result.strategy,
        intent_latency_ms=round(intent_result.latency_ms, 2),
        handoff=result.handoff,
        handoff_reason=result.handoff_reason,
        needs_clarification=result.needs_clarification,
        answer_confidence=result.answer_confidence,
        policy_violations=[v.to_dict() for v in result.policy_violations],
        service_unavailable=result.service_unavailable,
        unavailable_reason=result.unavailable_reason,
    )


@app.post("/chat/stream", dependencies=[Depends(protect_chat)], tags=["对话"])
async def chat_stream(req: ChatRequest, x_user_token: str | None = Header(None, alias="X-User-Token")):
    """
    /chat 的流式版本，SSE（text/event-stream）。

    事件类型：
      routing  路由决策，在第一个字之前就发出，前端可以立刻画出主/辅 Agent 徽章
      delta    回答增量。注意是**按句**发出的：答案策略守卫必须看到完整句子
               才能判断是不是无依据的价格/时效承诺，所以流式的粒度是句而不是 token
      result   最终结构化结果，字段与 /chat 的响应一致
      error    服务端异常

    浏览器的 EventSource 只支持 GET，这里是 POST，前端用 fetch + ReadableStream 消费。
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    from app.memory.conversation_memory import MsgRole

    user_id = await resolve_user_id(req, x_user_token)
    conv_id = req.conv_id or str(uuid.uuid4())

    async def event_source():
        try:
            # 准备阶段（记忆、意图、检索、**委派查库**）以前是先 await 完再发第一个事件——
            # 委派那 17~40 秒里客户端零字节。现在把它放进任务，边等边把下游进度作为
            # progress 事件推出去；静默满 15s 发一条 SSE 注释当心跳。
            progress_q: asyncio.Queue = asyncio.Queue()
            prep = asyncio.create_task(
                _prepare_request(req, conv_id, user_id, progress_cb=progress_q.put_nowait)
            )
            async for frame in _pump_until_done(prep, progress_q, heartbeat_s=SSE_HEARTBEAT_S):
                yield frame
            orch_req, intent_result, knowledge_used, evidence_trace = prep.result()
            yield _sse("meta", {
                "conv_id": conv_id,
                "intent": intent_result.intent.value,
                "intent_group": intent_result.intent_group,
                "intent_confidence": round(intent_result.confidence, 4),
                "intent_strategy": intent_result.strategy,
                "intent_latency_ms": round(intent_result.latency_ms, 2),
                "intent_source_scores": intent_result.source_scores,
                "entities": intent_result.entities,
                "knowledge_used": knowledge_used,
                **_evidence_fields(evidence_trace),
            })

            final = None
            async for event in _orchestrator.run_stream(orch_req):
                kind = event["type"]
                if kind == "routing":
                    yield _sse("routing", {k: v for k, v in event.items() if k != "type"})
                elif kind == "delta":
                    yield _sse("delta", {"text": event["text"]})
                elif kind == "result":
                    final = event["result"]

            if final is None:
                yield _sse("error", {"detail": "编排器未返回结果"})
                return

            ticket_id = await _create_handoff_ticket(final, user_id, conv_id, req.message)
            await _memory.add_message(user_id, conv_id, MsgRole.USER, req.message)
            await _memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, final.response)
            _spawn_background(_log_turn(conv_id, user_id, req.message, final.response,
                                        final.agent_type.value, current_request_id()))
            if not final.service_unavailable:      # 同 /chat：没答上来的轮次不提炼画像
                _spawn_background(_memory.update_profile(user_id, conv_id))

            yield _sse("result", {
                "conv_id": conv_id,
                "request_id": current_request_id(),
                **_ticket_fields(final, ticket_id),
                "response": final.response,
                "agent_type": final.agent_type.value,
                "agent_types": [a.value for a in final.agent_types],
                "primary_agent": final.primary_agent.value if final.primary_agent else final.agent_type.value,
                "supporting_agents": [a.value for a in final.supporting_agents],
                "routing_reason": final.routing_reason,
                "routing_confidence": final.routing_confidence,
                "escalated": final.escalated,
                "handoff": final.handoff,
                "handoff_reason": final.handoff_reason,
                "needs_clarification": final.needs_clarification,
                "answer_confidence": final.answer_confidence,
                "policy_violations": [v.to_dict() for v in final.policy_violations],
                "service_unavailable": final.service_unavailable,
                "unavailable_reason": final.unavailable_reason,
                "latency_ms": round(final.latency_ms, 1),
            })
        except Exception as ex:
            logger.exception("流式对话失败")
            yield _sse("error", {"detail": str(ex)})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 Nginx 的响应缓冲，否则代理会攒够一批才下发，流式就失去意义
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    """按 SSE 线格式序列化一个事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_HEARTBEAT_S = float(os.getenv("SSE_HEARTBEAT_S", "15"))


async def _pump_until_done(task, progress_q, heartbeat_s: float = 15.0):
    """
    等一个任务完成，期间把队列里的进度作为 progress 事件吐出；静默满 heartbeat_s
    就吐一条 SSE 注释（`: ping`）——浏览器忽略它，代理不会因空闲断连。

    **同时等队列和任务**，谁先来算谁。只等队列的话，任务干完了泵还阻塞在
    `queue.get()` 上，要等满一个心跳周期才发现——生产上心跳 15s，等于每次流式
    都白等 15 秒才发 meta。这个坑是测试里 frames 多出一条 ping 才暴露的。

    任务结束后把队列里剩下的进度清空再返回，保证 progress 一定在 meta 之前。
    抽成独立函数是为了不起整个 app 也能测它。
    """
    while not task.done():
        getter = asyncio.ensure_future(progress_q.get())
        done, _ = await asyncio.wait(
            {getter, task}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED
        )
        if getter in done:
            yield _sse("progress", getter.result())
            continue
        # 取消前再看一眼：可能刚好在这一瞬间拿到了，丢掉就少一条进度
        getter.cancel()
        if not done:
            yield ": ping\n\n"
    while not progress_q.empty():
        yield _sse("progress", progress_q.get_nowait())


async def _eval_grounding(message: str, intent_result) -> str:
    """
    评测用的出处提供者：复用 /chat 的检索链路，返回知识库上下文文本。

    fast_path 的口径也保持一致——规则通道定性的高频问题走单次直检，
    否则走查询改写 + 并行召回 + 重排 + 自纠错。
    """
    text, _ = await _build_knowledge_context(
        message,
        intent=getattr(intent_result, "intent", None),
        fast_path=getattr(intent_result, "fast_path", False),
    )
    return text


async def _label_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    给知识片段补上所属文档的展示标题。

    片段自带的 title 是导入时填的标题（常常只是文件名），文档正文里的标题行
    才是人认得的名字。查不到（文档表读失败、片段还没归档）就退回片段标题——
    标不上出处不该让这一轮对话失败。
    """
    if not sources or _knowledge_base is None:
        return sources
    try:
        titles = await _knowledge_base.document_titles([s["doc_id"] for s in sources])
    except Exception as ex:
        logger.warning(f"读取知识片段所属文档失败，出处只显示片段标题: {ex}")
        return sources
    return [{**s, "doc_title": titles.get(s["doc_id"]) or s["title"]} for s in sources]


async def _build_knowledge_context(
    message: str,
    intent=None,
    top_k: int = 3,
    fast_path: bool = False,
    trace: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """
    为 /chat 主链路构建 RAG 知识上下文。

    完整链路会复用 ToolManager 的查询改写、并行召回、重排 + 证据判定、
    自纠错、fallback；fast_path=True 时走单次直检，用两次 LLM 往返换检索
    广度——意图已经由规则通道确定的高频问题不需要多角度改写。

    注意这里少掉的只是**改写和重排**这两次 LLM 往返。混合检索（稠密向量 +
    关键词双路 RRF 融合）发生在存储层，不花任何 LLM 调用，所以快通道同样
    享受到它带来的召回提升，零网络往返的承诺不受影响。

    `trace` 传进来的话，这一轮检索的经过会记在里面：走的哪条路、放进上下文的
    是哪几段（`sources`，每段带文档和分片 id）。**只记真的进了上下文的片段**
    ——召回了但被重排挤掉的不算，降级占位的那一条（知识库不可用时的兜底）
    也不算：它不是知识，不能被显示成"回答的依据"。
    """
    info = trace if trace is not None else {}
    info.update(attempted=False, mode="", sources=[], reranked=False,
                corrected=False, evidence_ok=None, degraded=False)
    if _tool_manager is None:
        return "", False
    if not should_use_knowledge(message, intent=intent):
        return "", False
    info.update(attempted=True, mode="direct" if fast_path else "rewrite_rerank")
    try:
        if fast_path:
            result = await _tool_manager.call(
                "knowledge_search", {"query": message, "top_k": top_k}
            )
        else:
            result = await _tool_manager.search_with_rewrite(
                "knowledge_search", message, top_k=top_k
            )
        if not result.success or not isinstance(result.data, list) or not result.data:
            return "", False
        info.update(reranked=result.reranked, corrected=result.corrected,
                    evidence_ok=result.evidence_ok)

        parts = ["[知识库检索结果]"]
        sources: list[dict[str, Any]] = []
        used = False
        for i, item in enumerate(result.data[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "未命名文档"))
            content = str(item.get("content", "")).strip()
            score = item.get("score", "")
            if not content:
                continue
            used = True
            # 只走了关键词一路的结果没有向量分（score 是 None），不给模型看一行"相关度: None"
            relevance = f"   相关度: {score}\n" if isinstance(score, (int, float)) else ""
            parts.append(f"{i}. 标题: {title}\n{relevance}   内容: {content[:PASSAGE_CHARS]}")
            if item.get("fallback"):
                info["degraded"] = True
                continue
            sources.append({
                "doc_id": str(item.get("doc_id") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
                "title": title,
                "passage": item.get("chunk", 0),
                "score": score if isinstance(score, (int, float)) else None,
                "retrieval": str(item.get("retrieval") or ""),
                # 给人看的摘录去掉开头的标题行和 Markdown 记号；喂给模型的那份（上面的
                # parts）不动。截断处带省略号——硬切在词中间（"…Curbsi"）读起来像
                # 段落本身坏了，而被切掉的往往正是回答依据的那一句
                "excerpt": document_excerpt(content, 240),
            })

        if not used:
            return "", False
        info["sources"] = await _label_sources(sources)
        parts.append("请优先依据以上知识库内容回答；如果知识库内容不足，再结合通用客服能力说明。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")
        return "", False


def _currency_note(data: dict) -> str:
    """
    金额币种的说明句。**币种的来源必须在句子里可见。**

    契约 §4.6.5 把这一条单独立了一节，理由是它已经出过一次业务事故：下游只
    返回数字不返回币种，中文回答就默认写成「元」，接美国厂的库时报给 builder
    的金额带了错误的币种。当时的补救是在调用方配一个全局常量
    `SQL_AGENT_CURRENCY` 顶上——**那是个跨仓库配置在替契约打补丁**：
    它是**部署**的属性不是**数据**的属性，一个部署里只要有多币种的库，
    它必然给出错误答案，而且报出去的金额看起来完全正常。

    契约 v1.8 把这一条改成**按来源三分**，来源判定在
    `core.evidence.resolve_currency()`（`currency_source`，与 `status_source`
    同构）；这里只负责把那个来源**说进句子里**：

    1. `contract` —— 数据自己声明了 `currency` → 按数据说，可以当事实；
    2. `deployment_config` —— 部署配了全局币种 → 可以用，但**明写它来自部署
       配置**、不是本次查询返回的字段；
    3. `unknown` —— 两者都没有 → 只写数字，并**主动禁止**语言的默认值。

    第三档是一条**指令**不是一条禁令。契约 v1.6/v1.7 那条"不得补币种"是禁令，
    而自然语言有自己的默认值：中文回答不说币种就会写成「元」。
    "不说"会被默认值填上，所以规则必须写成"说什么"。
    """
    configured = _sql_agent.currency if _sql_agent is not None else ""
    currency, source = evidence.resolve_currency(data, configured)
    if source == evidence.CURRENCY_CONTRACT:
        return (
            f"以上金额的币种是 {currency}（本次查询随数据一同返回）。"
            f"提到金额时按 {currency} 表述，不要换算，也不要写成其它币种。"
        )
    if source == evidence.CURRENCY_DEPLOYMENT_CONFIG:
        return (
            f"以上金额按 {currency} 表述，不要换算，也不要写成其它币种。"
            f"但请注意：{currency} 来自本部署的配置（SQL_AGENT_CURRENCY），"
            "**不是**本次查询返回的字段——业务系统目前不返回币种。"
            "因此不得把币种说成系统核实过的结果；客户对币种有疑问时请转人工确认。"
        )
    return (
        "业务系统不返回币种，本部署也没有声明。提到金额时**只写数字，不要补"
        "任何币种符号或单位**（不要默认写成「元」），并说明币种需要人工确认。"
    )


async def _build_order_data_context(
    message: str,
    intent=None,
    entities: dict | None = None,
    user_id: str = "",
    conv_id: str = "",
    progress_cb=None,
    trace: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """
    为 /chat 主链路构建业务数据上下文：把问题委派给 ai-sql-agent 查真实数据。

    返回 `(上下文文本, 是否可作为依据)`。**第二个值不是"用没用到"，是"能不能
    当出处"**——调用方据此决定要不要放进 `grounding_sources`。两种非空返回：

    - **真数据** → `(数据块, True)`，进上下文也进 grounding sources；
    - **降级提示** → `(提示块, False)`，只进上下文**不进** grounding sources。
      它是写给 Agent 的指令而不是证据，混进 sources 会让答案策略拿它去给
      价格/日期"找出处"，等于用一句"查询失败了"来批准一个凭空的数字。

    任何一环不成立都返回空——这是增强项，拿不到数据时链路照常走知识库 + SOP，
    不能因此中断。

    **"拿到了响应但不该当依据"的情况，都要挡在这里。** 判定不在这个函数里
    自己写，统一走 `core.evidence.assess()`——三个承载者共用同一套口径
    （契约 §4.4.1），否则回滚（§6.2）会变成静默的信任口径切换。分四类：

    1. `unavailable` —— 下游挂了或熔断，fallback 给的占位结果。返回降级提示；
    2. `not_applicable` —— ai-sql-agent 自己判断这问题不需要查库，走了对话
       路径。那个 answer 是模型说的话，**不是数据**，把它当依据注入等于给
       一段没有出处的文本盖了"实时数据"的章。**连上下文都不进**；
    3. `failed` —— 说查了库，但一步都没成。**这一类以前看不见**：
       `queried_database` 的服务端定义只有一行 `pipeline_result is not None`，
       SQL 全跑挂了它也是 `true`，终态照发 `completed`。于是模型编的话会被
       当成"业务系统查到的真实数据"讲给客户。现在按 §4.6.1 的判据从
       `steps[]` 判出来，返回一段明写"没查成"的提示，**不作为出处**；
    4. `has_risk` / `degraded` / 缺字段 / 字符串布尔 —— 数据**进上下文**
       （丢掉比现状更糟，§4.6.1），但**不进 grounding_sources**，
       并带上明确的不确定表述约束。以前这两个标志只追加一句提示、
       函数照样返回 True，等于带风险的数据和已核实的金额共享同一个可信标志。

    另外，空 answer 直接返回空：查了但没有可讲的内容。

    **关于把它放进 grounding_sources（安全相关，别随手改）。** AnswerPolicy 会
    拦截"无依据的价格/时效承诺"，判据是承诺里的数字能不能在本轮 grounding
    sources 里找到出处。订单交期、账款金额恰好就是这两类数字——如果查回来的
    真实数据不进 sources，客服说"您的柜体 9 月 15 日出厂"会被当成无依据承诺
    替换成安全话术，整条委派看起来就像没生效。所以这段文本必须进 sources；
    反过来，上面第 2 条那种"不是数据的文本"绝不能进。

    `trace` 传进来的话，这次委派的结局会记在 `outcome` 里，供响应如实报告：
    `not_configured`（这个实例没接业务数据）、`skipped`（这个问题不需要查）、
    `model_unavailable`（本方模型服务不可用，这一轮根本没来委派——由 `_prepare_request`
    直接记下，不经过这个函数）、
    `evidence`（查到了，可作依据）、`unverified`（查到了，没过可信校验）、
    `failed` / `unavailable` / `error`（没查成）、`not_applicable`（下游判断
    不需要查库）、`empty`（查了，没有内容）。和上面的返回值是同一次判定的
    两种说法——一个给 Agent 读，一个给工作台读，不许各判各的。
    """
    from app.tools.sql_agent_client import should_query_business_data

    info = trace if trace is not None else {}
    info.update(enabled=False, attempted=False, outcome="not_configured")
    if _tool_manager is None or _sql_agent is None or not _sql_agent.enabled:
        return "", False
    info.update(enabled=True, outcome="skipped")
    if not should_query_business_data(message, intent=intent, entities=entities):
        return "", False
    info.update(attempted=True, outcome="error")

    try:
        result = await _tool_manager.call(
            "order_data_query",
            {"query": message, "user_id": user_id, "session_id": conv_id},
            # 走 A2A 流式时，下游的阶段进度经这个回调进 /chat/stream 的 progress 事件
            context={"progress_cb": progress_cb} if progress_cb else None,
        )
        data = result.data if isinstance(result.data, dict) else {}

        # 委派失败（超时、熔断、下游异常）。**不静默**——实测里最危险的一种
        # 形态就是这个：下游其实查完了（HTTP 200），只是调用方先放弃，客户
        # 拿到一段听起来完全合理、实则没有任何系统依据的回答，客户和坐席
        # 都分辨不出来。这里把失败明写进上下文，要求 Agent 说出来。
        if data.get("unavailable") or (not result.success and not data):
            reason = str(data.get("error") or result.error or "未知原因")
            info.update(outcome="unavailable", reason=reason)
            return "\n".join([
                "[业务系统查询未完成]",
                f"本轮尝试查询订单/生产/库存/账款系统，但没有拿到结果（{reason}）。",
                "回答时必须做到两点：",
                "1. 不要给出任何具体的订单状态、日期、金额、库存数字——这些本轮没有系统依据；",
                "2. 明确告诉客户这次没能查到系统记录，建议由人工跟进核实，不要让客户以为"
                "你说的内容已经过系统核对。",
            ]), False

        # 信任判定（契约 §4.6）。**唯一的一处**——三个承载者
        # （stream / send / 直连 /query）走的是同一个 assess()，
        # 因为回滚会让流量在它们之间切换（契约 §6.2 与 §4.4.1），
        # 判定要是分了叉，回滚就成了静默的信任口径切换。
        verdict = assess(data)

        # 下游自己判断这问题不需要查库（走了对话路径）。不是故障，不提示。
        # 那段 answer 是模型说的话不是数据，连上下文都不该进。
        if verdict.execution_status == evidence.NOT_APPLICABLE:
            info.update(outcome="not_applicable")
            return "", False

        trace_id = str(data.get("trace_id", "") or "")
        info.update(
            trace_id=trace_id,
            databases=[str(d) for d in data.get("selected_databases") or []],
            execution_status=verdict.execution_status,
        )

        # 查库路径但一步都没成。这里**必须**和"查到了但没有记录"分开：
        # `queried_database` 两种情况下都是 true（服务端定义只有一行
        # `pipeline_result is not None`），只看它会把"查挂了"讲成真实数据。
        if verdict.execution_status == evidence.FAILED:
            logger.warning(
                "业务数据查询未成功，不作为出处 trace_id=%s status=%s(%s) 原因=%s",
                trace_id, verdict.execution_status, verdict.status_source,
                "；".join(verdict.reasons) or "-",
            )
            info.update(outcome="failed", reasons=list(verdict.reasons))
            return "\n".join([
                "[业务系统查询未成功]",
                f"本轮查询业务系统的每一个步骤都没有成功执行（trace_id: {trace_id or '未知'}）。",
                "回答时必须做到两点：",
                "1. 不要给出任何具体的订单状态、日期、金额、库存数字——这些本轮没有系统依据；",
                "2. 明确告诉客户这次没能查到系统记录，建议由人工跟进核实，不要让客户以为"
                "你说的内容已经过系统核对。",
            ]), False

        answer = str(data.get("answer", "")).strip()
        if not answer:
            info.update(outcome="empty")
            return "", False

        # ai-sql-agent 的 answer 里嵌的是查询结果的 JSON 原文，行数多的时候
        # 能到几万字符（它那边 max_rows 默认 10000）。整段塞进 prompt 会把
        # 上下文顶爆，也会把知识库那部分挤掉，所以这里截断。
        # 截断阈值调大之前先想清楚：真正该做的是让下游少返点行，不是这边多收点。
        limit = int(os.getenv("SQL_AGENT_MAX_CONTEXT_CHARS", "2000"))
        if len(answer) > limit:
            answer = answer[:limit] + f"\n…（结果过长已截断，完整结果见 trace_id {trace_id}）"

        databases = ", ".join(str(d) for d in data.get("selected_databases") or []) or "未标明"
        # 标题就把可信度写在第一行：可作出处的那段叫「实时数据」，不可作出处的
        # 那段绝不能也叫这个名字——Agent 读到的第一行决定它怎么措辞。
        header = "[业务系统实时数据]" if verdict.usable_as_source else "[业务系统数据（未通过可信校验，仅供参考）]"
        parts = [
            header,
            f"来源: ai-sql-agent（命中库: {databases}；trace_id: {trace_id}；"
            f"执行状态: {verdict.execution_status}）",
            answer,
        ]
        if verdict.has_risk:
            parts.append(
                "注意: 这条查询结果带有异常提示（空结果 / 基数异常 / 语义不一致），"
                "SQL 跑通了但答的可能不是问的那件事。**不得**把其中的金额、日期、"
                "订单状态说成已核实的结论，只能说明系统里查到了这些内容、需要人工核对。"
            )
        if verdict.degraded:
            parts.append(
                f"注意: 数据服务处于降级状态（{data.get('degrade_reason', '') or '原因未说明'}），"
                "结果可能来自历史查询重放，可能已经过期。**不得**表述为当前状态，"
                "必须请客户以正式单据为准。"
            )
        parts.extend(f"注意: {c}" for c in verdict.caveats)
        parts.append(_currency_note(data))
        parts.append(
            "以上是业务系统查到的真实数据，回答订单/交期/库存/账款问题时优先依据它。"
            if verdict.usable_as_source else
            "以上内容**没有通过可信校验**，不能当作已核实的系统依据。"
            "涉及具体日期、金额、库存数字时必须明说这一点，并建议人工核实。"
        )
        if not verdict.usable_as_source:
            logger.warning(
                "业务数据未通过可信校验，不进 grounding_sources trace_id=%s status=%s(%s) 原因=%s",
                trace_id, verdict.execution_status, verdict.status_source,
                "；".join(verdict.reasons) or "-",
            )
        info.update(
            outcome="evidence" if verdict.usable_as_source else "unverified",
            has_risk=verdict.has_risk,
            degraded=verdict.degraded,
            reasons=list(verdict.reasons),
        )
        return "\n".join(p for p in parts if p), verdict.usable_as_source
    except Exception as ex:
        logger.warning(f"构建业务数据上下文失败: {ex}")
        info.update(outcome="error", reason=str(ex) or type(ex).__name__)
        return "", False




@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class TicketStatusRequest(BaseModel):
    status: str


async def _ticket_payloads(tickets: list) -> list[dict[str, Any]]:
    """
    工单 → 接口里的样子：工单本身 + 能流转到哪些状态 + 通知投递记录。

    `notification` 是 outbox 里那条记录的原样转述：`pending`（还在发 / 等重试）、
    `delivered`（接收端回了 2xx）、`failed`（放弃了，需要人介入）。没有记录就是
    `None`——通知没配，或者这张工单没入队。**`delivered` 只说明 webhook 收下了
    这条消息，不说明有人读了、接了、会回电**；那些事这个系统不记录，也就不报。
    """
    from app.tickets.ticket_store import allowed_transitions

    deliveries = await _ticket_store.notify_for([t.id for t in tickets])
    payloads = []
    for ticket in tickets:
        delivery = deliveries.get(ticket.id)
        payloads.append({
            **ticket.to_dict(),
            "allowed_transitions": allowed_transitions(ticket.status),
            "notification": {
                "status": delivery.status,
                "attempts": delivery.attempts,
                "last_error": delivery.last_error,
                "updated_at": delivery.updated_at,
            } if delivery else None,
        })
    return payloads


@app.get("/tickets", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def list_tickets(status: str | None = None, limit: int = 50):
    """工单列表（坐席/运维视角），可按状态过滤。"""
    if _ticket_store is None:
        raise HTTPException(503, "服务未就绪")
    tickets = await _ticket_store.list(status=status, limit=max(1, min(limit, 200)))
    return {
        "tickets": await _ticket_payloads(tickets),
        "stats": await _ticket_store.stats(),
        "notifications_enabled": _notifications_enabled(),
    }


@app.get("/tickets/{ticket_id}", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def get_ticket(ticket_id: str):
    if _ticket_store is None:
        raise HTTPException(503, "服务未就绪")
    ticket = await _ticket_store.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, f"工单不存在: {ticket_id}")
    return {
        **(await _ticket_payloads([ticket]))[0],
        "notifications_enabled": _notifications_enabled(),
    }


async def _log_turn(
    conv_id: str, user_id: str, user_msg: str,
    assistant_msg: str, agent_type: str, request_id: str,
) -> None:
    """把一轮对话写入审计日志（user + assistant 各一条）。"""
    if _conv_log is None:
        return
    await _conv_log.append(conv_id, user_id, "user", user_msg, request_id=request_id)
    await _conv_log.append(conv_id, user_id, "assistant", assistant_msg,
                           request_id=request_id, agent_type=agent_type)


@app.get("/conversations/{conv_id}", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def get_conversation(conv_id: str, limit: int = 500):
    """完整对话原文（坐席接工单 / 合规审计视角）。工单里的 conv_id 从这里回查。"""
    if _conv_log is None:
        raise HTTPException(503, "服务未就绪")
    entries = await _conv_log.get_conversation(conv_id, limit=min(limit, 2000))
    if not entries:
        raise HTTPException(404, f"对话不存在或已超出留存期: {conv_id}")
    return {"conv_id": conv_id, "messages": [e.to_dict() for e in entries]}


@app.post("/tickets/{ticket_id}/status", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def update_ticket_status(ticket_id: str, body: TicketStatusRequest):
    """流转工单状态：open → assigned → resolved / cancelled。"""
    if _ticket_store is None:
        raise HTTPException(503, "服务未就绪")
    try:
        ticket = await _ticket_store.update_status(ticket_id, body.status)
    except KeyError as exc:
        raise HTTPException(404, f"工单不存在: {ticket_id}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        **(await _ticket_payloads([ticket]))[0],
        "notifications_enabled": _notifications_enabled(),
    }


class SearchRequest(BaseModel):
    """检索请求体。"""
    query: str
    top_k: int = 5


@app.post("/search", dependencies=[Depends(require_api_key)])
async def search(body: SearchRequest):
    """
    演示检索优化链路：
      查询改写 → 并行召回（每路内部是 dense+lexical 混合）→ 去重
      → 重排 + 证据判定 →（不足则补检一轮）→ Top-K

    响应里的 corrected 表示是否触发过自纠错，evidence_ok 是最终的证据充分性
    判定——这两个信号是排"为什么这个问题答得不好"时最先要看的东西。

    每条结果带 `doc_id` 和 `doc_title`，能从片段追回它所在的文档。

    **和知识库的其它接口一样要员工钥匙。** 它原样返回知识片段的正文——
    内部的采购、成本规则也在里面——所以不能是匿名可读的。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite(
        "knowledge_search", body.query, top_k=body.top_k
    )
    results = result.data if isinstance(result.data, list) else []
    if _knowledge_base is not None:
        doc_ids = [r.get("doc_id", "") for r in results if isinstance(r, dict)]
        try:
            titles = await _knowledge_base.document_titles(doc_ids)
        except Exception as ex:
            logger.warning(f"读取检索结果所属文档失败: {ex}")
            titles = {}
        results = [
            {**r, "doc_title": titles.get(r.get("doc_id", ""), "")} if isinstance(r, dict) else r
            for r in results
        ]
    return {
        "query": body.query,
        "results": results,
        "reranked": result.reranked,
        "corrected": result.corrected,
        "evidence_ok": result.evidence_ok,
    }


def _require_knowledge_base():
    """取知识库实例。之前是从工具的 handler 上反查 __self__，绕了一大圈还容易断。"""
    if _knowledge_base is None:
        raise HTTPException(503, "知识库未初始化")
    return _knowledge_base


class DocInput(BaseModel):
    """单篇文档输入。"""
    title:   str
    content: str


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: list[DocInput]


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: dict[str, Any] | None = None


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: str | None = None
    turns: list[str] | None = None
    user_id: str | None = None
    conv_id: str | None = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: list[EvalIntentInput] | None = None
    dialog_cases: list[EvalDialogInput] | None = None
    # 是否把本次结果提升为新基线。默认 False：跑评测不应该顺手覆盖对比基准，
    # 否则回归检测只能看出"比上一次差"，连续小幅劣化会被完全放过。
    promote: bool = False


@app.post("/knowledge/add", tags=["知识库"], dependencies=[Depends(require_api_key)])
async def add_knowledge(body: BatchDocInput):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字），由 Embedder 生成向量后存入 pgvector（或 SQLite 兜底）。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "补件流程", "content": "缺件需在验收单注明并提供订单号、柜号和装箱单编号..."},
        {"title": "交期说明", "content": "标准交期 4-6 周，以工厂排产确认为准..."}
      ]
    }
    ```
    """
    kb = _require_knowledge_base()
    results = await _import_or_503(
        kb, [{"title": d.title, "content": d.content} for d in body.documents],
        source_type="manual",
    )
    return await _import_response(kb, results)


@app.post("/knowledge/upload", tags=["知识库"], dependencies=[Depends(require_api_key)])
async def upload_knowledge(file: UploadFile = File(...)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    kb = _require_knowledge_base()

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")
    # 只留文件名本身：有的浏览器会把整条本地路径带上来，那不该进知识库的来源栏
    filename = pathlib.PurePath((file.filename or "unknown").replace("\\", "/")).name

    if filename.lower().endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}") from e
        if not isinstance(docs, list) or not all(isinstance(d, dict) for d in docs):
            raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    results = await _import_or_503(kb, docs, source_type="upload", source_name=filename)
    return await _import_response(kb, results, f"文件 {filename} 导入成功")


async def _import_or_503(kb, docs: list[dict[str, Any]], **source) -> list[dict[str, Any]]:
    """
    导入文档；远端 Embedding 不可用时返回 503，而不是把没有向量的文档写进库。

    一批里排在前面、已经算出向量的文档这时已经入库了。导入是幂等的，
    所以提示里直接说"恢复后把同一份再导一次"——已有的会被跳过，不会重复。
    """
    from app.core.embedder import EmbeddingUnavailable

    try:
        return await kb.import_documents(docs, **source)
    except EmbeddingUnavailable as ex:
        logger.warning(f"知识库导入中断，Embedding 不可用: {ex}")
        raise HTTPException(
            503,
            "Embedding 服务暂时不可用，文档没有全部导入。恢复后把同一份内容重新导入即可，"
            "已经导入的不会重复。",
        ) from ex


async def _import_response(
    kb, results: list[dict[str, Any]], message: str | None = None
) -> dict[str, Any]:
    """
    导入接口的响应。`documents` 逐篇说明落成了什么（新建 / 已存在 / 空），
    带着文档 id——工作台导入完能直接打开那一篇，不用再去搜它进没进去。
    """
    chunks = sum(r["chunks"] for r in results if r["status"] == "created")
    stats = await kb.document_stats()
    return {
        "message": message or f"成功导入 {chunks} 个文档片段",
        "added_chunks": chunks,
        "added_documents": sum(1 for r in results if r["status"] == "created"),
        "documents": results,
        **stats,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """知识库统计：文档数、片段数，以及其中有多少篇是由历史分片归档来的。"""
    kb = _require_knowledge_base()
    return await kb.document_stats()


@app.get("/knowledge/documents", tags=["知识库"], dependencies=[Depends(require_api_key)])
async def list_knowledge_documents(q: str = "", limit: int = 100, offset: int = 0):
    """
    知识库里有哪些文档。不带 `q` 是浏览全部（新导入的在前）；带 `q` 时按空白
    拆成词，每个词都要在标题或正文里出现，响应里说明是标题命中还是正文命中。

    这是**按字面找文档**。"用一句问题找相关段落"是另一件事，走 `POST /search`
    ——那条链路会改写问题、做语义召回和重排，对话里用的也是它。

    每篇带 `origin`：`original` 是导入时存下的原文；`legacy` 是加文档表之前
    导入的，正文由当时的分片拼回；`legacy_partial` 是分片不全的历史文档。
    后两种没有来源和文件名可报——当时没记。
    """
    kb = _require_knowledge_base()
    return await kb.list_documents(
        query=q.strip()[:200], limit=max(1, min(limit, 500)), offset=max(0, offset)
    )


@app.get("/knowledge/documents/{doc_id}", tags=["知识库"],
         dependencies=[Depends(require_api_key)])
async def get_knowledge_document(doc_id: str):
    """一篇文档的正文，以及它被切成的片段（检索用的就是这些片段）。"""
    kb = _require_knowledge_base()
    document = await kb.get_document(doc_id)
    if document is None:
        raise HTTPException(404, f"文档不存在: {doc_id}")
    return document


@app.get("/sops", tags=["Skills"], dependencies=[Depends(require_api_key)])
async def list_sops():
    """
    业务 SOP 全文（只读）。SOP 就是加载进 Agent 的那些 Skill：它规定处理步骤
    和升级条件，和知识库里说明业务事实的文档不是一回事，所以单独一个入口，
    不混进知识检索结果。

    `/skills` 只给摘要，这里给全文，因此要员工钥匙：SOP 里写的是内部的审批
    边界和话术，不是给客户看的 FAQ。`source` 是相对 Skill 目录的路径，
    不暴露服务器上的绝对路径。
    """
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    root = _skill_manager.root_dir
    sops = []
    for skill in _skill_manager.skills:
        path = pathlib.Path(skill.path)
        try:
            source = str(path.relative_to(root))
            modified_at: float | None = path.stat().st_mtime
        except (ValueError, OSError):
            source, modified_at = path.name, None
        sops.append({
            "name": skill.name,
            "description": skill.description,
            "agents": skill.agents,
            "keywords": skill.keywords,
            "enabled": skill.enabled,
            "content": skill.content,
            "content_chars": len(skill.content),
            "source": source,
            "modified_at": modified_at,
        })
    return {"count": len(sops), "sops": sops, "errors": len(_skill_manager.errors)}


@app.post("/eval/run", dependencies=[Depends(require_api_key)])
async def run_eval(body: EvalRunInput | None = None):
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from app.evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    report = await _evaluator.run(
        intent_cases=intent_cases,
        dialog_cases=dialog_cases,
        promote=bool(body and body.promote),
    )
    return _eval_report_payload(report)


def _eval_report_payload(report) -> dict[str, Any]:
    return {
        "release":         report.release,
        "timestamp":       report.timestamp,
        "pass_rate":       report.pass_rate,
        "total":           report.total,
        "passed":          report.passed,
        "avg_scores":      report.avg_scores,
        "regressions":     report.regressions,
        "recommendations": report.recommendations,
        "baseline": {
            "release": report.baseline_release,
            "timestamp": report.baseline_timestamp,
        },
        "promoted":        report.promoted,
        # 模型服务不可用时评测中途中止：results 是中止前完成的那些，planned 是本来要跑的项数
        "incomplete":        report.incomplete,
        "incomplete_reason": report.incomplete_reason,
        "planned":           report.planned,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "scores": r.scores,
                "detail": r.detail,
                "metadata": r.metadata,
            }
            for r in report.results
        ],
    }


@app.get("/eval/baseline", tags=["评测"])
async def eval_baseline():
    """查看当前对比基线。回归检测的所有结论都是相对这份数据得出的。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    baseline = _evaluator.baseline
    if baseline is None:
        return {"exists": False, "message": "尚未建立基线，第一次运行 /eval/run 会自动落盘"}
    return {"exists": True, **_eval_report_payload(baseline)}


@app.post("/eval/promote", tags=["评测"], dependencies=[Depends(require_api_key)])
async def promote_baseline():
    """把最近一次评测结果提升为新基线（人工确认分数可接受后调用）。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    history = _evaluator.history
    if history and history[-1].incomplete:
        raise HTTPException(
            409, f"最近一次评测未完成（模型服务不可用：{history[-1].incomplete_reason}），不能提升为基线",
        )
    report = _evaluator.promote_baseline()
    if report is None:
        raise HTTPException(400, "还没有可提升的评测结果，请先调用 /eval/run")
    return {"message": "基线已更新", **_eval_report_payload(report)}


# ── 前端静态资源 ──────────────────────────────────────────────────────────────
def _mount_frontend(application: FastAPI) -> None:
    """
    把前端构建产物挂到 "/"，让单个进程同时提供 API 和页面。

    必须在所有 API 路由注册完之后调用：Starlette 按注册顺序匹配，
    挂在 "/" 的 StaticFiles 会吃掉所有未匹配路径，提前挂载会遮蔽 /chat 等接口。

    默认找 ../frontend/dist，可用 AI_HELPDESK_FRONTEND_DIST 覆盖。
    目录不存在时静默跳过——纯后端部署（docker compose 里前端是独立容器）不受影响。
    """
    configured = os.getenv("AI_HELPDESK_FRONTEND_DIST", "").strip()
    candidates = [pathlib.Path(configured)] if configured else [
        pathlib.Path(_ROOT) / "static",
        pathlib.Path(_ROOT).parent / "frontend" / "dist",
    ]
    for dist in candidates:
        if dist.is_dir() and (dist / "index.html").exists():
            # html=True：未命中静态文件时回落到 index.html，支持前端路由
            application.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
            logger.info(f"前端静态资源已挂载: {dist}")
            return
    logger.info("未找到前端构建产物，仅提供 API（前端请单独启动或先 npm run build）")


_mount_frontend(app)


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("ai-helpdesk CLI — 输入 quit 退出\n")

    from app.agents.agent_orchestrator import AgentOrchestrator, Request
    from app.core.skill_loader import SkillManager
    from app.memory.conversation_memory import MemoryManager, MsgRole

    cfg = resolve_llm_config()
    skill_manager = SkillManager(
        root_dir=os.getenv("AI_HELPDESK_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("AI_HELPDESK_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    from app.core.embedder import Embedder as _Embedder
    from app.storage.vector_store import LocalStore as _LocalStore
    mem  = MemoryManager(
        store=_LocalStore(db_path=os.getenv("VECTOR_STORE_DB_PATH", "/tmp/aihd_vector_store.db")),
        embedder=_Embedder.from_env(),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        ctx = await mem.get_context(user_id, conv_id, query=msg)
        history = [
            {"role": m.role.value, "content": m.content}
            for m in ctx.recent_messages[-5:]
        ] if ctx.recent_messages else None
        req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
        result = await orch.run(req)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

        print(f"\nai-helpdesk [{result.agent_type.value}]: {result.response}\n")

    await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "app.api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
