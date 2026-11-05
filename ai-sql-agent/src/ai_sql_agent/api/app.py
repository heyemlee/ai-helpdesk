from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ai_sql_agent import __version__
from ai_sql_agent.audit import AuditRecord, create_audit_store
from ai_sql_agent.auth import ApiKeyRecord
from ai_sql_agent.catalog import DataCatalog
from ai_sql_agent.feedback import (
    CertifiedSqlLibrary,
    FeedbackRecord,
    Verdict,
    create_feedback_store,
)
from ai_sql_agent.core.metrics import FEEDBACK_SUBMITTED, REQUEST_DURATION, REQUESTS
from ai_sql_agent.core import (
    CIRCUIT_REGISTRY,
    MODEL_ACCESS,
    configure_logging,
    get_logger,
    get_settings,
    get_trace_id,
    log_event,
    render_metrics,
    usage_summary,
    use_trace_id,
)
from ai_sql_agent.memory import create_memory_service
from ai_sql_agent.pipeline import PipelineConfig, Text2SQLPipeline, DynamicSqlAgentService
from ai_sql_agent.core.run_context import RunCancelled
from ai_sql_agent.runs import RunManager, create_run_store, runs_of
from ai_sql_agent.runs.disconnect import watch_disconnect

from .limits import RequestLimiter
from .result_summary import (
    StepLogResponse,
    apply_summary_to_audit,
    parse_denied_tables,
    run_outcome,
    summarize_result,
)
from .schema_watcher import SchemaWatcher
from .security import AuthGuard, require_admin, require_api_key

_logger = get_logger("api")

TRACE_HEADER = "X-Trace-Id"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class QueryRequest(BaseModel):
    """自然语言查询请求。"""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    enable_long_term: bool = False


# StepLogResponse 挪到了 result_summary：/query、A2A send、A2A stream 三处共用同一份（契约 §4.4）


class QueryResponse(BaseModel):
    trace_id: str = ""
    """本次查询的链路 ID，与服务端日志一一对应，排查时直接拿它检索。"""

    degraded: bool = False
    """模型不可用时走了降级链路，结果可能来自历史 SQL 重放。"""

    degrade_reason: str = ""

    route: str
    confidence: float
    reason: str
    answer: str
    queried_database: bool
    selected_databases: List[str] = []

    steps: List[StepLogResponse] = []
    """
    逐步骤日志。**契约面的名字是 `steps`**（§4.4.1）。

    A2A 那两条一直叫 `steps`，只有直连 `/query` 叫 `step_logs`——同一个
    `StepLogResponse`，两个名字。后果不是「字段名不好看」：调用方按 §4.6.7
    处理缺失字段，`steps` 取不到就按「缺失 = 不可信」处置，
    **于是每一次直连查询都不再是出处，而链路看起来完全正常**——
    没有报错、没有降级事件、没有任何症状。而 §6.2 的回滚恰好把流量切到这条路上。
    """

    step_logs: List[StepLogResponse] = []
    """
    **已弃用，与 `steps` 内容完全相同。** 过渡期两个字段同时发，不破坏现有消费方；
    删除它要走契约 §6 的变更流程，届时两侧同时删。

    契约明确拒绝了「在契约里承认第三种形状、写一张 step_logs ↔ steps 翻译表」
    ——那是把分叉制度化，此后每个新消费方都要重踩一次。
    """

    has_risk: bool = False
    """任一步骤带有软异常提示。前端据此提醒用户「这条结果需要核对」。"""

    # ---- 契约 §4.6 业务终态语义 ----
    #
    # 与 A2A artifact 的 data part 是同一套字段，取值都来自 summarize_result()
    # 归纳出的 ResultSummary：A2A 那边调 ResultSummary.data_part()；这里不调它，
    # 由 query() 逐字段填进来，两边的对齐靠 tests/test_business_outcome.py 守着。
    # 契约 §4.4 要求两条 transport 能做的信任判断一模一样，「走哪条路
    # 信息更多」本身就是缺陷。
    execution_status: str = "failed"
    """§4.6.1：这次查库到底跑成什么样。缺失/算不出来按 failed 处置。"""

    succeeded_steps: int = 0
    failed_steps: int = 0
    repaired_steps: int = 0
    result_truncated: bool = False
    """§4.6.3：任一步被截断。为真时「共有 N 条 / 全部 / 一共」这类说法都不成立。"""

    as_of: Optional[str] = None
    """§4.6.4：数据时点。为空时调用方不得表述「截至今天 / 目前 / 最新」。"""

    as_of_source: Optional[str] = None
    currency: Optional[str] = None
    """§4.6.5：本轮金额列的币种。多币种时为空，逐列读 step_logs[].column_units。"""


class HistoryItem(BaseModel):
    """一条历史查询（不含结果集，结果不落审计）。"""

    trace_id: str
    created_at: str
    query: str
    session_id: str = ""
    route: str = ""
    databases: List[str] = []
    tables: List[str] = []
    sqls: List[Dict[str, str]] = []
    success: bool = True
    degraded: bool = False

    has_risk: bool = False
    """当时返回给用户时带了软异常提示。历史页据此标「需要核实」。"""

    execution_status: str = ""
    """
    业务终态（契约 §4.6.1）。空串 = 这条记录早于该字段，或请求没跑到出结果；
    调用方此时只能退回看 `success` / `error`，**不得**把空串当成 success。
    """

    failed_steps: int = 0
    repaired_steps: int = 0
    status_code: int = 200
    duration_ms: float = 0.0
    total_tokens: int = 0
    error: Optional[str] = None


class HistoryResponse(BaseModel):
    user_id: str
    items: List[HistoryItem] = []


class FeedbackRequest(BaseModel):
    """
    用户对一次回答的判定。

    只收 trace_id 与判定本身：问题原文与实际执行的 SQL 从审计记录回填，
    不接受客户端传。否则「这条 SQL 是对的」就成了客户端说了算，
    而认证 SQL 会被注入后续查询的生成 Prompt。
    """

    trace_id: str = Field(min_length=1)
    verdict: Literal["correct", "wrong", "unsure"]
    comment: str = Field(default="", max_length=2000)


class FeedbackResponse(BaseModel):
    recorded: bool
    trace_id: str
    verdict: str
    certified: bool = False
    """这条查询是否会作为认证范例参与后续生成。"""

    message: str = ""


class SaveMemoryRequest(BaseModel):
    """用户主动保存结构化结果到个人知识库。"""

    user_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    result: Dict[str, Any]
    database: str = ""
    tables: List[str] = []
    columns: List[str] = []
    sql: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    app.state.auth = AuthGuard()
    app.state.audit = create_audit_store()
    app.state.limiter = RequestLimiter(app.state.audit)

    # 每次请求一条 run 记录 + 事件日志。上一个进程没收尾的 run 在这里标成 interrupted。
    app.state.runs = RunManager(
        create_run_store(),
        max_attr_bytes=settings.run_event_attr_max_bytes,
        retention_days=settings.runs_retention_days,
        deadline_seconds=settings.run_deadline_seconds,
    )
    app.state.runs.sweep_orphans()
    app.state.runs.purge_expired()
    app.state.runs.start_maintenance()

    # 行数上限与执行超时取配置（AI_SQL_MAX_ROWS / AI_SQL_MAX_EXECUTION_SECONDS），经这份
    # PipelineConfig 交给执行器。启动日志也从它读：日志打出来的数就是执行器在用的数。
    pipeline_config = PipelineConfig(
        max_rows=settings.max_rows,
        max_execution_seconds=settings.max_execution_seconds,
        max_result_bytes=settings.max_result_bytes,
        max_value_bytes=settings.max_value_bytes,
    )

    log_event(
        _logger,
        logging.INFO,
        "api.startup",
        auth_mode=settings.auth_mode,
        pgvector=bool(settings.pgvector_dsn),
        model_provider=settings.model_provider,
        chat_model=settings.chat_model,
        coder_model=settings.coder_model,
        model_credentials=settings.has_model_credentials,
        # page = 模型连接是管理员在页面上保存的那一份，压过了环境变量和 .env
        model_settings="page" if settings.page_store.get("values") else "server",
        embedding_backend=settings.embedding_backend,
        embedding_model=settings.embedding_model if settings.embedding_backend == "remote" else "",
        embedding_settings="page" if settings.page_store.get("embedding") else "server",
        audit=settings.audit_enabled,
        schema_refresh_seconds=settings.schema_refresh_seconds,
        rate_limit_per_minute=settings.default_rate_limit_per_minute,
        max_rows=pipeline_config.max_rows,
        max_execution_seconds=pipeline_config.max_execution_seconds,
        max_result_bytes=pipeline_config.max_result_bytes,
        max_value_bytes=pipeline_config.max_value_bytes,
    )

    for section, key in (("chat", "problem"), ("embedding", "embedding_problem")):
        if settings.page_store.get(key):
            # 页面保存过模型连接，但那一节现在用不了：服务照常起，用的是环境变量和 .env 里的那一份
            log_event(
                _logger,
                logging.WARNING,
                "model_settings.unusable",
                path=settings.model_settings_file,
                section=section,
                problem=settings.page_store[key],
            )

    # 记忆库的位置取配置（AI_SQL_MEMORY_DB_PATH）：两个实例各用各的库，对话历史才不会串
    memory = create_memory_service()
    audit_store = app.state.audit
    app.state.feedback = create_feedback_store()

    # 用户点过「对」的查询 → 同类问题的生成范例。
    # 关掉开关时不注入：这条链路会占 Prompt 预算，值不值得要能对照测量。
    certified_library = (
        CertifiedSqlLibrary(
            loader=lambda user_id: app.state.feedback.certified(user_id=user_id),
            top_k=settings.certified_sql_top_k,
        )
        if settings.certified_sql_enabled and settings.feedback_enabled
        else None
    )
    app.state.certified_library = certified_library

    pipeline = Text2SQLPipeline(
        pipeline_config,
        # 模型不可用时可以重放这位用户同一问题上次成功的 SQL
        sql_history_lookup=audit_store.last_successful_sqls,
        # 模型正常时，把确认过正确的相似查询当写法参考注入
        certified_lookup=certified_library.lookup if certified_library else None,
    )

    app.state.memory = memory
    app.state.pipeline = pipeline
    app.state.service = DynamicSqlAgentService(pipeline, memory)

    # 数据目录与只读预览。与问答共用 Pipeline 的语义层、库连接和执行路由，
    # 所以「目录里看得到的表」与「提问查得到的表」是同一份事实。
    app.state.catalog = DataCatalog.from_pipeline(pipeline)

    app.state.schema_watcher = SchemaWatcher(
        pipeline, settings.schema_refresh_seconds
    )
    app.state.schema_watcher.start()

    app.state.readonly_audits = _audit_business_databases(pipeline)

    # 生产模式的启动闸：不合格就**不启动**，不是打个警告继续跑。
    check_production_readiness(app)

    try:
        yield
    finally:
        _shutdown(app)


class ProductionReadinessError(RuntimeError):
    """生产模式下配置不合格。进程应当起不来。"""


def check_production_readiness(app: FastAPI) -> List[str]:
    """
    生产模式（`AI_SQL_APP_ENV=production`）的启动闸。

    以前这些都是「警告」：readiness 明确把「缺模型凭证走 Mock」当成可就绪，
    启动只读审计发现违规也只是 log 一条 WARNING。**警告在生产里等于没有**——
    没有人会在凌晨看日志，而这几项里任何一项不合格，服务都会以一种看起来
    完全正常的方式给出错误答案：Mock 模型编 SQL、示例库返回演示数据、
    权限没配就是全放开、账号能写就等着某天模型生成一条 DELETE。

    开发模式下全部降级为警告——本地跑 demo 与测试不该被这些挡住。
    返回问题清单，方便测试直接断言。
    """
    settings = get_settings()
    problems: List[str] = []
    scope = settings.deployment_scope

    # 部署范围必须显式声明。这一条排在最前面，因为下面有两条检查**由它决定
    # 该不该生效**——范围没定就判不了那两条，只能先把范围问出来。
    #
    # 为什么要有它：2026-09 定的第一次上线范围是「同权限内部人员 + 只读」的
    # 试点，这个范围让三条硬阻断降级为可接受（最小授权没落到真实业务库、
    # 行范围不启用、impersonation 不启用）。**那三条降级只在系统确实跑在这个
    # 范围里时才成立**，而在这条断言之前，"这是单租户部署"只写在文档里。
    # 一个不被代码强制的范围决定不是决定，是期望。
    if not scope:
        problems.append(
            "没有声明 AI_SQL_DEPLOYMENT_SCOPE。生产部署必须显式说明自己是哪种范围："
            "internal_single_tenant（同权限内部人员、所有 Key 看到的数据范围相同）"
            "或 multi_tenant（不同最终用户只能看各自的数据）。"
            "**这个配置故意没有默认值**：漏配和想清楚了配成某个值，风险完全不同。"
        )

    if not settings.has_model_credentials:
        problems.append(
            "没有模型凭证，链路会走 Mock。Mock 会编出语法正确但内容错误的 SQL，"
            "而且不会有任何症状。"
        )
    if settings.auth_mode != "api_key":
        problems.append(
            f"AI_SQL_AUTH_MODE={settings.auth_mode}：鉴权关闭时所有请求都是"
            "匿名身份、全部权限。"
        )
    if settings.unknown_role_policy != "deny":
        problems.append(
            "AI_SQL_UNKNOWN_ROLE_POLICY=full_access：角色没匹配到策略就给全部权限，"
            "配错一个角色名的后果是全库可读。"
        )
    if not settings.semantic_dir:
        problems.append(
            "没有配置 AI_SQL_SEMANTIC_DIR，用的是内置演示元数据与演示库。"
        )

    unknown_databases = _policy_databases_not_in_semantic_layer(app)
    if unknown_databases:
        problems.append(
            "这些策略引用了语义层里没有的库标识符："
            + "、".join(unknown_databases)
            + "。deny by default 之下，这类配错的后果是那把 Key 什么都查不到，"
            "而且没有任何症状。"
        )

    if scope == "internal_single_tenant":
        # 单租户范围里 impersonation 不解决任何问题（所有人权限相同）。
        # 打开它说明有人**以为**这套部署在按最终用户隔离数据——那个误解本身
        # 就是风险：它会让人把系统用在范围之外，而系统照常工作。
        impersonating = _keys_with_impersonation(app)
        if impersonating:
            problems.append(
                "AI_SQL_DEPLOYMENT_SCOPE=internal_single_tenant，但这些 Key 允许"
                " impersonation：" + "、".join(impersonating)
                + "。同权限范围里冒充谁都一样，打开它只会让人以为这里在做数据隔离。"
                "要做真隔离，把范围改成 multi_tenant 并读那一档的要求。"
            )
    else:
        impersonators = _impersonating_keys_without_user_policies(app)
        if impersonators:
            problems.append(
                "这些 Key 允许 impersonation，但配置里没有 users 段（最终用户的角色）："
                + "、".join(impersonators)
                + "。那样每个最终用户拿到的都是**服务身份的全部权限**，"
                "换 user_id 不换权限。见 docs/authz-model.md。"
            )

    # 行范围只在 multi_tenant 下是必须的。单租户范围里所有人看到的数据相同，
    # 要求为每张表写一条 predicate: "*" 只是噪音——而噪音会让人开始
    # 机械地复制粘贴那一行，那时它就不再是"有人写下来的"了，
    # 这条检查存在的理由（默认值兜不出安全）反而被磨掉。
    undeclared_rows = [] if scope == "internal_single_tenant" else _tables_without_row_scope(app)
    if undeclared_rows:
        problems.append(
            "这些策略覆盖到的表没有声明行范围（row_filters）："
            + "、".join(undeclared_rows)
            + "。不声明时运行时按放行处理，所以「这张表不做行隔离」必须是有人"
            "写下来的，而不是默认值兜出来的——写 {\"table\": \"…\", "
            "\"predicate\": \"*\"} 即可。"
        )

    keys_without_roles = _keys_without_roles(app)
    if keys_without_roles:
        problems.append(
            "这些 API Key 没有声明 roles，等于不做数据权限限制："
            + "、".join(keys_without_roles)
        )

    writable = [
        user
        for user, verdict in getattr(app.state, "readonly_audits", {}).items()
        if verdict != "readonly"
    ]
    if writable:
        problems.append(
            "这些业务库账号不是只读：" + "、".join(writable)
            + "。应用层校验是第二道防线，不能是唯一一道。"
        )

    if settings.app_env != "production":
        for problem in problems:
            log_event(_logger, logging.WARNING, "production_readiness.warning",
                      problem=problem, app_env=settings.app_env)
        return problems

    if problems:
        for problem in problems:
            log_event(_logger, logging.ERROR, "production_readiness.failed",
                      problem=problem)
        raise ProductionReadinessError(
            "生产模式启动检查未通过：\n  - " + "\n  - ".join(problems)
        )

    log_event(_logger, logging.INFO, "production_readiness.ok")
    return []


def _policy_databases_not_in_semantic_layer(app: FastAPI) -> List[str]:
    """
    策略里写的库标识符，语义层里有没有。

    这类错误现在是**完全静默**的：`deploy/api_keys.example.json` 一度还写着
    `pm_db` / `mes_db`（契约 v1.6 已标退役），配上 deny by default 之后
    那把 Key 什么都查不到——既没有报错，也没有「你没权限」的提示，
    因为策略语法完全合法，只是指向了一个不存在的库。

    返回 `角色:库名` 的列表。含通配符的库段跳过（`*` 本来就是「所有库」）。
    """
    store = getattr(getattr(app.state, "auth", None), "store", None)
    pipeline = getattr(app.state, "pipeline", None)
    if store is None or pipeline is None:
        return []

    known = {database.name for database in pipeline.databases}
    if not known:
        return []

    found: List[str] = []
    for role, policy in getattr(store, "policies", {}).items():
        rules = (
            list(policy.allow_tables)
            + list(policy.deny_tables)
            + list(policy.deny_columns)
        )
        for rule in rules:
            head = rule.split(".", 1)[0]
            if not head or any(ch in head for ch in "*?["):
                continue
            if head not in known:
                found.append(f"{role}:{head}")
    return sorted(set(found))


def _keys_with_impersonation(app: FastAPI) -> List[str]:
    """
    允许 impersonation 的 Key，**不管配没配 `users` 段**。

    与下面那个的区别是判据不同，不是宽松版：
    `_impersonating_keys_without_user_policies` 问的是"打开了却没配全"，
    这个问的是"打开了没有"——因为在 `internal_single_tenant` 范围里，
    配全了也不该打开：所有人权限相同，冒充谁都一样，
    而打开它会让人以为这套部署在按最终用户隔离数据。
    """
    store = getattr(getattr(app.state, "auth", None), "store", None)
    if store is None:
        return []
    return sorted(
        record.caller
        for record in store.records.values()
        if record.can_impersonate
    )


def _impersonating_keys_without_user_policies(app: FastAPI) -> List[str]:
    """
    允许 impersonation、却没有 `users` 段的 Key。

    没有 `users` 段时 impersonation 只换 `user_id` 不换权限——alice 和 bob 权限
    完全相同。运行时保持这个旧行为是为了不破坏现有部署；上生产之前必须写下来。
    """
    store = getattr(getattr(app.state, "auth", None), "store", None)
    if store is None or getattr(store, "users", None):
        return []
    return sorted(
        record.caller
        for record in store.records.values()
        if record.can_impersonate
    )


def _tables_without_row_scope(app: FastAPI) -> List[str]:
    """
    策略允许、但一条 `row_filters` 都没匹配上的表（设计见 docs/authz-model.md）。

    返回 `角色:库.表`，最多列 10 条——一个新部署可能几十张表都没声明，
    错误信息刷屏帮不上忙。
    """
    store = getattr(getattr(app.state, "auth", None), "store", None)
    pipeline = getattr(app.state, "pipeline", None)
    if store is None or pipeline is None:
        return []

    from ai_sql_agent.auth.identifiers import bind_dialect

    metas = getattr(pipeline, "business_metas", {}) or {}
    dialects = {
        database.name: getattr(database, "dialect", "") or ""
        for database in getattr(pipeline, "databases", []) or []
    }
    found: List[str] = []
    for role, policy in getattr(store, "policies", {}).items():
        if not hasattr(policy, "row_predicate"):
            continue
        for database, tables in metas.items():
            # 表名的大小写按这个库的方言比，与关卡同一条规则
            bound = bind_dialect(policy, dialects.get(database, ""))
            for table in tables:
                if not bound.allows_table(database, table):
                    continue
                if bound.row_predicate(database, table) is None:
                    found.append(f"{role}:{database}.{table}")
    return sorted(found)[:10]


def _keys_without_roles(app: FastAPI) -> List[str]:
    store = getattr(getattr(app.state, "auth", None), "store", None)
    if store is None:
        return []
    return sorted(
        record.caller
        for record in store.records.values()
        if not record.roles
    )


def _audit_business_databases(pipeline) -> Dict[str, Any]:
    """
    启动时体检业务库账号是不是真的只读。

    只读约束有三层（应用层 SQL 校验、连接层只读事务、数据库账号权限），
    第三层配错了平时看不出来——直到某天模型生成了一条 DELETE。
    这里在启动时就把问题喊出来，而不是等出事。
    """
    from ai_sql_agent.ops import audit_business_databases

    summary: Dict[str, Any] = {}
    try:
        audits = audit_business_databases(pipeline.databases)
    except Exception as exc:
        log_event(
            _logger,
            logging.WARNING,
            "readonly_audit.failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return summary

    for audit in audits:
        summary[audit.user] = "readonly" if audit.is_readonly else "writable"
        if audit.is_readonly:
            log_event(
                _logger, logging.INFO, "readonly_audit.ok", user=audit.user
            )
        else:
            log_event(
                _logger,
                logging.WARNING,
                "readonly_audit.violation",
                user=audit.user,
                problems=audit.problems(),
                hint="用 python -m ai_sql_agent.ops provision 建只读账号后改用它的 DSN",
            )
    return summary


def _shutdown(app: FastAPI) -> None:
    """优雅关闭：等待记忆摘要线程收尾，释放 HTTP 连接池。"""
    from ai_sql_agent.a2a.server import shutdown_stream_executor
    from ai_sql_agent.core.transport import close_http_client

    log_event(_logger, logging.INFO, "api.shutdown", stage="begin")
    shutdown_stream_executor()

    watcher = getattr(app.state, "schema_watcher", None)
    if watcher is not None:
        watcher.stop()

    memory = getattr(app.state, "memory", None)
    if memory is not None:
        memory.close(wait=True)

    runs = runs_of(app)
    if runs is not None:
        runs.shutdown()

    close_http_client()

    log_event(_logger, logging.INFO, "api.shutdown", stage="done")


app = FastAPI(
    title="AI SQL Agent API",
    description="自然语言数据查询 Agent：动态路由 + 三层 Schema 检索 + "
    "CoT 规划 + SQL 生成 + MCP 路由执行 + 长短期记忆。",
    version=__version__,
    lifespan=lifespan,
)


# A2A：Agent Card + JSON-RPC /a2a（message/send、message/stream）。
# 与 /query 同一套鉴权、限流、审计；流式开关见 settings.a2a_streaming。
from ai_sql_agent.a2a import router as _a2a_router  # noqa: E402

app.include_router(_a2a_router)

# 数据浏览：目录 + 只读预览（/catalog …）。与 /query 同一套身份与表列权限。
from .catalog import router as _catalog_router  # noqa: E402

app.include_router(_catalog_router)

# 模型连接：管理员在页面上查看 / 测试 / 保存 / 恢复（/admin/model …）。
from .model_admin import router as _model_admin_router  # noqa: E402

app.include_router(_model_admin_router)

# 运行记录：GET /runs/{id}、/runs/{id}/events，POST /runs/{id}/cancel。只看得到自己的 run。
from .runs import router as _runs_router  # noqa: E402

app.include_router(_runs_router)


class _ConsoleStaticFiles(StaticFiles):
    """
    页面的 CSS / JS。每次都让浏览器回来确认一下（ETag 命中就是 304）。

    没有构建步骤也就没有带哈希的文件名；不加这一条，浏览器会按启发式缓存
    旧脚本，升级之后出现「页面是新的、脚本是旧的」，而且只在部分人那里出现。
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _ConsoleStaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """
    为每个请求绑定 trace_id，并记录一条请求级汇总日志。

    调用方可通过 X-Trace-Id 头透传上游链路 ID；响应头原样返回，
    便于前端把用户看到的一次查询和后端日志对上。
    """
    incoming = request.headers.get(TRACE_HEADER, "")

    with use_trace_id(incoming) as trace_id:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            path = request.url.path
            metric_path = _metric_path(request, status_code)
            REQUESTS.inc(
                method=request.method, path=metric_path, status=str(status_code)
            )
            REQUEST_DURATION.observe(
                elapsed, method=request.method, path=metric_path
            )
            log_event(
                _logger,
                logging.INFO,
                "api.request",
                method=request.method,
                path=path,
                status_code=status_code,
                duration_ms=round(elapsed * 1000, 2),
                usage=usage_summary(),
            )


def _metric_path(request: Request, status_code: int) -> str:
    """
    指标里的 path 标签。标签的取值必须是有限的一组：

    - 命中的接口用路由模板（`/catalog/{database}/{table}`），不用原始路径——
      否则标签数跟着表的数量涨；
    - 静态文件统一记成 `/static`：这个挂载点不走鉴权，谁都能拿任意文件名来请求；
    - 没命中任何路由的 404 统一记成 `unmatched`：扫描器发来的每一个随机路径
      都各占一个标签的话，指标迟早被撑爆。
    """
    path = request.url.path
    template = getattr(request.scope.get("route"), "path", None)
    if template:
        return template
    if path == "/static" or path.startswith("/static/"):
        return "/static"
    if status_code == 404:
        return "unmatched"
    return path


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """
    查询界面：业务查数、数据浏览、历史查询三个入口。

    原生 HTML / CSS / ES Module，无构建步骤：`static/index.html` 是外壳，
    样式与脚本在 `/static` 下按职责分文件。页面自己不做鉴权，API Key 只存在
    当前标签页，请求时放在 X-API-Key 头里；业务数据一律经接口取，页面不直连库。
    """
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/health")
def health() -> Dict[str, str]:
    """
    存活探针：只回答“进程还在不在”，不查任何外部依赖。

    依赖抖动不应该让编排系统重启进程，那是就绪探针的职责。
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> Dict[str, Any]:
    """
    就绪探针：检查外部依赖，任一不可用就摘流量但不重启。

    检查项：记忆库可读写、向量后端可用（配置了 pgvector 时真连一次）、
    审计存储可写、模型凭证是否配置（缺失只降级为 Mock，不算不就绪）。
    """
    settings = get_settings()
    checks: Dict[str, Any] = {}
    ready = True

    try:
        app.state.memory.store.list_messages(user_id="__probe__", session_id="__probe__")
        checks["memory_store"] = "ok"
    except Exception as exc:
        checks["memory_store"] = f"error: {exc}"
        ready = False

    try:
        app.state.audit.daily_usage("__probe__")
        checks["audit_store"] = "ok"
    except Exception as exc:
        checks["audit_store"] = f"error: {exc}"
        ready = False

    if settings.pgvector_dsn:
        try:
            import psycopg

            conn = psycopg.connect(settings.pgvector_dsn, connect_timeout=3)
            conn.close()
            checks["pgvector"] = "ok"
        except Exception as exc:
            checks["pgvector"] = f"error: {exc}"
            ready = False
    else:
        checks["pgvector"] = "not_configured (内存向量索引)"

    # 已知的模型访问故障：只读本进程记下的状态，**不为此去探测模型**。
    # 这把 Key 被服务端拒过之后，「配置了 Key」不再等于「能调模型」——以前这里照样写 ok。
    access_faults = MODEL_ACCESS.snapshot()
    if access_faults:
        checks["model_access"] = access_faults
    rejected = [fault for fault in access_faults if fault["permanent"]]

    if settings.has_model_credentials and rejected:
        checks["model_credentials"] = (
            "configured, but rejected by the model service (见 model_access)；"
            "修复后重启，或由管理员调用 POST /admin/circuit/reset"
        )
    elif settings.has_model_credentials:
        checks["model_credentials"] = "ok"
    else:
        # 生产模式下这不是「降级」，是不就绪。Mock 会编出语法正确、内容错误的
        # SQL，而且从响应上看不出区别——把它算成 ready 等于把编造当成服务。
        checks["model_credentials"] = "missing (模型走 Mock)"
        if settings.app_env == "production":
            ready = False
    checks["model_provider"] = f"{settings.model_provider} ({settings.chat_model})"
    if settings.page_store.get("values"):
        # 模型连接来自页面保存的那一份，不是环境变量——排查「配置明明改了却没生效」时先看这里
        checks["model_settings"] = f"saved from the console at {settings.page_store.get('saved_at', '')}"
    elif settings.page_store.get("problem"):
        checks["model_settings"] = f"saved file is unusable: {settings.page_store['problem']}"
    checks["auth_mode"] = settings.auth_mode

    # 熔断状态只做展示：模型挂了会走降级链路，服务本身仍然可用，
    # 因此不把它算进 ready 判定，否则会被摘掉流量反而没法降级服务。
    # 上面的 model_access 同理：凭证被拒时服务仍然能重放历史 SQL、能浏览数据。
    circuits = {
        name: snapshot["state"]
        for name, snapshot in CIRCUIT_REGISTRY.snapshot().items()
    }
    if circuits:
        checks["model_circuits"] = circuits

    readonly_audits = getattr(app.state, "readonly_audits", {})
    if readonly_audits:
        checks["business_db_accounts"] = readonly_audits

    if not ready:
        raise HTTPException(status_code=503, detail={"ready": False, "checks": checks})

    return {"ready": True, "checks": checks}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus 抓取端点（免鉴权，按内网访问设计）。"""
    return render_metrics()


@app.post("/admin/schema/reload")
def reload_schema(principal=Depends(require_admin)) -> Dict[str, Any]:
    """
    手动重建 Schema 索引。

    业务库改了结构又不想等下一个轮询周期时用；
    定时轮询由 SchemaWatcher 负责（AI_SQL_SCHEMA_REFRESH_SECONDS）。
    """
    changed = app.state.pipeline.reload_schema(reason=f"manual:{principal.caller}", force=True)
    return {
        "reloaded": True,
        "changed_databases": changed,
        "fingerprints": {
            name: value[:12]
            for name, value in app.state.pipeline.schema_fingerprints.items()
        },
    }


@app.post("/admin/circuit/reset")
def reset_circuits(principal=Depends(require_admin)) -> Dict[str, Any]:
    """
    手动复位模型熔断器，并清掉已记下的模型访问故障（凭证被拒 / 额度耗尽 / 模型无权限）。

    模型侧确认恢复后不想等冷却、或者凭证 / 额度在服务端修好了又不想重启时用。
    （换一把 Key 不用来这里：在页面上保存模型连接时会自动清一遍，见 `/admin/model`。）
    这里**不验证**凭证是不是真的好了：清掉之后，下一个真实请求会再去试一次，
    还是被拒就重新记下。需要管理员身份；没有、也不打算有匿名的复位入口。
    """
    before = CIRCUIT_REGISTRY.snapshot()
    access_before = MODEL_ACCESS.snapshot()
    CIRCUIT_REGISTRY.reset_all()
    MODEL_ACCESS.reset_all()
    # 抽词器在模型调用失败一次之后会一直用规则抽词，直到进程重启；
    # 既然管理员说模型侧恢复了，把它也放回来，否则检索质量会停在降级状态。
    extractor = getattr(getattr(app.state, "pipeline", None), "keyword_extractor", None)
    reenable = getattr(extractor, "reset_model", None)
    if callable(reenable):
        reenable()
    log_event(
        _logger,
        logging.INFO,
        "circuit.manual_reset",
        caller=principal.caller,
        before={name: item["state"] for name, item in before.items()},
        model_access_cleared=len(access_before),
    )
    return {
        "reset": True,
        "before": before,
        "after": CIRCUIT_REGISTRY.snapshot(),
        "model_access_before": access_before,
        "model_access_after": MODEL_ACCESS.snapshot(),
    }


@app.get("/admin/schema/status")
def schema_status(principal=Depends(require_admin)) -> Dict[str, Any]:
    """查看各库当前指纹与是否有未生效的变更。"""
    watcher = app.state.schema_watcher
    return {
        "watcher_enabled": watcher.enabled,
        "interval_seconds": watcher.interval_seconds,
        "pending_changes": app.state.pipeline.detect_schema_changes(),
        "fingerprints": {
            name: value[:12]
            for name, value in app.state.pipeline.schema_fingerprints.items()
        },
    }


@app.post("/query", response_model=QueryResponse)
async def query(
    request: QueryRequest,
    response: Response,
    http_request: Request,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> QueryResponse:
    """
    自然语言查询入口。

    动态路由决定进入 Text2SQL 链路还是对话问答链路。
    每次调用都会落一条审计记录（含 trace_id、SQL、命中表、token 用量），
    和一条 run 记录（响应头 `X-Run-Id`，事件日志见 `GET /runs/{run_id}/events`）。

    主体仍是同步的，放在线程池里跑（和以前 `def` 端点一样）；这一层只多做一件事：
    开着 `AI_SQL_CANCEL_ON_DISCONNECT`（默认开，契约 v1.13 §5.4）时盯着客户端，断开就取消这次 run。
    """
    principal = await run_in_threadpool(_admit_query, api_key, request.user_id)

    runs = runs_of(app)
    run = None
    if runs is not None:
        run = await run_in_threadpool(
            runs.start,
            entry="http_query",
            caller=principal.caller,
            user_id=principal.user_id,
            session_id=request.session_id,
            query=request.query,
        )
        response.headers["X-Run-Id"] = run.run_id

    work = run_in_threadpool(_answer_query, request, principal, run)
    if run is None or not get_settings().cancel_on_disconnect:
        return await work
    return await watch_disconnect(
        http_request, work, lambda: runs.cancel(run.run_id, "client_disconnected")
    )


def _admit_query(api_key: Optional[ApiKeyRecord], user_id: str):
    """身份解析 + 限流配额（同步，读审计库算配额）。"""
    principal = app.state.auth.resolve_principal(api_key, user_id)
    app.state.limiter.check(principal)
    return principal


def _answer_query(request: QueryRequest, principal, run) -> QueryResponse:
    """`/query` 的同步主体：跑 service、写审计、组装响应。"""
    runs = runs_of(app)
    started = time.perf_counter()
    audit = AuditRecord(
        trace_id=get_trace_id(),
        caller=principal.caller,
        user_id=principal.user_id,
        session_id=request.session_id,
        query=request.query,
        run_id=run.run_id if run is not None else "",
    )

    with runs.running(run) if runs is not None and run is not None else nullcontext():
        try:
            result = app.state.service.run(
                user_id=principal.user_id,
                session_id=request.session_id,
                query=request.query,
                enable_long_term=request.enable_long_term,
                access=principal.policy,
            )
        except RunCancelled as exc:
            # 取消不是故障：审计照写（配额照计，契约 §5.5），状态码用 499（客户端关闭请求）
            audit.success = False
            audit.status_code = 499
            audit.error = str(exc)
            _write_audit(audit, started, principal)
            raise HTTPException(status_code=499, detail="查询已取消。") from exc
        except ValueError as exc:
            audit.success = False
            audit.status_code = 400
            audit.error = str(exc)
            _write_audit(audit, started, principal)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            audit.success = False
            audit.status_code = 500
            audit.error = f"{type(exc).__name__}: {exc}"
            _write_audit(audit, started, principal)
            raise

        summary = summarize_result(result)
        apply_summary_to_audit(summary, audit)
        step_logs = summary.step_logs
        selected_databases = summary.selected_databases
        failed_steps = summary.failed_steps
        repaired_steps = summary.repaired_steps
        has_risk = summary.has_risk
        degraded = summary.degraded
        degrade_reason = summary.degrade_reason
        _write_audit(audit, started, principal)
        if run is not None:
            run.set_outcome(**run_outcome(summary))

    log_event(
        _logger,
        logging.INFO,
        "api.query",
        caller=principal.caller,
        user_id=principal.user_id,
        session_id=request.session_id,
        route=result.decision.route.value,
        confidence=result.decision.confidence,
        queried_database=result.queried_database,
        databases=selected_databases,
        steps=len(step_logs),
        failed_steps=failed_steps,
        repaired_steps=repaired_steps,
        degraded=degraded,
        degrade_reason=degrade_reason,
        has_risk=has_risk,
        execution_status=summary.execution_status,
    )

    return QueryResponse(
        trace_id=get_trace_id(),
        degraded=degraded,
        degrade_reason=degrade_reason,
        route=result.decision.route.value,
        confidence=result.decision.confidence,
        reason=result.decision.reason,
        answer=result.answer,
        queried_database=result.queried_database,
        selected_databases=selected_databases,
        # 过渡期同一份数据序列化两次（契约 §4.4.1）
        steps=step_logs,
        step_logs=step_logs,
        has_risk=has_risk,
        execution_status=summary.execution_status,
        succeeded_steps=summary.succeeded_steps,
        failed_steps=failed_steps,
        repaired_steps=repaired_steps,
        result_truncated=summary.result_truncated,
        as_of=summary.as_of,
        as_of_source=summary.as_of_source,
        currency=summary.currency,
    )


_parse_denied_tables = parse_denied_tables  # 实现在 result_summary，这里保留名字


def _write_audit(audit: AuditRecord, started: float, principal) -> None:
    """写审计记录；审计失败绝不影响查询本身。"""
    usage = usage_summary()
    audit.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    audit.prompt_tokens = usage["prompt_tokens"]
    audit.completion_tokens = usage["completion_tokens"]

    try:
        app.state.audit.record(audit)
        app.state.limiter.quota_guard.invalidate(principal.user_id)
    except Exception as exc:
        log_event(
            _logger,
            logging.ERROR,
            "audit.write_failed",
            error=f"{type(exc).__name__}: {exc}",
        )


@app.get("/history", response_model=HistoryResponse)
def history(
    request: Request,
    limit: int = 20,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> HistoryResponse:
    """
    最近查询记录。

    默认只返回调用方自己的记录；管理员可以用 user_id 查他人，
    普通 Key 传别人的 user_id 会被拒绝——历史里有问题原文和 SQL，
    等同于数据访问面。
    """
    principal = app.state.auth.resolve_principal(api_key, "")

    target = user_id or principal.user_id
    if target != principal.user_id and not principal.is_admin:
        raise HTTPException(status_code=403, detail="只能查看自己的历史查询。")

    limit = max(1, min(int(limit), 100))
    rows = app.state.audit.recent(limit=limit, user_id=target)

    items = [
        HistoryItem(
            trace_id=row.get("trace_id", ""),
            created_at=str(row.get("created_at", "")),
            query=row.get("query", ""),
            session_id=str(row.get("session_id") or ""),
            route=row.get("route", ""),
            databases=list(row.get("databases") or []),
            tables=list(row.get("tables") or []),
            sqls=[
                item
                for item in (row.get("sqls") or [])
                if isinstance(item, dict)
            ],
            success=bool(row.get("success")),
            degraded=bool(row.get("degraded")),
            has_risk=bool(row.get("has_risk")),
            execution_status=str(row.get("execution_status") or ""),
            failed_steps=int(row.get("failed_steps") or 0),
            repaired_steps=int(row.get("repaired_steps") or 0),
            status_code=int(row.get("status_code") or 200),
            duration_ms=float(row.get("duration_ms") or 0),
            total_tokens=int(row.get("prompt_tokens") or 0)
            + int(row.get("completion_tokens") or 0),
            error=row.get("error"),
        )
        for row in rows
    ]

    return HistoryResponse(user_id=target, items=items)


@app.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(
    request: FeedbackRequest,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> FeedbackResponse:
    """
    记录用户对一次回答的判定（对 / 错 / 说不准）。

    这是让工具越用越准的那条回路：

    - 标记「错」的问题进反面教材清单，用 `python -m ai_sql_agent.feedback export-dataset`
      导进评测集——评测集原本全是我们自己出的模板题，真实用户问法与它的差距，
      正是线上准确率与评测跑分的差距；
    - 标记「对」的查询成为认证范例，注入后续同类问题的生成 Prompt。

    问题原文与 SQL 都按 trace_id 从审计记录回填，不接受客户端传。
    """
    settings = get_settings()
    if not settings.feedback_enabled:
        raise HTTPException(status_code=404, detail="反馈功能未启用。")

    principal = app.state.auth.resolve_principal(api_key, "")

    audited = app.state.audit.get_by_trace(request.trace_id)
    if audited is None:
        raise HTTPException(
            status_code=404,
            detail=f"没有找到 trace_id={request.trace_id} 的查询记录。",
        )

    owner = str(audited.get("user_id") or "")
    if owner != principal.user_id and not principal.is_admin:
        # 反馈会影响别人的认证范例，越权表态等于替别人认证 SQL
        log_event(
            _logger,
            logging.WARNING,
            "feedback.denied",
            caller=principal.caller,
            trace_id=request.trace_id,
            owner=owner,
        )
        raise HTTPException(status_code=403, detail="只能对自己的查询提交反馈。")

    verdict = Verdict(request.verdict)
    feedback = FeedbackRecord(
        trace_id=request.trace_id,
        user_id=owner or principal.user_id,
        verdict=verdict,
        query=str(audited.get("query") or ""),
        databases=list(audited.get("databases") or []),
        tables=list(audited.get("tables") or []),
        sqls=[
            item for item in (audited.get("sqls") or []) if isinstance(item, dict)
        ],
        comment=request.comment.strip(),
        caller=principal.caller,
        route=str(audited.get("route") or ""),
        had_risk=bool(audited.get("has_risk")),
    )

    try:
        app.state.feedback.record(feedback)
    except Exception as exc:
        log_event(
            _logger,
            logging.ERROR,
            "feedback.write_failed",
            trace_id=request.trace_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(status_code=500, detail="反馈写入失败。") from exc

    certified = feedback.is_certifiable and get_settings().certified_sql_enabled

    log_event(
        _logger,
        logging.INFO,
        "feedback.recorded",
        trace_id=request.trace_id,
        user_id=feedback.user_id,
        verdict=verdict.value,
        had_risk=feedback.had_risk,
        certified=certified,
        has_comment=bool(feedback.comment),
    )
    FEEDBACK_SUBMITTED.inc(verdict=verdict.value, had_risk=str(feedback.had_risk).lower())

    if verdict == Verdict.CORRECT:
        message = (
            "已记录。这条查询会作为参考写法用于后续同类问题。"
            if certified
            else "已记录。"
        )
    elif verdict == Verdict.WRONG:
        message = "已记录。这条会进待改进清单，用于补评测集和排查元数据缺口。"
    else:
        message = "已记录，会有人来看一眼。"

    return FeedbackResponse(
        recorded=True,
        trace_id=request.trace_id,
        verdict=verdict.value,
        certified=certified,
        message=message,
    )


@app.get("/feedback/stats")
def feedback_stats(principal=Depends(require_admin)) -> Dict[str, Any]:
    """
    线上反馈汇总：真实用户口径的准确率与静默错误率。

    与 benchmark 的区别是题目来自真实用户，不是我们自己出的。
    两个数差得远时，以这个为准——评测集只代表我们想到的问法。
    """
    stats = app.state.feedback.stats()
    unresolved = app.state.feedback.recent(limit=10, verdict=Verdict.WRONG.value)
    return {
        "stats": stats.to_dict(),
        "recent_wrong": [
            {
                "trace_id": item.get("trace_id"),
                "query": item.get("query"),
                "comment": item.get("comment"),
                "had_risk": item.get("had_risk"),
                "resolved": item.get("resolved"),
            }
            for item in unresolved
        ],
    }


@app.post("/memory/save")
def save_memory(
    request: SaveMemoryRequest,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    响应用户“保存到个人知识库”动作。
    """
    principal = app.state.auth.resolve_principal(api_key, request.user_id)
    app.state.limiter.check(principal)

    memory = app.state.memory.save_structured_result(
        user_id=principal.user_id,
        query=request.query,
        result=request.result,
        database=request.database,
        tables=request.tables,
        columns=request.columns,
        sql=request.sql,
    )
    return {"memory_id": memory.id, "summary": memory.summary}
