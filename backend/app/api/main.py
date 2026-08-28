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
import dataclasses
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
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    Security,
    UploadFile,
)
from fastapi import Request as HttpRequest
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator

load_dotenv()

from app.core import llm_guard  # noqa: E402
from app.core.llm_utils import ANSWER_EFFORT, resolve_llm_config  # noqa: E402
from app.core.openai_compat import tools_rejected  # noqa: E402
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
from app.events import EventLog, EventLogConfig, TurnRecorder, bind, events, instance_from_env  # noqa: E402
from app.events.projection import mismatches, project_turn, summarize_turn  # noqa: E402
from app.events.recorder import MAX_ID_CHARS  # noqa: E402

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
_model_runtime = None
_model_watch_task = None
_llm_info: dict[str, str] = {}
# 一轮对话的事件日志（app/events/）。lifespan 里按配置换成真的；默认这个什么都不记，
# 但 turn_id 照样生成——不跑 lifespan 的测试和脚本也拿得到
_event_log: EventLog = EventLog.disabled()

# 这个进程是哪个实例：staff 或 customer（AI_HELPDESK_INSTANCE）。审计日志的实例列用它，
# 事件日志也按它分开两个实例的行。没设是空串
_INSTANCE = instance_from_env()

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
    global _model_runtime, _model_watch_task, _event_log

    from app.api import model_admin
    from app.core import model_settings
    from app.core.model_runtime import ModelRuntime, chat_config_from, embedder_from

    # 模型连接：控制台保存的那一份压过 .env（core/model_settings.py）。没开这个功能、
    # 或者文件里没有某一组时，那一组照旧用 .env。
    settings_path = model_settings.settings_path()
    saved_models = model_settings.load_store(settings_path)
    for section, problem in (saved_models.get("problems") or {}).items():
        logger.warning(f"控制台保存的模型连接（{section}）读不出来，按没有处理: {problem}")
    cfg = chat_config_from(saved_models)
    logger.info(
        f"LLM 供应商: {cfg['provider']}  模型: {cfg['model']}  "
        f"base_url: {cfg.get('base_url') or '(官方)'}  来源: {cfg['source']}"
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
    embedder, embedding_meta = embedder_from(saved_models)
    _embedder = embedder
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

    # 一轮对话的事件日志（app/events/）：有 Postgres 写 turn_events，没有就存进程内最近
    # 200 轮。默认关着；关着时 turn_id 照样生成、照样返回
    _event_log = await EventLog.open(EventLogConfig.from_env(), db=_pg_database)

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
    _log_loop_config()

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

    # 模型连接的运行时：控制台改了之后由它换给各组件，客户实例靠它跟上员工实例
    _model_runtime = ModelRuntime(
        path=settings_path,
        editable=model_settings.editable_here(),
        chat=cfg,
        embedder=embedder,
        embedding_meta=embedding_meta,
        store=vector_store,
        llm_components=[_orchestrator, _tool_manager, _memory, _evaluator],
        llm_info=_llm_info,
    )
    if cfg["source"] == "page":
        # 上面各组件是按 .env 的协议造的客户端；页面保存的那份可能是另一家，统一换一遍
        _model_runtime.apply_chat(cfg)
    model_admin.install(_model_runtime)
    if settings_path:
        # 不放进 _background_tasks：那里的任务停机时要等它们跑完，这个循环不会自己结束
        _model_watch_task = asyncio.create_task(_model_runtime.watch())
        logger.info(
            f"模型连接可在控制台配置（{'可修改' if _model_runtime.editable else '只读，跟随员工实例'}）"
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
    if _model_watch_task is not None:
        _model_watch_task.cancel()
    switch = getattr(_model_runtime, "_task", None)
    if switch is not None and not switch.done():
        # 向量切换做到一半：取消它。换向量在一个事务里，没提交就是没换
        switch.cancel()
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
    # 排在后台任务之后：画像更新这类任务的模型调用也记在那一轮里，等它们跑完再写完日志
    await _event_log.close(timeout_s=float(os.getenv("SHUTDOWN_DRAIN_TIMEOUT_S", "10")))
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


class _AsciiJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


@app.exception_handler(RequestValidationError)
async def _request_validation_error(request: HttpRequest, exc: RequestValidationError) -> JSONResponse:
    """
    和 FastAPI 默认的一样回 422 和 detail，只是按 ASCII 转义。

    detail 里会回显没通过校验的原值。原值里有孤立代理字符时（/chat 挡掉的坏 id 正是这种），
    默认的 UTF-8 编码会抛错，客户端拿到的就成了 500。
    """
    return _AsciiJSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


from app.agents.tool_loop import LoopConfig, LoopSetup, deadline_for, deadline_warnings  # noqa: E402
from app.agents.turn_prep import TurnDeps, prepare_turn  # noqa: E402
from app.core.metrics import (  # noqa: E402
    RATE_LIMIT_HITS,
    TICKET_NOTIFY_FAILURES,
    TICKET_NOTIFY_PENDING,
    TURN_PROJECTION_MISMATCH,
    UNMATCHED_PATH,
    observe_http,
)
from app.core.telemetry import current_trace_id, setup_telemetry, tag_current_span  # noqa: E402
from app.tools.knowledge_base import UnknownCategory  # noqa: E402
from app.tools.turn_tools import Prefetch, loop_tools  # noqa: E402

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


async def _create_handoff_ticket(
    result, user_id: str, conv_id: str, message: str, turn_id: str = "",
) -> str | None:
    """
    转人工时落工单。工单创建失败不能影响用户拿到回复，只记录错误。

    判了转人工就发一条 ticket_created，没建成也发（status=not_created）：和响应里报给
    工作台的那三个字段同一份（_ticket_fields）。
    """
    if not result.handoff:
        return None
    ticket = None
    if _ticket_store is not None:
        try:
            ticket = await _ticket_store.create(
                conv_id=conv_id,
                user_id=user_id,
                reason=result.handoff_reason,
                request_id=current_request_id(),
                intent=result.intent.value if result.intent else "",
                answer_confidence=result.answer_confidence,
                last_message=message,
                turn_id=turn_id,
            )
        except Exception:
            logger.exception("转人工工单创建失败")
    if ticket is not None and _ticket_notifier is not None and _ticket_notifier.enabled:
        # **先落待发送记录再发**（TicketNotifier.enqueue 内部做的第一件事）。
        # 反过来的话，发送与落记录之间进程挂掉，这次通知就没有任何痕迹——
        # 那正是原来的形态：一个 fire-and-forget task，连 HTTP 状态都不看。
        _spawn_background(_ticket_notifier.enqueue(ticket))
    ticket_id = ticket.id if ticket is not None else None
    fields = _ticket_fields(result, ticket_id)
    events().emit("ticket_created", {"ticket_id": fields["ticket_id"], "status": fields["ticket_status"],
                                     "notification": fields["ticket_notification"]})
    return ticket_id


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
def _id_problem(value: str) -> str:
    """
    客户端送来的 conv_id、user_id 能不能收：不能收时说为什么，能收返回空串。

    这两个 id 会原样进审计、工单和事件日志。NUL、控制字符、孤立代理字符存不进 Postgres，
    超长的撑破索引项：这一轮自己的审计和工单就写不进去，等于给了客户一个绕过审计的办法。
    上限和事件日志里 id 的上限一致（events/recorder.MAX_ID_CHARS），放行的都能原样记下来。
    正常客户端用 UUID，不受影响。
    """
    if len(value) > MAX_ID_CHARS:
        return f"不能超过 {MAX_ID_CHARS} 个字"
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return "不能含控制字符"
        if 0xD800 <= code <= 0xDFFF:
            return "不能含孤立代理字符"
    return ""


class ChatRequest(BaseModel):
    message:     str
    user_id:     str = "anonymous"
    conv_id:     str | None = None

    @field_validator("user_id", "conv_id")
    @classmethod
    def _storable_id(cls, value: str | None) -> str | None:
        """/chat 和 /chat/stream 共用这一处：坏 id 直接 422，不进这一轮。message 不在这里管。"""
        if value is not None and (problem := _id_problem(value)):
            raise ValueError(problem)
        return value


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
    # 这一轮的 id，服务端生成（request_id 可能由客户端决定，不能当主键）。事件日志、
    # 审计和工单都按它指回这一轮；日志关着时也有
    turn_id: str = ""
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
    # 工具循环打开时才有（见 agents/tool_loop.py）：这一轮的调用轨迹——规则预取的两条
    # 在前、模型作答时自己发起的在后——和作答阶段的模型用量（调用次数、输入输出 token；
    # 被要求直接作答时带 forced，退回单次生成时带 fallback）
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
    answer_usage: dict[str, int | str] = Field(default_factory=dict)


# ── 路由 ──────────────────────────────────────────────────────────────────────
# 模型连接的管理接口（api/model_admin.py）。带员工钥匙这一层和其它写接口一样；
# 能不能改还要过它自己那道"本机控制台发来的"检查。
from app.api import model_admin as _model_admin  # noqa: E402

app.include_router(_model_admin.router, dependencies=[Depends(require_api_key)])


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
        # 工具循环（AI_HELPDESK_TOOL_LOOP）开没开；tools_rejected 为 true 说明模型网关明确
        # 拒绝过工具调用，专家因此退回了单次生成——开关开着，实际没在用。
        "tool_loop": {
            "enabled": _LOOP_CONFIG is not None,
            "tools_rejected": tools_rejected(),
        },
        # 事件日志开没开、写进去多少、丢了多少。没开成（实例没设）时 disabled_reason 写明原因；
        # last_error 只有异常类名：这个接口在客户实例上是匿名可读的
        "event_log": _event_log.health(),
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


def _prefetch() -> Prefetch:
    """
    规则预取那两路查询，绑定到当前进程的工具上（实现见 tools/turn_tools.py）。

    每次现取、不缓存：换模型、测试替换改的都是这几个模块级变量。
    """
    return Prefetch(tool_manager=_tool_manager, sql_agent=_sql_agent, label_sources=_label_sources)


# 工具循环（agents/tool_loop.py）：AI_HELPDESK_TOOL_LOOP 没打开时为 None，默认关闭。
# 进程启动时读一次，改了要重启。
_LOOP_CONFIG = LoopConfig.from_env()


def _loop_setup(prefetch: Prefetch) -> LoopSetup | None:
    """工具循环打开时，这一轮能用的工具（绑定当前进程的工具）和边界；关着返回 None。"""
    if _LOOP_CONFIG is None:
        return None
    return LoopSetup(tools=loop_tools(prefetch), config=_LOOP_CONFIG)


def _log_loop_config() -> None:
    """
    启动时说一声工具循环用的是哪些工具、墙钟上限多少；显式配的上限让查库调不动时告警。

    要在工具都注册完之后调：查库工具在不在、超时多少，要看这个进程有没有接 SQL Agent。
    """
    if _LOOP_CONFIG is None:
        return
    tools = list(loop_tools(_prefetch()).values())
    for warning in deadline_warnings(_LOOP_CONFIG, tools):
        logger.warning("启动检查: 工具循环 %s", warning)
    source = "显式配置" if _LOOP_CONFIG.budget.deadline_s is not None else "按慢工具的超时推算"
    logger.info("工具循环已打开：工具 %s，墙钟上限 %g 秒（%s；只能查知识库的专家是 %g 秒）",
                ", ".join(t.name for t in tools) or "无",
                deadline_for(_LOOP_CONFIG.budget, tools), source,
                deadline_for(_LOOP_CONFIG.budget, [t for t in tools if not t.slow]))


async def _prepare_request(req: "ChatRequest", conv_id: str, user_id: str, progress_cb=None):
    """
    /chat 和 /chat/stream 共用的准备阶段，实现见 `agents/turn_prep.prepare_turn`。

    返回 (编排请求, 意图结果, 是否用到知识库, 依据记录)。这里只按当前进程的单例
    装配依赖：记忆、意图识别、模型服务的保护状态、两路预取，以及工具循环（打开时）。
    """
    prefetch = _prefetch()
    return await prepare_turn(
        req.message,
        user_id=user_id,
        conv_id=conv_id,
        # 与响应头 X-Request-ID 同源，编排器日志和 API 日志能互相对上
        request_id=current_request_id(),
        deps=TurnDeps(
            load_memory=_memory.get_context,
            recognize_intent=_orchestrator.recognize_intent,
            outage_reason=_model_unavailable_reason,
            prefetch=prefetch,
            loop=_loop_setup(prefetch),
        ),
        progress_cb=progress_cb,
    )


def _evidence_fields(evidence_trace: dict[str, Any]) -> dict[str, Any]:
    """依据记录 → 响应字段。/chat 的响应体和 /chat/stream 的 meta 事件用同一份。"""
    knowledge = dict(evidence_trace.get("knowledge") or {})
    return {
        "knowledge_sources": knowledge.pop("sources", []),
        "knowledge": knowledge,
        "business_data": dict(evidence_trace.get("business_data") or {}),
    }


# ── 一轮的开始和结束（事件日志，见 app/events/）──────────────────────────────

def _release() -> str:
    """这个进程跑的是哪一版：镜像构建时写进 AI_HELPDESK_RELEASE（git 短提交号）。"""
    return os.getenv("AI_HELPDESK_RELEASE", "").strip() or app.version


def _loop_summary() -> dict[str, Any]:
    """turn_started 里的工具循环：开没开、预算、每类专家的白名单、这个实例接了哪些工具。"""
    if _LOOP_CONFIG is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "budget": dataclasses.asdict(_LOOP_CONFIG.budget),
        "allow": {agent: list(tools) for agent, tools in _LOOP_CONFIG.allow.items()},
        "tools": sorted(loop_tools(_prefetch())),
    }


def _start_turn(transport: str, conv_id: str, user_id: str, message: str) -> TurnRecorder:
    """
    一轮开始：分配 turn_id，发 turn_started。在身份解析和限流之后调——被拒之门外的
    请求（401、429）不算一轮。结束时必须 `turn.close()`（同会话在途表）。
    """
    turn = _event_log.open_turn(conv_id=conv_id, request_id=current_request_id())
    # trace 和日志互相指：trace 里能看到 turn_id，turn_started 里记 trace_id
    tag_current_span("helpdesk.turn_id", turn.turn_id)
    turn.emit("turn_started", {
        "transport": transport,
        "release": _release(),
        "trace_id": current_trace_id(),
        "llm": {"provider": _llm_info.get("provider", ""), "model": _llm_info.get("model", "")},
        "answer_effort": ANSWER_EFFORT,
        "loop": _loop_summary(),
        "cross_session": bool(getattr(_memory, "cross_session", False)),
        "data_enabled": bool(_sql_agent is not None and _sql_agent.enabled),
        "concurrent": turn.concurrent,
    }, {"user_id": user_id, "message": message})
    return turn


def _turn_outcome(result) -> str:
    if result.service_unavailable:
        return "unavailable"
    if result.handoff:
        return "handoff"
    if result.needs_clarification:
        return "clarification"
    return "answered"


def _complete_turn(turn: TurnRecorder, result, ticket_id: str | None, shown: str | None = None) -> None:
    """
    发 turn_completed：客户看到了什么。在返回响应、发 result 之前调。

    shown 是流式实际发出去的全文。答到一半模型失败时，它比 result.response 多出前面那半截
    （result.response 只是故障提示），partial 据此判断，body 里两段都留；/chat 不传，就是
    result.response。
    """
    shown = result.response if shown is None else shown
    body = {"response": result.response}
    if shown != result.response:
        body["shown"] = shown
    turn.emit("turn_completed", {
        "outcome": _turn_outcome(result),
        "agent_type": result.agent_type.value,
        "agent_types": [a.value for a in result.agent_types],
        "primary": result.primary_agent.value if result.primary_agent else result.agent_type.value,
        "supporting": [a.value for a in result.supporting_agents],
        "escalated": result.escalated,
        "handoff": result.handoff,
        "needs_clarification": result.needs_clarification,
        "answer_confidence": result.answer_confidence,
        "partial": bool(result.service_unavailable and len(shown) > len(result.response)),
        "service_unavailable": result.service_unavailable,
        "unavailable_reason": result.unavailable_reason,
        "violations": len(result.policy_violations),
        "ticket_id": ticket_id,
        "latency_ms": round(result.latency_ms, 1),
        "totals": turn.totals(),
    }, body)


def _shadow_check(turn: TurnRecorder, **sent: dict[str, Any]) -> None:
    """
    影子校验：拿这一轮内存里的事件做一次投影，和真正发出去的比（不读库，开销可以忽略）。

    事件从十几个节点发出，迟早有人改了响应、忘了事件。对不上只记日志和指标
    （turn_projection_mismatch_total），不影响这一轮。日志里只报字段名，不报值。
    只在记原文时比：只记结构时，投影里本来就没有原文。
    """
    if turn.mode != "full":
        return
    try:
        projected = project_turn(turn.events)
        wrong = [f"{part}.{key}" for part, payload in sent.items()
                 for key in mismatches(projected[part], payload)]
    except Exception:
        logger.warning("影子校验没跑成（turn %s）", turn.turn_id, exc_info=True)
        return
    if wrong:
        TURN_PROJECTION_MISMATCH.inc()
        logger.warning("事件投影和响应对不上（turn %s）: %s", turn.turn_id, ", ".join(wrong))


def _fail_turn(turn: TurnRecorder, ex: BaseException, stage: str) -> None:
    """未捕获的异常：发 turn_failed。异常信息可能回显输入，放 body。"""
    turn.emit("turn_failed", {"error_type": type(ex).__name__, "stage": stage},
              {"error": str(ex)[:2000]})


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
    turn = _start_turn("chat", conv_id, user_id, req.message)
    stage = "prepare"
    try:
        with bind(turn):
            orch_req, intent_result, knowledge_used, evidence_trace = await _prepare_request(
                req, conv_id, user_id
            )

            # 3. 执行
            stage = "answer"
            result = await _orchestrator.run(orch_req)

            # 4. 转人工 → 落工单（失败不影响响应）
            stage = "after"
            ticket_id = await _create_handoff_ticket(result, user_id, conv_id, req.message,
                                                     turn.turn_id)

            # 5. 写入记忆 + 审计留档（审计写入不阻塞响应）
            await _memory.add_message(user_id, conv_id, MsgRole.USER, req.message)
            await _memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)
            _spawn_background(_log_turn(conv_id, user_id, req.message, result.response,
                                        result.agent_type.value, current_request_id(),
                                        turn.turn_id))

            # 6. 异步更新用户画像（不阻塞响应）。没答上来的轮次不提炼：那一轮的"助手发言"
            #    是一句故障提示，里面没有客户的任何偏好，提炼只是再花一次模型调用。
            #    任务建在 bind 里面，它的模型调用也记在这一轮（排在 turn_completed 后面）。
            if not result.service_unavailable:
                _spawn_background(_memory.update_profile(user_id, conv_id))
            _complete_turn(turn, result, ticket_id)
    except Exception as ex:
        _fail_turn(turn, ex, stage)
        raise
    finally:
        if not turn.terminal:
            # 没有终结事件就出来了：请求被取消（客户端断开、停机）
            turn.emit("turn_aborted", {"stage": stage, "sent_chars": 0})
        turn.close()

    response = ChatResponse(
        conv_id=conv_id,
        request_id=current_request_id(),
        turn_id=turn.turn_id,
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
        trajectory=result.trajectory,
        answer_usage=result.answer_usage,
    )
    payload = response.model_dump()
    _shadow_check(turn, meta=payload, result=payload)
    return response


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
        # 在生成器里开始这一轮：响应一个字节都没发出去就断开的，生成器根本不会跑，
        # 也就不留一条没有结尾的轮次
        turn = _start_turn("stream", conv_id, user_id, req.message)
        # 走到哪一步、客户已经看到了哪些字：中途断开时记进 turn_aborted
        stage, shown = "prepare", []
        with bind(turn):
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
                meta = {
                    "conv_id": conv_id,
                    "turn_id": turn.turn_id,
                    "intent": intent_result.intent.value,
                    "intent_group": intent_result.intent_group,
                    "intent_confidence": round(intent_result.confidence, 4),
                    "intent_strategy": intent_result.strategy,
                    "intent_latency_ms": round(intent_result.latency_ms, 2),
                    "intent_source_scores": intent_result.source_scores,
                    "entities": intent_result.entities,
                    "knowledge_used": knowledge_used,
                    **_evidence_fields(evidence_trace),
                }
                yield _sse("meta", meta)

                # 作答阶段同样边等边报进度、静默发心跳：工具循环打开时，专家作答中途也可能
                # 委派查库（一次十几秒到四十多秒），这段时间编排器一个事件都不出。
                stage = "answer"
                final, routing = None, {}
                answer = _pump_answer(_orchestrator.run_stream(orch_req), progress_q,
                                      heartbeat_s=SSE_HEARTBEAT_S)
                async for source, item in answer:
                    if source == "frame":
                        yield item
                        continue
                    kind = item["type"]
                    if kind == "routing":
                        routing = {k: v for k, v in item.items() if k != "type"}
                        yield _sse("routing", routing)
                    elif kind == "delta":
                        shown.append(item["text"])
                        yield _sse("delta", {"text": item["text"]})
                    elif kind == "tool":
                        yield _sse("progress", _tool_progress(item))
                    elif kind == "result":
                        final = item["result"]

                if final is None:
                    turn.emit("turn_failed", {"error_type": "NoResult", "stage": stage},
                              {"error": "编排器未返回结果"})
                    yield _sse("error", {"detail": "编排器未返回结果"})
                    return

                stage = "after"
                ticket_id = await _create_handoff_ticket(final, user_id, conv_id, req.message,
                                                         turn.turn_id)
                await _memory.add_message(user_id, conv_id, MsgRole.USER, req.message)
                await _memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, final.response)
                _spawn_background(_log_turn(conv_id, user_id, req.message, final.response,
                                            final.agent_type.value, current_request_id(),
                                            turn.turn_id))
                if not final.service_unavailable:      # 同 /chat：没答上来的轮次不提炼画像
                    _spawn_background(_memory.update_profile(user_id, conv_id))
                _complete_turn(turn, final, ticket_id, shown="".join(shown))

                result = {
                    "conv_id": conv_id,
                    "request_id": current_request_id(),
                    "turn_id": turn.turn_id,
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
                    "trajectory": final.trajectory,
                    "answer_usage": final.answer_usage,
                }
                _shadow_check(turn, meta=meta, routing=routing, result=result)
                yield _sse("result", result)
            except Exception as ex:
                logger.exception("流式对话失败")
                _fail_turn(turn, ex, stage)
                yield _sse("error", {"detail": str(ex)})
            finally:
                if not turn.terminal:
                    # 生成器被关掉、终结事件还没发：客户端中途断开了。这里不能 yield，
                    # 也不能 await——emit 是同步的，正是为了能在这里调
                    turn.emit("turn_aborted", {"stage": stage,
                                               "sent_chars": sum(len(t) for t in shown)})
                turn.close()

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

# 工具循环里的两个进度阶段（见 agents/tool_loop.py）。和准备阶段一样带 `helpdesk.` 前缀；
# `label` 只给不认识这两个阶段的旧前端兜底，新前端按 stage 和 tool 自己出文案。
STAGE_TOOL_CALL = "helpdesk.tool_call"
STAGE_TOOL_RESULT = "helpdesk.tool_result"
_TOOL_LABEL = {
    "knowledge_search": "正在检索知识库",
    "order_data_query": "正在查业务系统",
}


def _tool_progress(event: dict[str, Any]) -> dict[str, Any]:
    """
    编排器的 tool 事件 → SSE 的 progress。开始时说查什么，结束时说结局。

    完整的那一条（含检索到的片段、委派的 trace）不在这里发，进 result 事件的 trajectory：
    进度只为让页面在等的时候有话可说，越小越好。
    """
    tool = str(event.get("tool") or "")
    base = {"tool": tool, "call_id": str(event.get("call_id") or ""), "agent": event.get("agent")}
    if event.get("phase") == "start":
        args = event.get("args")
        query = str(args.get("query") or "") if isinstance(args, dict) else ""
        return {"stage": STAGE_TOOL_CALL, "label": _TOOL_LABEL.get(tool, "正在查资料"),
                **base, "query": query[:120]}
    return {
        "stage": STAGE_TOOL_RESULT, "label": "查询完成", **base,
        "outcome": str(event.get("outcome") or ""),
        "evidence": bool(event.get("evidence")),
        "is_error": bool(event.get("is_error")),
        "dedup": bool(event.get("dedup")),
        "latency_ms": event.get("latency_ms"),
    }


async def _pump_answer(events, progress_q, heartbeat_s: float = 15.0):
    """
    作答阶段的泵：编排器的事件流放进一个任务里跑，和进度队列一起等。

    产出 `("event", 编排器事件)` 或 `("frame", 现成的 SSE 帧)`：谁先到先发，两边都静默满
    heartbeat_s 发一条心跳注释。编排器那边的异常在事件流结束后原样抛出，由调用方照旧报成
    error 事件。和 `_pump_until_done` 一样，结束前把队列里剩下的进度发完。
    """
    events_q: asyncio.Queue = asyncio.Queue()
    finished = object()

    async def drive():
        try:
            async for event in events:
                events_q.put_nowait(event)
        finally:
            events_q.put_nowait(finished)

    task = asyncio.create_task(drive())
    next_event = next_progress = None
    try:
        while True:
            next_event = asyncio.ensure_future(events_q.get())
            next_progress = asyncio.ensure_future(progress_q.get())
            done, pending = await asyncio.wait(
                {next_event, next_progress}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED
            )
            # 没等到的那一个取消掉：asyncio.Queue 的 get 被取消不会丢东西，下一圈再取
            for waiter in pending:
                waiter.cancel()
            # 同一刻两边都到了，先发事件：编排器的事件先进队列（比如"开始查库"），
            # 进度是之后才冒出来的（下游查库的分段），按发生的先后发
            event = next_event.result() if next_event in done else None
            if event is not None and event is not finished:
                yield "event", event
            if next_progress in done:
                yield "frame", _sse("progress", next_progress.result())
            if event is finished:
                break
            if not done:
                yield "frame", ": ping\n\n"
        while not progress_q.empty():
            yield "frame", _sse("progress", progress_q.get_nowait())
        await task
    finally:
        # 客户端中途断开时停在这里：编排器那边和两个还在等的 get 都别留着
        for pending_work in (task, next_event, next_progress):
            if pending_work is not None and not pending_work.done():
                pending_work.cancel()


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
    """规则预取的知识库那一路，用当前进程的工具。见 `tools/turn_tools.Prefetch.knowledge_context`。"""
    return await _prefetch().knowledge_context(
        message, intent=intent, top_k=top_k, fast_path=fast_path, trace=trace)


async def _build_order_data_context(
    message: str,
    intent=None,
    entities: dict | None = None,
    user_id: str = "",
    conv_id: str = "",
    progress_cb=None,
    trace: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """规则预取的业务数据那一路，用当前进程的工具。见 `tools/turn_tools.Prefetch.order_data_context`。"""
    return await _prefetch().order_data_context(
        message, intent=intent, entities=entities, user_id=user_id, conv_id=conv_id,
        progress_cb=progress_cb, trace=trace)


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
    assistant_msg: str, agent_type: str, request_id: str, turn_id: str = "",
) -> None:
    """把一轮对话写入审计日志（user + assistant 各一条），带上 turn_id 和实例。"""
    if _conv_log is None:
        return
    await _conv_log.append(conv_id, user_id, "user", user_msg, request_id=request_id,
                           turn_id=turn_id, instance=_INSTANCE)
    await _conv_log.append(conv_id, user_id, "assistant", assistant_msg,
                           request_id=request_id, agent_type=agent_type,
                           turn_id=turn_id, instance=_INSTANCE)


@app.get("/conversations/{conv_id}", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def get_conversation(conv_id: str, limit: int = 500):
    """
    完整对话原文（坐席接工单 / 合规审计视角）。工单里的 conv_id 从这里回查。

    只返回本实例的记录，加上加实例列之前的老记录：两个实例共用这张表，客户自己挑的
    conv_id 可能撞上员工的会话。每条带 turn_id，能找回那一轮的经过。
    """
    if _conv_log is None:
        raise HTTPException(503, "服务未就绪")
    entries = await _conv_log.get_conversation(conv_id, limit=min(limit, 2000), instance=_INSTANCE)
    if not entries:
        raise HTTPException(404, f"对话不存在或已超出留存期: {conv_id}")
    return {"conv_id": conv_id, "messages": [e.to_dict() for e in entries]}


# ── 一轮的经过（事件日志的读接口）──────────────────────────────────────────────
#
# 全部要员工钥匙，只读本实例的行（存储按实例隔离）。AI_HELPDESK_EVENT_LOG_READ=off 时返回
# 404，和接口不存在一样——客户实例这样配；对外的 nginx 本来也只放行 /chat、/chat/stream、
# /health，这是第二层。

def _turn_reads():
    if not _event_log.readable:
        raise HTTPException(404, "Not Found")
    return _event_log.store


@app.get("/conversations/{conv_id}/turns", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def list_conversation_turns(conv_id: str, limit: int = 50):
    """这个会话的各轮，新的在前：结局、作答 Agent、意图、转人工、工单、耗时、完整不完整。"""
    store = _turn_reads()
    turn_ids = await store.turn_ids_of(conv_id, limit=max(1, min(limit, 200)))
    by_turn = await store.events_of_turns(turn_ids)
    return {"conv_id": conv_id,
            "turns": [summarize_turn(by_turn[t]) for t in turn_ids if t in by_turn]}


@app.get("/turns/{turn_id}", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def get_turn(turn_id: str):
    """
    一轮的经过：{turn_id, complete, redacted, meta, routing, result}，三个对象和当轮响应
    （/chat/stream 的 meta、routing、result）同一个形状。工作台刷新之后靠它恢复检查器和
    学习模式；超出保留期、或者当时日志没开，返回 404。
    """
    events = await _turn_reads().events_of(turn_id)
    if not events:
        raise HTTPException(404, f"这一轮的经过不存在或已超出保留期: {turn_id}")
    return project_turn(events)


@app.get("/turns/{turn_id}/events", tags=["Tickets"], dependencies=[Depends(require_api_key)])
async def get_turn_events(turn_id: str):
    """一轮的原始事件，按 seq 排好。"""
    events = await _turn_reads().events_of(turn_id)
    if not events:
        raise HTTPException(404, f"这一轮的经过不存在或已超出保留期: {turn_id}")
    return {"turn_id": turn_id, "events": [e.to_dict() for e in events]}


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
    """单篇文档输入。category 可省（未分类），写 key、中文名或英文名都认。"""
    title:   str
    content: str
    category: str | None = None


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: list[DocInput]


class DocumentPatch(BaseModel):
    """改一篇文档。现在只能改分类：空串是未分类。"""
    category: str


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

    每篇可以带 `category`（分类，见 `GET /knowledge/categories`），写 key、中文名、
    英文名都行，省略是未分类。任何一篇的分类写错了，整批都不导入，返回 400。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "补件流程", "content": "缺件需在验收单注明并提供订单号、柜号和装箱单编号...",
         "category": "delivery"},
        {"title": "交期说明", "content": "标准交期 4-6 周，以工厂排产确认为准...",
         "category": "生产与交期"}
      ]
    }
    ```
    """
    kb = _require_knowledge_base()
    results = await _import_or_503(
        kb,
        [{"title": d.title, "content": d.content, "category": d.category} for d in body.documents],
        source_type="manual",
    )
    return await _import_response(kb, results)


@app.post("/knowledge/upload", tags=["知识库"], dependencies=[Depends(require_api_key)])
async def upload_knowledge(file: UploadFile = File(...), category: str = Form("")):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "...", "category": "..."}, ...]`

    表单里的 `category` 是这次导入的分类：txt / md 就落在这一类；JSON 里每篇自己写了
    `category` 的用自己的，没写的用它。都不给是未分类。

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

    results = await _import_or_503(
        kb, docs, source_type="upload", source_name=filename, category=category
    )
    return await _import_response(kb, results, f"文件 {filename} 导入成功")


async def _import_or_503(kb, docs: list[dict[str, Any]], **source) -> list[dict[str, Any]]:
    """
    导入文档；远端 Embedding 不可用时返回 503，而不是把没有向量的文档写进库。

    一批里排在前面、已经算出向量的文档这时已经入库了。导入是幂等的，
    所以提示里直接说"恢复后把同一份再导一次"——已有的会被跳过，不会重复。
    """
    from app.core.embedder import EmbeddingUnavailable

    if _model_runtime is not None and _model_runtime.switching:
        # 重算向量期间进来的分片不会有新模型的向量，那次切换就换不上
        raise HTTPException(
            409, "正在切换向量模型，完成之前不能导入文档。切换通常在几分钟内完成，之后再导入即可。"
        )
    try:
        return await kb.import_documents(docs, **source)
    except UnknownCategory as ex:
        # 导入前就校验了，这时库里什么都没写
        raise HTTPException(400, f"{ex}这一批没有导入任何文档。") from ex
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
async def list_knowledge_documents(
    q: str = "", limit: int = 100, offset: int = 0, category: str = "",
):
    """
    知识库里有哪些文档。不带 `q` 是浏览全部（新导入的在前）；带 `q` 时按空白
    拆成词，每个词都要在标题或正文里出现，响应里说明是标题命中还是正文命中。

    `category` 只看一个分类（`uncategorized` 是未分类）；不认识的分类返回空列表。
    响应里的 `categories` 是每个分类里满足 `q` 的篇数，不受 `category` 影响。

    这是**按字面找文档**。"用一句问题找相关段落"是另一件事，走 `POST /search`
    ——那条链路会改写问题、做语义召回和重排，对话里用的也是它。

    每篇带 `origin`：`original` 是导入时存下的原文；`legacy` 是加文档表之前
    导入的，正文由当时的分片拼回；`legacy_partial` 是分片不全的历史文档。
    后两种没有来源和文件名可报——当时没记。
    """
    kb = _require_knowledge_base()
    return await kb.list_documents(
        query=q.strip()[:200], limit=max(1, min(limit, 500)), offset=max(0, offset),
        category=category.strip()[:64] or None,
    )


@app.get("/knowledge/categories", tags=["知识库"], dependencies=[Depends(require_api_key)])
async def list_knowledge_categories():
    """
    知识库的分类（按客户从咨询到售后的顺序）和每类的篇数，外加未分类的篇数。

    分类是固定的一组，用来整理和浏览文档；检索不看分类。
    """
    kb = _require_knowledge_base()
    return await kb.category_overview()


@app.patch("/knowledge/documents/{doc_id}", tags=["知识库"],
           dependencies=[Depends(require_api_key)])
async def update_knowledge_document(doc_id: str, body: DocumentPatch):
    """
    改一篇文档的分类（`{"category": "delivery"}`，空串是未分类）。

    只改分类：正文、分片、向量和关键词索引都不动，检索结果不受影响。
    """
    kb = _require_knowledge_base()
    try:
        document = await kb.set_document_category(doc_id, body.category)
    except UnknownCategory as ex:
        raise HTTPException(400, str(ex)) from ex
    if document is None:
        raise HTTPException(404, f"文档不存在: {doc_id}")
    return document


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
