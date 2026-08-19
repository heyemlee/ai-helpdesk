"""
ai-helpdesk 智能客服系统 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import json
import logging
import os
import pathlib
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any

_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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

from core.request_context import (  # noqa: E402
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
_recognizer   = None
_user_auth    = None
_rate_limiter = None
_ticket_store = None
_shared_state = None
_infra_redis  = None
_conv_log     = None
_pg_database  = None
_reranker     = None

# 后台任务引用集合。asyncio 只持有 Task 的弱引用，不留强引用的话
# 任务可能在完成前被 GC 掉，用户画像会静默不更新。
_background_tasks: set = set()


def _spawn_background(coro) -> None:
    """派发不阻塞响应的后台任务，并持有引用直到完成。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _anthropic_cfg() -> dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


def _embedding_cfg() -> dict[str, Any]:
    """
    意图识别 Embedding 这一路的配置。

    配置了 OpenAI 兼容端点就走远端真向量；留空则退化为本地 n-gram 向量，
    Embedding 依然作为独立的一路参与投票。
    """
    return {
        "embedding_base_url": os.getenv("EMBEDDING_BASE_URL", "").strip() or None,
        "embedding_api_key": os.getenv("EMBEDDING_API_KEY", "").strip() or None,
        "embedding_model": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator
    global _skill_manager, _knowledge_base, _recognizer

    print(BANNER, flush=True)

    import redis.asyncio as aioredis

    from agents.agent_orchestrator import AgentOrchestrator
    from core.answer_policy import AnswerPolicy
    from core.auth import RateLimiter, UserTokenAuth
    from core.embedder import Embedder
    from core.intent_recognizer import IntentRecognizer
    from core.reranker import Reranker
    from core.shared_state import SharedState
    from core.skill_loader import SkillManager
    from evaluation.evaluator import EndToEndEvaluator
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from storage.conversation_log import ConversationLog, PgConversationLog
    from storage.pg import Database
    from storage.vector_store import LocalStore, PgStore
    from tickets.ticket_store import PgTicketStore, TicketStore

    global _user_auth, _rate_limiter, _ticket_store, _shared_state, _infra_redis
    global _conv_log, _pg_database, _reranker

    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

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
    embedder = Embedder.from_env()
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

    # 意图识别器：Orchestrator 和 Evaluator 共用同一个实例，
    # 这样 /health 里的规则通道命中率统计反映的是线上真实流量。
    _recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        enable_fast_path=os.getenv("ENABLE_INTENT_FAST_PATH", "true").lower() != "false",
        **_embedding_cfg(),
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
    )

    # MCP 工具管理器 + RAG 知识库（pgvector / SQLite 双后端的混合检索）
    _reranker = Reranker.from_env()
    _tool_manager = MCPToolManager(
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
    logger.info(
        f"知识库已加载: {await kb.doc_count_async()} 个文档片段"
        f"（后端 {kb.backend}，混合检索 dense+lexical，重排 {_reranker.backend}）"
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


from core.metrics import RATE_LIMIT_HITS, observe_http  # noqa: E402
from core.telemetry import setup_telemetry  # noqa: E402
from mcp.knowledge_base import should_use_knowledge  # noqa: E402

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
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        request_id_var.reset(token)
        route = request.scope.get("route")
        path_template = getattr(route, "path", request.url.path)
        if path_template not in ("/metrics", "/health"):  # 探活不计入业务指标
            observe_http(request.method, path_template, status, _time.monotonic() - t0)
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
    """写操作/管理端点的鉴权依赖。未配置 AI_HELPDESK_API_KEY 时直接放行。"""
    if not _API_KEY:
        return
    if not api_key or not secrets.compare_digest(api_key, _API_KEY):
        raise HTTPException(401, "缺少或错误的 X-API-Key")


def protect_chat(api_key: str | None = Security(_api_key_header)) -> None:
    """/chat 的可选鉴权，由 AI_HELPDESK_PROTECT_CHAT 控制。"""
    if not _PROTECT_CHAT:
        return
    require_api_key(api_key)


class TokenRequest(BaseModel):
    user_id: str
    ttl_s: int | None = None


@app.post("/auth/token", tags=["Auth"], dependencies=[Depends(require_api_key)])
async def issue_user_token(body: TokenRequest):
    """
    为指定用户签发 HMAC 令牌（生产上这一步由登录系统完成，这里由持有
    API key 的运维/网关侧代发）。未配置 AI_HELPDESK_AUTH_SECRET 时返回 409。
    """
    from core.auth import AuthError
    if _user_auth is None or not _user_auth.enabled:
        raise HTTPException(409, "未配置 AI_HELPDESK_AUTH_SECRET，令牌鉴权未开启")
    try:
        token = _user_auth.issue(body.user_id, ttl_s=body.ttl_s)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user_id": body.user_id, "token": token}


async def resolve_user_id(req: "ChatRequest", user_token: str | None) -> str:
    """从令牌推导可信 user_id；鉴权未开启时保持旧行为。超限抛 429。"""
    from core.auth import AuthError
    try:
        user_id = _user_auth.verify(user_token, req.user_id) if _user_auth else (req.user_id or "anonymous")
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    if _rate_limiter is not None and not await _rate_limiter.check(user_id):
        RATE_LIMIT_HITS.labels(user_id=user_id).inc()
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
    webhook = os.getenv("TICKET_WEBHOOK_URL", "").strip()
    if webhook:
        _spawn_background(_notify_ticket_webhook(webhook, ticket))
    return ticket.id


async def _notify_ticket_webhook(url: str, ticket) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"type": "ticket.created", "ticket": ticket.to_dict()})
    except Exception as ex:
        logger.warning(f"工单 webhook 通知失败: {ex}")


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
    # 请求追踪 id（与响应头 X-Request-ID 一致）；转人工时创建的工单 id
    request_id: str = ""
    ticket_id: str | None = None


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
        "conversations": conversations,
        "storage_backend": _knowledge_base.backend if _knowledge_base else "unknown",
        "retrieval": {
            "mode": "hybrid_dense_lexical_rrf",
            "rerank": _reranker.backend if _reranker else "llm_listwise",
        },
        "auth": {
            "user_token": _user_auth.enabled if _user_auth else False,
            "rate_limit_per_minute": _rate_limiter.limit if _rate_limiter else 0,
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
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


async def _prepare_request(req: "ChatRequest", conv_id: str, user_id: str):
    """
    /chat 和 /chat/stream 共用的准备阶段：
      读取三级记忆 → 意图识别 → 按需 RAG → 组装编排请求

    返回 (编排请求, 意图结果, 是否用到知识库)。
    抽出来是为了让流式和非流式共用同一套上下文构造——两条链路一旦漂移，
    就会出现"流式和非流式对同一个问题给出不同答案"的问题。
    """
    from agents.agent_orchestrator import Request as OrcReq

    # 1. 读取记忆上下文
    mem_ctx = await _memory.get_context(user_id, conv_id, query=req.message)

    # 2. 对话历史，用于意图识别上下文
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    intent_result = await _orchestrator.recognize_intent(req.message, history=history)

    # 规则通道命中说明意图已经确定，检索这边也跟着走短链路：
    # 直接检索，跳过"查询改写 + LLM 重排"两次额外的 LLM 往返。
    knowledge_text, knowledge_used = await _build_knowledge_context(
        req.message,
        intent=intent_result.intent,
        fast_path=intent_result.fast_path,
    )
    context_parts = [mem_ctx.to_prompt_text()]
    if knowledge_text:
        context_parts.append(knowledge_text)
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
        # 只有检索到的知识才算"出处"，记忆和历史不能作为价格/时效的依据
        grounding_sources=[knowledge_text] if knowledge_text else [],
    )
    return orch_req, intent_result, knowledge_used


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(protect_chat)])
async def chat(req: ChatRequest, x_user_token: str | None = Header(None, alias="X-User-Token")):
    """
    主对话接口。完整流程：
      身份解析/限流 → 记忆读取 → 意图识别 → Agent 路由 → 执行 → 工单 → 记忆写入
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    from memory.conversation_memory import MsgRole

    user_id = await resolve_user_id(req, x_user_token)
    conv_id = req.conv_id or str(uuid.uuid4())
    orch_req, intent_result, knowledge_used = await _prepare_request(req, conv_id, user_id)

    # 3. 执行
    result = await _orchestrator.run(orch_req)

    # 4. 转人工 → 落工单（失败不影响响应）
    ticket_id = await _create_handoff_ticket(result, user_id, conv_id, req.message)

    # 5. 写入记忆 + 审计留档（审计写入不阻塞响应）
    await _memory.add_message(user_id, conv_id, MsgRole.USER, req.message)
    await _memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)
    _spawn_background(_log_turn(conv_id, user_id, req.message, result.response,
                                result.agent_type.value, current_request_id()))

    # 6. 异步更新用户画像（不阻塞响应）
    _spawn_background(_memory.update_profile(user_id, conv_id))

    return ChatResponse(
        conv_id=conv_id,
        request_id=current_request_id(),
        ticket_id=ticket_id,
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

    from memory.conversation_memory import MsgRole

    user_id = await resolve_user_id(req, x_user_token)
    conv_id = req.conv_id or str(uuid.uuid4())

    async def event_source():
        try:
            orch_req, intent_result, knowledge_used = await _prepare_request(req, conv_id, user_id)
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
            _spawn_background(_memory.update_profile(user_id, conv_id))

            yield _sse("result", {
                "conv_id": conv_id,
                "request_id": current_request_id(),
                "ticket_id": ticket_id,
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


async def _build_knowledge_context(
    message: str,
    intent=None,
    top_k: int = 3,
    fast_path: bool = False,
) -> tuple[str, bool]:
    """
    为 /chat 主链路构建 RAG 知识上下文。

    完整链路会复用 MCPToolManager 的查询改写、并行召回、重排 + 证据判定、
    自纠错、fallback；fast_path=True 时走单次直检，用两次 LLM 往返换检索
    广度——意图已经由规则通道确定的高频问题不需要多角度改写。

    注意这里少掉的只是**改写和重排**这两次 LLM 往返。混合检索（稠密向量 +
    关键词双路 RRF 融合）发生在存储层，不花任何 LLM 调用，所以快通道同样
    享受到它带来的召回提升，零网络往返的承诺不受影响。
    """
    if _tool_manager is None:
        return "", False
    if not should_use_knowledge(message, intent=intent):
        return "", False
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

        parts = ["[知识库检索结果]"]
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
            parts.append(f"{i}. 标题: {title}\n   相关度: {score}\n   内容: {content[:600]}")

        if not used:
            return "", False
        parts.append("请优先依据以上知识库内容回答；如果知识库内容不足，再结合通用客服能力说明。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")
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


@app.get("/tickets", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def list_tickets(status: str | None = None, limit: int = 50):
    """工单列表（坐席/运维视角），可按状态过滤。"""
    if _ticket_store is None:
        raise HTTPException(503, "服务未就绪")
    tickets = await _ticket_store.list(status=status, limit=min(limit, 200))
    return {"tickets": [t.to_dict() for t in tickets], "stats": await _ticket_store.stats()}


@app.get("/tickets/{ticket_id}", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def get_ticket(ticket_id: str):
    if _ticket_store is None:
        raise HTTPException(503, "服务未就绪")
    ticket = await _ticket_store.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, f"工单不存在: {ticket_id}")
    return ticket.to_dict()


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
    return ticket.to_dict()


class SearchRequest(BaseModel):
    """检索请求体。"""
    query: str
    top_k: int = 5


@app.post("/search")
async def search(body: SearchRequest):
    """
    演示检索优化链路：
      查询改写 → 并行召回（每路内部是 dense+lexical 混合）→ 去重
      → 重排 + 证据判定 →（不足则补检一轮）→ Top-K

    响应里的 corrected 表示是否触发过自纠错，evidence_ok 是最终的证据充分性
    判定——这两个信号是排"为什么这个问题答得不好"时最先要看的东西。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite(
        "knowledge_search", body.query, top_k=body.top_k
    )
    return {
        "query": body.query,
        "results": result.data,
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
    count = await kb.add_documents_async([{"title": d.title, "content": d.content} for d in body.documents])
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


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
    filename = file.filename or "unknown"

    if filename.endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}") from e
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    count = await kb.add_documents_async(docs)
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息（文档片段总数）。"""
    kb = _require_knowledge_base()
    return {"total_chunks": await kb.doc_count_async()}


@app.post("/eval/run", dependencies=[Depends(require_api_key)])
async def run_eval(body: EvalRunInput | None = None):
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

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

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from core.skill_loader import SkillManager
    from memory.conversation_memory import MemoryManager, MsgRole

    cfg = _anthropic_cfg()
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
    from core.embedder import Embedder as _Embedder
    from storage.vector_store import LocalStore as _LocalStore
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
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
