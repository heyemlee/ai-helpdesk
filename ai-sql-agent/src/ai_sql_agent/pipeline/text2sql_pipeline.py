from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ai_sql_agent.auth import (
    ViewCatalog,
    authorization_scope,
    check_sql_access,
    extract_referenced_tables,
)
from ai_sql_agent.auth.identifiers import bind_dialect
from ai_sql_agent.core.errors import ModelCallError
from ai_sql_agent.core.run_context import check_cancelled
from ai_sql_agent.core import (
    get_logger,
    get_settings,
    get_trace_id,
    log_event,
    stage,
    usage_scope,
    usage_summary,
)
from ai_sql_agent.core.metrics import (
    ACCESS_DENIED,
    DEGRADED_RESPONSES,
    SCHEMA_RELOADS,
    SQL_EXECUTIONS,
    CROSS_DB_VIOLATIONS,
    PLAN_DB_CONFLICTS,
    SCHEMA_COMPACTED,
    SCHEMA_COLUMNS_DROPPED,
    RESULT_ANOMALIES,
    SQL_REPAIRS,
)
from ai_sql_agent.planning import CotPlanner, few_shots_for, ThinkingModelClient, ThinkingModelConfig
from ai_sql_agent.execution import MCPRouter, PostgresMCPExecutor, SQLiteMCPExecutor
from ai_sql_agent.execution.objects import MCPExecutionResult
from ai_sql_agent.execution.dialects import get_dialect

from . import provenance
from ai_sql_agent.retrieval.access import ALLOW_ALL, SchemaAccessFilter
from ai_sql_agent.retrieval.hierarchical_service import (
    DatabaseSource,
    ThreeTierRetrievalConfig,
    ThreeTierSchemaRetrievalService,
)
from ai_sql_agent.retrieval.hybrid_schema_retrieval_service import (
    HybridSchemaRetrievalConfig,
)
from ai_sql_agent.retrieval.rerank_client import AliyunRerankClient, AliyunRerankConfig
from ai_sql_agent.retrieval.schema_fingerprint import database_fingerprint
from ai_sql_agent.retrieval.rrf_fusion_client import RRFFusionConfig
from ai_sql_agent.generation import (
    CoderModelClient,
    CoderModelConfig,
    CotStep,
    LocalSchemaStore,
    SqlGenerator,
)
from ai_sql_agent.normalization import FiscalConfig, QueryNormalizer
from ai_sql_agent.generation.projection import RawProjectionError
from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker
from ai_sql_agent.repair import SqlRepairService

from ai_sql_agent.semantic import SemanticLayer, resolve_semantic_layer

from .demo_data import (
    create_erp_demo_database,
    create_mes_demo_database,
    create_pm_demo_database,
    create_wms_demo_database,
)
from .embedding import describe_embedding, local_embedding_client, schema_embedding_client
from .local_clients import (
    HybridKeywordExtractor,
    build_schema_glossary,
)
from .objects import DatabaseConfig, PipelineConfig, PipelineResult, StepExecutionLog
from .short_path import decide_short_path
from .schema_budget import fit_graphs_to_budget
from .token_budget import PromptBudgetManager, columns_in_prompt

# 回填给下一步 SQL 生成 Prompt 的上一步结果行数上限
_MAX_BACKFILL_ROWS = 50

# 规划里「排除前面那些记录」的说法（见 `_dependent_step_outcome`）。前面 0 行时，
# 只看那些记录的答案是没有，排除那些记录的答案是其余全部，两者必须分开。
_EXCLUSION_WORDS = re.compile(
    r"排除|剔除|除去|去掉|去除|过滤掉|排掉|不在|不属于|不包含|不含|之外|以外|非上|"
    r"除了|不是|未出现|没有出现|没出现|标出|标注|是否属于|是否在|!=|<>|"
    r"NOT\s+IN|NOT\s+EXISTS|NOT\s+LIKE|EXCEPT|LEFT\s+JOIN",
    re.I,
)

# 回指前面结果的说法（见 `_step_dependencies`）
_BACK_REFERENCE = re.compile(
    r"这些|那些|上述|前述|此前|该(?:列表|批|些|组|取值|编号|单号|工程号|订单)|对应的"
)

# 规划明说「只取前面那些值」的说法：前面 0 行时只有这种步骤能直接答「没有」。
# 认不出来的不算——默认是没执行，不是「没有记录」。
_INCLUSION_WORDS = re.compile(
    r"落在[^；;。]{0,20}(?:列表|范围|取值)内|在(?:该|这些|上述)[^；;。]{0,12}(?:列表|取值|范围)(?:之)?内|"
    r"筛选(?:出)?(?:这些|该|上述|对应)|只(?:看|保留|取|查)(?:这些|该|上述)|属于(?:这些|该|上述)|"
    r"\bIN\s*\(",
    re.I,
)

_MAX_BACKFILL_LITERALS = 50
"""单列最多拼进 IN 条件的取值数，防止 Prompt 被长结果集撑爆。"""


def _as_sql_literals(values) -> List[str]:
    """
    把一列取值转成 SQL 字面量，去重并保持首次出现的顺序。

    数字不加引号，其余按字符串处理并转义单引号；None 跳过（IN 里放 NULL 无意义）。
    """
    literals: List[str] = []
    seen = set()
    for value in values:
        if value is None or value in seen:
            continue
        seen.add(value)
        if isinstance(value, bool):
            literals.append("1" if value else "0")
        elif isinstance(value, (int, float)):
            literals.append(str(value))
        else:
            literals.append("'" + str(value).replace("'", "''") + "'")
        if len(literals) >= _MAX_BACKFILL_LITERALS:
            break
    return literals


def build_rerank_client() -> AliyunRerankClient:
    """
    字段层重排用的托管 Rerank 客户端。

    模型名、API Key、workspace 全部取 Settings：模型名来自 AI_SQL_RERANK_MODEL，
    默认 qwen3-rerank。这里原来写死 model="qwen-rerank"，和 Settings 的默认值、
    客户端自己的注释都对不上，改环境变量也不生效。没配 key 时客户端退回本地打分。
    """
    return AliyunRerankClient(AliyunRerankConfig(api_key="", workspace_id=""))


REMOTE_COLUMN_BUDGET = (10, 3)
"""远端 Embedding 时字段级的（每库保留字段数, 每表保底字段数）。"""

LOCAL_COLUMN_BUDGET = (20, 4)
"""本地哈希向量时的同一组参数。"""


def column_budget_for(embedding_client) -> tuple:
    """
    进图字段的预算跟着 Schema 检索实际用的向量走。

    每表保底字段是为了多表关联时不让一张表占满配额、另一张表只剩 join key；当初用本地哈希向量时
    设 4 把 multi_join 召回从 14/25 拉到 17/25。换成远端 Embedding 后召回已经是 100%，
    这两个数反倒成了进图字段占比超线（44%，门禁线 40%）的主因。2026-10-14 只跑检索的扫参：

    - 远端：每库 10 个、保底 3 个，评测集和留出集召回仍是 100%，占比降到 37%；
      再少（每库 8 个）留出集开始漏字段；少选库或表会漏跨库题的字段。
    - 本地：保底从 4 降到 3，留出集召回从 55% 掉到 47.5%（每库 10–15 个时；规则抽词，可复现）。

    所以只在真用上远端时收紧。配了远端、启动时调不通会退回本地向量，这里拿到的就是本地客户端，
    参数跟着回到宽的一档。
    """
    if getattr(embedding_client, "backend", "local") == "remote":
        return REMOTE_COLUMN_BUDGET
    return LOCAL_COLUMN_BUDGET


_logger = get_logger("pipeline")


class _ModelUnavailable(Exception):
    """规划 / 生成 / 修复时模型不可用：交给 `_run` 走降级出口（`_degrade`）。"""

    def __init__(self, failed_stage: str, error: ModelCallError):
        super().__init__(str(error))
        self.failed_stage = failed_stage
        self.error = error


@dataclass
class _PreparedQuery:
    """准备阶段的产物：规划、逐步执行、收尾都从这里取。"""

    normalization: Any
    effective_query: str
    keywords: List[str]
    contextual_query: str
    retrieval_result: Any
    schema_context: str


class Text2SQLPipeline:
    """
    AI SQL Agent Text2SQL 端到端流程。

    当前实现串联：
    1. 创建多库测试数据（展厅销售域 + 工厂生产域）
    2. 三层 Schema 检索（库级 → 表级 → 字段级）与 SchemaGraph 构建
    3. CoT 四元组规划（每一步标注目标数据库）
    4. 按步骤生成 SQL，多步执行时回填上一步结果
    5. MCP 路由执行（按步骤 database 路由到对应执行器）

    结果校验与回调修复见 ai_sql_agent.repair 模块。
    """

    def __init__(
        self,
        config: Optional[PipelineConfig] = None,
        sql_history_lookup: Optional[
            Callable[[str, str], Sequence[Tuple[str, str]]]
        ] = None,
        certified_lookup: Optional[Callable[..., Sequence[object]]] = None,
    ):
        self.config = config or PipelineConfig()
        self.sql_history_lookup = sql_history_lookup
        """
        (user_id, query) -> [(database, sql)]，取该用户同一问题上次成功执行的 SQL。

        模型不可用时用它重放：SQL 是缓存的，数据仍然是实时查出来的。
        由 API 层注入（数据源是审计表），Pipeline 不直接依赖审计模块。
        """

        self.certified_lookup = certified_lookup
        """
        取「用户确认过答对了」的相似查询，作为写法参考注入 SQL 生成 Prompt。

        与 sql_history_lookup 的分工：那个是模型挂了之后**重放同一个问题**的
        原样 SQL；这个是模型正常时给**相似问题**当范例，永远不直接执行。
        同样由 API 层注入（数据源是反馈表），Pipeline 不依赖反馈模块。
        """
        self.prompt_budget = PromptBudgetManager(self.config.prompt_budget)

        # 业务语义层：库/表/字段的业务含义、跨库关联键、指标口径。
        # 模型对业务的全部认知都来自这里——没配置时用内置演示元数据，
        # 接真实库必须配 AI_SQL_SEMANTIC_DIR，否则检索只能按字段名字面匹配。
        self.semantic: SemanticLayer = (
            self.config.semantic_layer
            if self.config.semantic_layer is not None
            else resolve_semantic_layer()
        )

        self.databases = self._resolve_databases()
        self.business_metas = self._resolve_business_metas()

        # 术语表来自业务元数据本身，让模型把口语说法映射到 Schema 里已有的
        # 描述与别名上。这是留出集召回率的主要抓手：不往短语表里补口语词，
        # 补了只对补进去的那几条有效，还会把留出集变成第二个训练集。
        self.keyword_extractor = HybridKeywordExtractor(
            glossary=(
                build_schema_glossary(self.business_metas)
                if self.config.enable_keyword_glossary
                else ""
            )
        )
        self.dialects = {
            database.name: database.dialect for database in self.databases
        }

        # 视图会遮住它读的基表，授权关卡自己推不出哪些名字是视图（见 auth/views.py）。
        # 目录随授权作用域交给关卡；启动时扫一次，之后每次 schema 轮询都重扫。
        self.view_catalog = ViewCatalog()
        self._refresh_view_catalog()

        self.schema_retrieval_service = self._build_schema_retrieval_service()
        self.schema_fingerprints = self._current_fingerprints()
        self._reload_lock = threading.Lock()
        self.cot_planner = self._build_cot_planner()
        self.mcp_router = self._build_mcp_router()
        # 语义自检复用规划模型的客户端。离线 Mock 环境下它不真做语义判断，
        # 自检按「不适用」处理（见 SemanticSelfChecker.run），不留发现、不算风险提示。
        self.result_validator = ResultValidator(
            self_checker=SemanticSelfChecker(
                getattr(self.cot_planner, "thinking_client", None)
                if getattr(self.config, "enable_self_check", True)
                else None
            ),
            enable_cardinality=getattr(self.config, "enable_validation", True),
            enable_self_check=getattr(self.config, "enable_self_check", True),
        )
        self.query_normalizer = QueryNormalizer(
            FiscalConfig(
                fiscal_year_start_month=self.config.fiscal_year_start_month
            ),
            enable_coreference=self.config.enable_normalization,
            enable_time_parsing=self.config.enable_normalization,
        )
        self.sql_repair = SqlRepairService(
            coder_client=CoderModelClient(
                CoderModelConfig(use_mock_when_no_api_key=True)
            ),
            max_attempts=self.config.max_repair_attempts,
        )

    def run(
        self,
        query: str,
        keywords: Optional[List[str]] = None,
        conversation_context: str = "",
        access: Optional[SchemaAccessFilter] = None,
        user_id: str = "",
        history_queries: Optional[List[str]] = None,
    ) -> PipelineResult:
        """
        执行完整 Text2SQL 流程。

        access 为当前用户的数据权限：检索阶段裁掉无权表字段，
        执行阶段再按引用表校验一次（详见 _check_table_access）。

        history_queries 为同会话的历史问题，供代词消解使用。
        """
        # HTTP 入口已经用 use_trace_id() 建好了用量上下文，这里不会重复建；
        # 当库直接调用（benchmark、脚本）时补上，否则 usage 汇总恒为 0。
        #
        # authorization_scope 是执行器那道强制关卡的入口：作用域内的每一次
        # ai_sql_agent.execution.execute（首次生成 / 失败修复 / 历史重放）都按这份权限判，
        # 作用域外的执行一律被拒。以前每个调用点各写一次检查，修复那条漏了。
        with usage_scope(), authorization_scope(
            access or ALLOW_ALL,
            user_id=user_id,
            trace_id=get_trace_id(),
            views=self.view_catalog,
        ):
            return self._run(
                query,
                keywords=keywords,
                conversation_context=conversation_context,
                access=access,
                user_id=user_id,
                history_queries=history_queries,
            )

    def _run(
        self,
        query: str,
        keywords: Optional[List[str]] = None,
        conversation_context: str = "",
        access: Optional[SchemaAccessFilter] = None,
        user_id: str = "",
        history_queries: Optional[List[str]] = None,
    ) -> PipelineResult:
        """
        `run()` 的实现体，用量上下文由 `run()` 负责。

        顺序由代码固定：准备（归一化 → 抽词 → 检索）→ 规划 → 逐步（生成 → 执行 → 修复 → 校验）
        → 收尾。规划、生成、修复里模型不可用时，统一从这里走降级出口（`_degrade`）。
        """
        access = access or ALLOW_ALL
        prepared = self._prepare(
            query,
            keywords=keywords,
            conversation_context=conversation_context,
            history_queries=history_queries,
            access=access,
        )
        try:
            cot_result = self._plan(prepared)
            step_logs = self._run_steps(
                query, prepared, cot_result, access=access, user_id=user_id
            )
        except _ModelUnavailable as unavailable:
            return self._degrade(
                query=query,
                keywords=prepared.keywords,
                schema_context=prepared.schema_context,
                retrieval_result=prepared.retrieval_result,
                access=access,
                user_id=user_id,
                failed_stage=unavailable.failed_stage,
                error=unavailable.error,
            )
        return self._finish(query, prepared, cot_result, step_logs)

    def _prepare(
        self,
        query: str,
        *,
        keywords: Optional[List[str]],
        conversation_context: str,
        history_queries: Optional[List[str]],
        access: SchemaAccessFilter,
    ) -> "_PreparedQuery":
        """归一化 → 抽词 → 拼会话记忆 → 三层检索与预算裁剪。"""
        # ① 问题归一化：代词消解 + 相对时间表达式 → 绝对区间
        #
        # 这里以前是一条裸 log_event，而且**只在 changed 为真时才记**。两个后果：
        # 事件汇（trace.stage()）收不到它，所以契约 §4.5 声明的九个 stage 实发八个；
        # 问题没被改写时连日志都没有，排查时看不出这一步跑没跑过。
        # 改成真 stage 顺带修好后者——观测盲区和契约缺口是同一个根因。
        with stage("normalization", _logger) as normalization_stage:
            normalization = self.query_normalizer.normalize(
                query, history_queries=history_queries or ()
            )
            # changed 要到阶段结束才知道，所以它进的是**结束日志**，不是进度事件
            # （契约 §4.5「附带字段只可能是进入该 stage 时已经知道的那些」）。
            normalization_stage.set(
                changed=normalization.changed,
                original=normalization.original_query,
                normalized=normalization.normalized_query,
                references=normalization.resolved_references,
                date_ranges=[r.to_text() for r in normalization.date_ranges],
            )
        effective_query = normalization.normalized_query

        resolved_keywords = keywords or self.keyword_extractor.extract(effective_query)

        contextual_query = effective_query
        if conversation_context.strip():
            memory_context = self.prompt_budget.fit_memory(conversation_context)
            contextual_query = (
                f"以下是与当前问题相关的会话记忆：\n{memory_context}\n\n"
                f"当前用户问题：{effective_query}"
            )

        with stage(
            "retrieval",
            _logger,
            keywords=resolved_keywords,
            with_memory=bool(conversation_context.strip()),
        ) as retrieval_stage:
            retrieval_result = self.schema_retrieval_service.retrieve(
                query=contextual_query,
                keywords=resolved_keywords,
                access=access,
            )
            # 预算感知：先降描述精度，再按相关性丢字段，最后才交给字符级裁剪兜底。
            # 「字段进不进得来」比「字段描述得多详细」重要得多——实测被预算切掉的
            # 字段里 4/5 是检索已经找到的 gold 字段，后果是规划模型写出
            # 「Schema 未提供该字段」再生成 SELECT ... WHERE 0。
            sections, budget_info = fit_graphs_to_budget(
                list(retrieval_result.schema_graphs.values()),
                self.prompt_budget.config.schema_budget,
            )
            if budget_info["compact"]:
                SCHEMA_COMPACTED.inc()
            if budget_info["dropped"]:
                SCHEMA_COLUMNS_DROPPED.inc(int(budget_info["dropped"]))
            if budget_info["compact"] or budget_info["dropped"]:
                # 丢了什么必须留痕：字段被预算丢掉是「模型答不出来」最隐蔽的原因，
                # 不打日志的话只会看到一句莫名其妙的「Schema 未提供该字段」。
                log_event(
                    _logger,
                    logging.INFO,
                    "retrieval.schema_budget_applied",
                    compact=budget_info["compact"],
                    dropped=budget_info["dropped"],
                    dropped_columns=budget_info["dropped_columns"][:10],
                    budget=self.prompt_budget.config.schema_budget,
                )
            # 极端情况兜底：仍超预算时按库分配裁剪。整段头部截断会优先砍掉
            # 最后一个库，而跨库查询正依赖第二个库（见 fit_schema_sections）。
            schema_context = self.prompt_budget.fit_schema_sections(sections)
            # 跨库约束在预算裁剪**之后**追加：它比任何单个字段说明都重要，
            # 不能因为 Schema 太长就被裁掉。
            if retrieval_result.cross_database_notes:
                schema_context = (
                    f"{schema_context}\n\n{retrieval_result.cross_database_notes}"
                )
            retrieval_stage.set(
                databases=list(retrieval_result.selected_databases),
                tables={
                    database: list(tables)
                    for database, tables in retrieval_result.selected_tables.items()
                },
                schema_context_chars=len(schema_context),
            )

        return _PreparedQuery(
            normalization=normalization,
            effective_query=effective_query,
            keywords=resolved_keywords,
            contextual_query=contextual_query,
            retrieval_result=retrieval_result,
            schema_context=schema_context,
        )

    def _plan(self, prepared: "_PreparedQuery"):
        """短路径判定 + 规划（计划与库冲突时带着冲突重新规划一次）。模型不可用时抛 `_ModelUnavailable`。"""
        effective_query = prepared.effective_query
        contextual_query = prepared.contextual_query
        retrieval_result = prepared.retrieval_result
        schema_context = prepared.schema_context

        routing = decide_short_path(
            effective_query,
            selected_tables={
                database: list(tables)
                for database, tables in retrieval_result.selected_tables.items()
            },
            enabled=self.config.enable_short_path,
        )

        try:
            with stage("planning", _logger, short_path=routing.use_short_path) as planning_stage:
                if routing.use_short_path:
                    # 简单查询跳过独立规划调用，直接按检索结果构造单步计划
                    cot_result = self._short_path_plan(
                        effective_query, retrieval_result
                    )
                else:
                    # 命中多个库时给一个完整走通的跨库范例：跨库拆步的规则
                    # 此前只以文字指令存在，模型读懂了规则仍写不对格式
                    #（两个库压进一步 / 第一步不输出关联键）。
                    cot_result = self.cot_planner.plan(
                        user_query=contextual_query,
                        schema_graph=schema_context,
                        few_shots=few_shots_for(
                            len(retrieval_result.selected_databases)
                        ) if self.config.enable_cross_db_fewshot else [],
                    )
                # 步骤-库一致性校验：某一步声明的库里如果根本没有它处理对象
                # 里的表，SQL 一定生成不对。这时问题出在计划本身，重新生成
                # SQL 是治标的——把具体冲突写回 Prompt 重新规划一次才治本。
                conflicts = (
                    []
                    if routing.use_short_path or not self.config.enable_plan_db_check
                    else self._plan_database_conflicts(cot_result)
                )
                replan_adopted = False
                if conflicts:
                    log_event(
                        _logger,
                        logging.WARNING,
                        "planning.database_conflict",
                        conflicts=conflicts,
                    )
                    PLAN_DB_CONFLICTS.inc()
                    replanned = self.cot_planner.plan(
                        user_query=contextual_query,
                        schema_graph=schema_context,
                        few_shots=few_shots_for(
                            len(retrieval_result.selected_databases)
                        ),
                        extra_instructions="；".join(conflicts),
                    )
                    # 只有重新规划确实解决了冲突才采纳，否则保留原计划：
                    # 换一版同样错的计划只是白烧一次调用。
                    if replanned.steps and not self._plan_database_conflicts(
                        replanned
                    ):
                        cot_result = replanned
                        replan_adopted = True

                planning_stage.set(
                    steps=len(cot_result.steps),
                    step_databases=[step.database for step in cot_result.steps],
                    short_path_reason=routing.reason,
                    replanned=replan_adopted,
                )
        except ModelCallError as exc:
            raise _ModelUnavailable("planning", exc) from exc

        return cot_result

    def _run_steps(
        self,
        query: str,
        prepared: "_PreparedQuery",
        cot_result,
        *,
        access: SchemaAccessFilter,
        user_id: str,
    ) -> List[StepExecutionLog]:
        """
        按计划逐步执行；每一步拿上一步的结果回填（见 `_serialize_step_result`）。

        **要用前面某一步取值的步骤，那一步没交出取值时不生成、不执行。** 跨库计划里，
        后一步可能拿前一步查出来的关联键当字面量筛选（规划提示词规则 4）。被依赖的那一步
        0 行或者没查成，这些取值就不存在——以前照样生成，模型手里没有单号，写出
        `WHERE order_no IN (order_no)`，把整张表查了出来（2026-11-06 客服问法评测 c18：
        问一张单，返回了 936 条别家的配送记录）。

        只拦**真用到**前面结果的步骤（`_step_dependencies`）；不依赖前面的步骤照常跑
        （规则 3 也允许按库拆出彼此独立的步骤）。拦下时分三种（`_dependent_step_outcome`）：

        - 依赖的步骤查到 0 行、规划**明说**只取这些值（`_INCLUSION_WORDS`）：答案就是没有，
          记成执行成功、0 行（契约 §4.6.1 的 no_data，客服会据实说没有记录）；
        - 依赖的步骤查到 0 行、其它说法（排除、标注，或者认不出来）：答案可能是其余全部，
          没有取值写不出来，记成没执行——默认按这一种，不把「其余全部」说成「没有」；
        - 依赖的步骤没查成：取值未知，记成没执行。

        后两种让整次查询成为 partial_success：只有成功的那几步能当依据。
        """
        retrieval_result = prepared.retrieval_result
        schema_stores = {
            database: LocalSchemaStore.from_schema_graph(graph)
            for database, graph in retrieval_result.schema_graphs.items()
        }

        step_logs: List[StepExecutionLog] = []
        previous_results = ""
        # 每一步交出了什么：rows / empty / failed，供后面依赖它的步骤判断
        outcomes: Dict[int, str] = {}

        for cot_step in cot_result.steps:
            step_index = len(step_logs) + 1
            held = self._dependent_step_outcome(cot_step, step_index=step_index, outcomes=outcomes)
            if held is not None:
                step_logs.append(held)
                # 回填清空：这一步本该交出的取值没有，后面的步骤不能拿到更早那一步的
                # 取值、当成「上一步结果」抄进 IN 列表（依赖说法没认出来时就会这样）
                previous_results = ""
            else:
                step_log, execution_result = self._run_step(
                    query,
                    prepared.effective_query,
                    cot_step,
                    cot_result,
                    step_index=step_index,
                    schema_stores=schema_stores,
                    previous_results=previous_results,
                    access=access,
                    user_id=user_id,
                )
                step_logs.append(step_log)
                if execution_result is not None:
                    previous_results = self._serialize_step_result(execution_result)
            execution = step_logs[-1].execution_result or {}
            outcomes[step_index] = (
                "failed" if not execution.get("success")
                else "rows" if execution.get("rows") else "empty"
            )

        return step_logs

    @staticmethod
    def _step_dependencies(cot_step, *, step_index: int, loose: bool = True) -> set:
        """
        这一步的规划文字里引用了前面哪几步的结果。

        规划写依赖的说法不止一种：「先读取上一步结果中的…」「把步骤1输出的…取值当作字面量
        列表」「前一步」。认「上一步 / 前一步」（指紧挨着的那一步）和「步骤 N / 第 N 步」；
        都没提到就是不依赖前面的步骤。
        """
        text = " ".join(
            str(getattr(cot_step, name, "") or "")
            for name in ("processing_objects", "operation_instruction", "output_target")
        )
        steps = {
            int(a or b)
            for a, b in re.findall(r"步骤\s*(\d+)|第\s*(\d+)\s*步", text)
            if 0 < int(a or b) < step_index
        }
        if step_index > 1 and (
            re.search(r"上一步|前一步|上一个步骤|上步|前面(?:查到|得到|输出|的结果)", text)
            # 「用这些工程号找订单」这类回指也算依赖紧挨着的那一步（loose）。认宽一点是故意的：
            # 只有那一步没交出取值时才有影响，误认的代价是一个独立步骤被记成没执行
            # （部分成功），漏认的代价是没有筛选条件、把整张表当答案（c18）。
            # 只靠回指认出来的依赖不够确定，不能据此直接答「没有记录」（见 _dependent_step_outcome）
            or (loose and _BACK_REFERENCE.search(text))
        ):
            steps.add(step_index - 1)
        return steps

    def _dependent_step_outcome(
        self, cot_step, *, step_index: int, outcomes: Dict[int, str]
    ) -> Optional[StepExecutionLog]:
        """依赖的步骤没交出取值时，这一步的日志（不生成、不执行）；照常执行时返回 None。"""
        dependencies = self._step_dependencies(cot_step, step_index=step_index)
        failed = sorted(n for n in dependencies if outcomes.get(n) == "failed")
        empty = sorted(n for n in dependencies if outcomes.get(n) == "empty")
        if not failed and not empty:
            return None

        # 「答案就是没有」只在规划明说「只取这些值」时才成立；说法认不出来一律按没执行算
        # （宁可报部分成功，也不把「其余全部」说成「没有记录」）。排除的说法优先：
        # 「筛选不在这些 job_no 里的工单」两种词都有，按排除算
        plan_text = " ".join(
            str(getattr(cot_step, name, "") or "")
            for name in ("processing_objects", "operation_instruction", "output_target")
        )
        # 依赖是只靠「这些 / 对应的」这类回指认出来的，就不确定这一步真用了前一步的取值：
        # 可能是个独立步骤碰巧用了这些词。那样答「没有」是错的，只能记成没执行
        explicit = self._step_dependencies(cot_step, step_index=step_index, loose=False)
        keeps_only_those = (
            set(empty) <= explicit
            and not _EXCLUSION_WORDS.search(plan_text)
            and bool(_INCLUSION_WORDS.search(cot_step.operation_instruction or ""))
        )
        if failed:
            reason = f"第 {'、'.join(map(str, failed))} 步没有查成"
            message = f"{reason}，这一步要用它的取值，取值不知道，所以没有执行。"
            success = False
        elif keeps_only_those:
            reason = f"第 {'、'.join(map(str, empty))} 步查到 0 行"
            message = ""
            success = True
        else:
            reason = f"第 {'、'.join(map(str, empty))} 步查到 0 行"
            message = (
                f"{reason}。这一步要用那些取值，但看不出是只取这些值，还是排除它们或只拿来标注："
                "只取这些值的话答案是没有，排除或标注的话答案是符合其它条件的全部记录。"
                "没有取值写不出这一步（条件只会被丢掉或者写成和字段自己比较），所以没有执行。"
            )
            success = False

        log_event(
            _logger,
            logging.INFO,
            "pipeline.dependent_step_held",
            step=step_index,
            database=cot_step.database,
            reason=reason,
            answered_empty=success,
        )
        return StepExecutionLog(
            database=cot_step.database,
            cot_step=cot_step,
            local_schema="",
            sql="",
            execution_request={},
            execution_result={
                "database": cot_step.database,
                "sql": "",
                "success": success,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                # 只看那些记录、那些记录一条都没有：答案就是没有，不算出错
                "error": message or None,
                "held_reason": f"{reason}，这一步要用它的取值，没有生成 SQL",
            },
        )

    @staticmethod
    def _raw_projection_failure(cot_step, local_schema, error):
        return StepExecutionLog(
            database=cot_step.database, cot_step=cot_step,
            local_schema=local_schema.to_prompt_context(), sql=error.sql,
            execution_request={}, execution_result={
                "database": cot_step.database, "sql": error.sql,
                "success": False, "columns": [], "rows": [], "row_count": 0,
                "truncated": False, "error": str(error), "error_category": "raw_projection_incomplete",
            },
        ), None

    def _run_step(
        self,
        query: str,
        effective_query: str,
        cot_step,
        cot_result,
        *,
        step_index: int,
        schema_stores,
        previous_results: str,
        access: SchemaAccessFilter,
        user_id: str,
    ):
        """
        一步：生成 → 跨库检查 → 执行 → 修复 → 校验。

        返回 `(步骤日志, 执行结果)`；Schema 不足、没有走到执行的步骤，执行结果为 None，
        下一步的回填保持不变。模型在生成或修复时不可用就抛 `_ModelUnavailable`。
        """
        sql_cot_step = CotStep(
            database=cot_step.database,
            processing_objects=cot_step.processing_objects,
            operation_instruction=cot_step.operation_instruction,
            output_target=cot_step.output_target,
            expected_cardinality=getattr(cot_step, "expected_cardinality", ""),
        )

        schema_store = self._resolve_schema_store(
            schema_stores, sql_cot_step
        )

        # 权限收紧到一张表都不剩时，检索结果是空的。以前这里会一路走到
        # `next(iter({}))` 抛 StopIteration，HTTP 层翻成 500——权限不足
        # 是个正常业务结论，不是服务故障。deny by default 之后这条路更容易
        # 走到（角色没配策略就是这个形态），所以必须先判掉。
        if schema_store is None:
            return self._schema_insufficient_step(sql_cot_step, step_index), None

        sql_generator = SqlGenerator(
            schema_store=schema_store,
            coder_client=CoderModelClient(
                CoderModelConfig(
                    use_mock_when_no_api_key=True,
                )
            ),
        )

        # 规划阶段判定 Schema 不足（无相关表，或相关表不在当前用户权限内）：
        # 直接给出人能看懂的结论，不要退化成“找不到执行器”这种内部错误
        if sql_cot_step.database not in self.dialects:
            return self._schema_insufficient_step(sql_cot_step, step_index), None

        step_dialect = self.dialects.get(sql_cot_step.database, "sqlite")

        local_schema = schema_store.extract_local_schema(sql_cot_step)

        certified_examples = self._certified_examples(
            query=effective_query,
            database=sql_cot_step.database,
            user_id=user_id,
            access=access,
        )

        with stage(
            "sql_generation",
            _logger,
            step=step_index,
            database=sql_cot_step.database,
            dialect=step_dialect,
            with_backfill=bool(previous_results),
            certified_examples=bool(certified_examples),
        ) as generation_stage:
            try:
                sql_result = sql_generator.generate(
                    sql_cot_step,
                    sql_dialect=step_dialect,
                    previous_results=previous_results,
                    certified_examples=certified_examples,
                )
            except RawProjectionError as exc:
                return self._raw_projection_failure(sql_cot_step, local_schema, exc)
            except ModelCallError as exc:
                generation_error = exc
                sql_result = None
            else:
                generation_error = None
                generation_stage.set(sql=sql_result.sql)

        # 跨库静态校验：生成的 SQL 只能引用**当前步骤目标库**里的表。
        #
        # 跨库问题里模型很容易把两个库压进一条 SQL（在 erp 的步骤里写
        # `... IN (SELECT project_code FROM project)`），执行时报
        # 「no such table」。这类错误静态就能判出来，不必等数据库报错——
        # 而且带着「哪张表不在这个库」的提示重生成，比让修复回路去猜有效。
        if sql_result is not None and self.config.enable_cross_db_guard:
            try:
                sql_result = self._enforce_step_database(
                    sql_result=sql_result,
                    cot_step=sql_cot_step,
                    generator=sql_generator,
                    dialect=step_dialect,
                    previous_results=previous_results,
                    step_index=step_index,
                    certified_examples=certified_examples,
                )
            except RawProjectionError as exc:
                return self._raw_projection_failure(sql_cot_step, local_schema, exc)

        if generation_error is not None:
            raise _ModelUnavailable("sql_generation", generation_error)

        execution_request = sql_result.to_execution_request()

        with stage(
            "execution",
            _logger,
            step=step_index,
            database=sql_cot_step.database,
        ) as execution_stage:
            denied = self._check_table_access(
                database=sql_cot_step.database,
                sql=sql_result.sql,
                access=access,
            )
            if denied:
                execution_result = MCPExecutionResult(
                    database=sql_cot_step.database,
                    sql=sql_result.sql,
                    success=False,
                    error=(
                        f"没有访问权限：{', '.join(denied)}。"
                        "请联系管理员申请该表的查询权限。"
                    ),
                )
                ACCESS_DENIED.inc(database=sql_cot_step.database)
                SQL_EXECUTIONS.inc(
                    database=sql_cot_step.database, status="denied"
                )
                execution_stage.set(
                    success=False,
                    row_count=0,
                    denied_tables=denied,
                    error=execution_result.error,
                )
            else:
                execution_result = self._execute_unless_self_comparison(
                    execution_request["database"], execution_request["sql"], step_dialect
                )
                SQL_EXECUTIONS.inc(
                    database=sql_cot_step.database,
                    status="ok" if execution_result.success else "error",
                )
                execution_stage.set(
                    success=execution_result.success,
                    row_count=execution_result.row_count,
                    truncated=execution_result.truncated,
                    error=execution_result.error,
                )

        final_sql = sql_result.sql
        repair_attempts: List[dict] = []

        if not execution_result.success and self.config.enable_repair and not denied:
            try:
                with stage(
                    "repair",
                    _logger,
                    step=step_index,
                    database=sql_cot_step.database,
                    error=execution_result.error,
                ) as repair_stage:
                    outcome = self.sql_repair.repair(
                        database=sql_cot_step.database,
                        sql=sql_result.sql,
                        error_message=execution_result.error or "",
                        local_schema=local_schema,
                        sql_dialect=step_dialect,
                        execute=lambda database, sql: self._execute_unless_self_comparison(
                            database, sql, step_dialect
                        ),
                    )
                    repair_attempts = [
                        attempt.to_dict() for attempt in outcome.attempts
                    ]
                    SQL_REPAIRS.inc(
                        result="repaired" if outcome.repaired else "failed"
                    )
                    repair_stage.set(
                        attempts=len(repair_attempts),
                        repaired=outcome.repaired,
                        final_sql=outcome.final_sql,
                    )
            except ModelCallError as exc:
                # 修复要靠模型重写 SQL。模型不可用时这里以前没有接住，异常一路冒到
                # HTTP 层变成 500——规划、生成阶段遇到同一个错误都是走降级的，
                # 修复不该是例外。
                SQL_REPAIRS.inc(result="model_unavailable")
                raise _ModelUnavailable("repair", exc) from exc
            if outcome.repaired and outcome.final_result is not None:
                execution_result = outcome.final_result
                final_sql = outcome.final_sql

        # 单位先算出来：语义自检要拿它当已知事实（见 self_check._UNIT_FACTS）
        units, records, metadata = self._provenance(
            database=sql_cot_step.database,
            sql=final_sql,
            execution_result=execution_result,
        )

        # 软异常校验：SQL 跑通了，但结果未必对
        validation_payload: dict = {}
        if execution_result.success and self.config.enable_validation and not denied:
            with stage(
                "validation",
                _logger,
                step=step_index,
                database=sql_cot_step.database,
            ) as validation_stage:
                outcome = self.result_validator.validate(
                    query=query,
                    sql=final_sql,
                    row_count=execution_result.row_count,
                    expected_cardinality=getattr(
                        sql_cot_step, "expected_cardinality", ""
                    ),
                    is_final_step=(step_index == len(cot_result.steps)),
                    preceding_summary=self._preceding_summary(
                        cot_result.steps, step_index
                    ),
                    step_objective=(
                        f"{sql_cot_step.operation_instruction}"
                        f"（输出：{sql_cot_step.output_target}）"
                        if self.config.enable_intermediate_self_check
                        else ""
                    ),
                    column_units=units,
                )
                validation_payload = outcome.to_dict()
                for finding in outcome.findings:
                    RESULT_ANOMALIES.inc(
                        anomaly=finding.anomaly.value,
                        severity=finding.severity,
                    )
                validation_stage.set(
                    passed=outcome.passed,
                    findings=[f.anomaly.value for f in outcome.findings],
                )

        step_log = StepExecutionLog(
            database=sql_cot_step.database,
            cot_step=sql_cot_step,
            local_schema=local_schema.to_prompt_context(),
            sql=sql_result.sql,
            execution_request=execution_request,
            execution_result=execution_result.to_dict(),
            repair_attempts=repair_attempts,
            final_sql=final_sql,
            validation=validation_payload,
            column_units=units,
            source_records=records,
            column_metadata=metadata,
        )
        return step_log, execution_result

    def _finish(
        self,
        query: str,
        prepared: "_PreparedQuery",
        cot_result,
        step_logs: List[StepExecutionLog],
    ) -> PipelineResult:
        """完成日志与结果组装。"""
        retrieval_result = prepared.retrieval_result
        normalization = prepared.normalization
        resolved_keywords = prepared.keywords
        schema_context = prepared.schema_context
        retrieved_columns = [
            f"{database}.{table_name}.{getattr(column, 'column_name', '')}"
            for database, graph in retrieval_result.schema_graphs.items()
            for table_name, columns in (getattr(graph, "columns", {}) or {}).items()
            for column in columns
            if getattr(column, "column_name", "")
        ]

        log_event(
            _logger,
            logging.INFO,
            "pipeline.completed",
            steps=len(step_logs),
            databases=list(retrieval_result.selected_databases),
            succeeded_steps=sum(
                1 for log in step_logs if log.execution_result.get("success")
            ),
            repaired_steps=sum(1 for log in step_logs if log.repair_attempts),
            usage=usage_summary(),
        )

        return PipelineResult(
            query=query,
            retrieved_columns=retrieved_columns,
            prompt_columns=columns_in_prompt(schema_context),
            normalization=normalization.to_dict(),
            keywords=resolved_keywords,
            schema_context=schema_context,
            cot_output=cot_result.raw_output,
            step_logs=step_logs,
            selected_databases=list(retrieval_result.selected_databases),
            selected_tables={
                database: list(tables)
                for database, tables in retrieval_result.selected_tables.items()
            },
        )

    def _enforce_step_database(
        self,
        *,
        sql_result,
        cot_step,
        generator,
        dialect: str,
        previous_results: str,
        step_index: int,
        certified_examples: str = "",
    ):
        """
        校验 SQL 只引用当前步骤目标库的表；越界则带提示重生成一次。

        只重试一次：这是格式性错误，模型拿到明确提示通常一次就能改对；
        重试更多次的收益远低于延迟与成本。改不对就放行，交给执行与修复回路。
        """
        available = self._tables_of(cot_step.database)
        if not available:
            return sql_result

        foreign = [
            table
            for table in sorted(extract_referenced_tables(sql_result.sql))
            if table not in available
        ]
        if not foreign:
            return sql_result

        log_event(
            _logger,
            logging.WARNING,
            "sql.cross_database_reference",
            step=step_index,
            database=cot_step.database,
            foreign_tables=foreign,
        )
        CROSS_DB_VIOLATIONS.inc(database=cot_step.database)

        hint = (
            f"上一次生成的 SQL 引用了 {'、'.join(foreign)}，"
            f"但它们不在数据库 {cot_step.database} 中，执行会报 no such table。"
            f"当前步骤只能使用 {cot_step.database} 里的表；"
            "需要其他库的数据时，请使用「上一步结果」中已给出的值作为字面量条件，"
            "不要写跨库子查询或 JOIN。"
        )
        retry_step = CotStep(
            database=cot_step.database,
            processing_objects=cot_step.processing_objects,
            operation_instruction=f"{cot_step.operation_instruction}。{hint}",
            output_target=cot_step.output_target,
            expected_cardinality=getattr(cot_step, "expected_cardinality", ""),
        )

        try:
            retried = generator.generate(
                retry_step,
                sql_dialect=dialect,
                previous_results=previous_results,
                certified_examples=certified_examples,
            )
        except ModelCallError:
            return sql_result

        still_foreign = [
            table
            for table in sorted(extract_referenced_tables(retried.sql))
            if table not in available
        ]
        return sql_result if still_foreign else retried

    def _certified_examples(
        self,
        *,
        query: str,
        database: str,
        user_id: str,
        access,
    ) -> str:
        """
        取已确认正确的相似查询，渲染成 Prompt 段落。

        没有配置反馈来源、没有 user_id、或者一条都不相似时返回空串——
        这一段是可有可无的增强，任何异常都不应该影响正常生成。
        """
        if self.certified_lookup is None or not user_id:
            return ""

        try:
            examples = self.certified_lookup(
                query=query,
                database=database,
                user_id=user_id,
                # 绑到这个库的方言上：范例里的表名和关卡按同一条大小写规则判
                access=bind_dialect(access, self._dialect_name(database)),
            )
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "certified.lookup_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return ""

        # 只物化一次：lookup 返回生成器时，第二次 list() 会拿到空，
        # 结果是「日志说注入了 0 条，Prompt 里却有内容」这种对不上的现象
        materialized = list(examples or [])
        if not materialized:
            return ""

        from ai_sql_agent.feedback import render_certified_examples

        rendered = render_certified_examples(materialized)
        log_event(
            _logger,
            logging.INFO,
            "certified.injected",
            database=database,
            count=len(materialized),
        )
        return rendered

    def _tables_of(self, database: str) -> set:
        """目标库里可用的表名集合。"""
        meta = self.business_metas.get(database) or {}
        return set(meta.keys())

    @staticmethod
    def _preceding_summary(steps, step_index: int) -> str:
        """
        把当前步骤之前已经完成的工作压成一句话，交给语义自检。

        没有这一段时，跨库查询的最后一步会被判成「漏掉了问题里的筛选条件」——
        那些条件在前一步的库里已经执行过，结果以字面量形式写进了本步 SQL。
        """
        earlier = list(steps)[: max(step_index - 1, 0)]
        if not earlier:
            return ""
        return "；".join(
            f"步骤{index}（在 {step.database}）：{step.operation_instruction}"
            f"，输出 {step.output_target}"
            for index, step in enumerate(earlier, start=1)
        )

    def _table_owner(self) -> Dict[str, str]:
        """表名 → 所属库。四个业务库表名不重名；真重名时只保留先出现的，不猜。"""
        owner: Dict[str, str] = {}
        for database, meta in self.business_metas.items():
            for table_name in meta or {}:
                owner.setdefault(table_name, database)
        return owner

    def _plan_database_conflicts(self, cot_result) -> List[str]:
        """
        找出「步骤声明的数据库里没有它处理对象所引用的表」的步骤。

        这是跨库失败里最隐蔽的一种：计划看起来是两步，但第二步把第一步的表
        也写进了处理对象，SQL 生成必然引用不存在的表。执行时报 no such table
        还算好的，更糟的是模型改用同库的同名近义字段硬凑——SQL 跑通、结果是错的。
        """
        owner = self._table_owner()
        conflicts: List[str] = []

        for step in cot_result.steps:
            declared = (step.database or "").strip()
            if declared not in self.business_metas:
                if declared:
                    conflicts.append(
                        f"步骤{step.step_no} 声明的数据库「{declared}」不存在，"
                        f"只能从 {'、'.join(sorted(self.business_metas))} 里选"
                    )
                continue

            available = self._tables_of(declared)
            text = f"{step.processing_objects} {step.output_target}"
            foreign = sorted(
                {
                    table_name
                    for table_name, table_db in owner.items()
                    if table_db != declared
                    and table_name not in available
                    and re.search(rf"(?<![A-Za-z0-9_]){re.escape(table_name)}(?![A-Za-z0-9_])", text)
                }
            )
            if foreign:
                conflicts.append(
                    f"步骤{step.step_no} 声明数据库为 {declared}，"
                    f"但处理对象里出现了属于其他库的表 {'、'.join(foreign)}"
                    f"（{'、'.join(f'{t} 在 {owner[t]}' for t in foreign)}）。"
                    "请把它们拆到各自的步骤里，并用「输出目标」把关联键传给下一步"
                )

        return conflicts

    def _short_path_plan(self, query: str, retrieval_result):
        """
        短路径计划：不调规划模型，按检索结果直接构造单步计划。

        只在 decide_short_path 判定为简单单库查询时使用；处理对象直接取
        检索命中的表，操作指令就是原问题，交给 Coder 模型一次生成 SQL。
        """
        from ai_sql_agent.planning.objects import CotPlanningResult, CotPlanningStep

        database = next(
            (
                db
                for db, tables in retrieval_result.selected_tables.items()
                if tables
            ),
            "",
        )

        # 处理对象必须写到**字段级**：SQL 生成阶段按它裁剪局部 Schema，
        # 只给表名会把字段全裁掉，模型看不到可用字段，必然生成不出正确 SQL。
        # 短路径没有规划模型来挑字段，就把该库检索命中的字段全部带上。
        graph = (retrieval_result.schema_graphs or {}).get(database)
        objects = [
            f"{table_name}.{getattr(column, 'column_name', '')}"
            for table_name, columns in (getattr(graph, "columns", {}) or {}).items()
            for column in columns
            if getattr(column, "column_name", "")
        ]
        if not objects:
            objects = list(retrieval_result.selected_tables.get(database, []))

        step = CotPlanningStep(
            step_no=1,
            database=database,
            processing_objects="，".join(objects),
            operation_instruction=query,
            output_target=query,
            expected_cardinality="数条",
        )
        return CotPlanningResult(
            user_query=query,
            prompt="",
            raw_output="[short_path] 简单查询，跳过独立规划调用",
            steps=[step],
        )

    def _schema_insufficient_step(self, cot_step, step_index: int) -> StepExecutionLog:
        """Schema 不足时的步骤日志：给出可操作的说明而不是内部错误。"""
        message = (
            "当前可见的表不足以回答这个问题："
            f"{cot_step.processing_objects or '未找到相关表或字段'}。"
            "可能是数据库里没有相关表，也可能是你没有相关表的访问权限，"
            "需要的话联系管理员申请。"
        )
        log_event(
            _logger,
            logging.WARNING,
            "pipeline.schema_insufficient",
            step=step_index,
            database=cot_step.database,
            processing_objects=cot_step.processing_objects,
        )
        return StepExecutionLog(
            database=cot_step.database,
            cot_step=cot_step,
            local_schema="",
            sql="",
            execution_request={},
            execution_result={
                "database": cot_step.database,
                "sql": "",
                "success": False,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "error": message,
            },
        )

    # ------------------------------------------------------------------
    # 模型不可用时的降级
    # ------------------------------------------------------------------

    def _degrade(
        self,
        *,
        query: str,
        keywords: List[str],
        schema_context: str,
        retrieval_result,
        access: SchemaAccessFilter,
        user_id: str,
        failed_stage: str,
        error: ModelCallError,
    ) -> PipelineResult:
        """
        模型调用失败后的降级出口。

        优先重放该用户同一问题上次成功的 SQL——SQL 是缓存的，
        数据仍然实时查；重放前照样过一遍数据权限（权限可能已经收紧）。
        没有可重放的历史就返回明确的降级结果，而不是抛 5xx。
        """
        log_event(
            _logger,
            logging.ERROR,
            "pipeline.model_unavailable",
            failed_stage=failed_stage,
            error_category=error.category,
            error=str(error),
        )

        step_logs = self._replay_history(query=query, user_id=user_id, access=access)
        reason = (
            "model_unavailable_replayed_sql" if step_logs else "model_unavailable"
        )

        DEGRADED_RESPONSES.inc(reason=reason, failed_stage=failed_stage)
        log_event(
            _logger,
            logging.WARNING,
            "pipeline.degraded",
            reason=reason,
            failed_stage=failed_stage,
            replayed_steps=len(step_logs),
        )

        return PipelineResult(
            query=query,
            keywords=keywords,
            schema_context=schema_context,
            cot_output="",
            step_logs=step_logs,
            selected_databases=list(retrieval_result.selected_databases),
            selected_tables={
                database: list(tables)
                for database, tables in retrieval_result.selected_tables.items()
            },
            degraded=True,
            degrade_reason=reason,
        )

    def _replay_history(
        self,
        *,
        query: str,
        user_id: str,
        access: SchemaAccessFilter,
    ) -> List[StepExecutionLog]:
        """重放该用户同一问题上次成功的 SQL；任一步失败就整体放弃。"""
        if self.sql_history_lookup is None or not user_id:
            return []

        try:
            history = list(self.sql_history_lookup(user_id, query))
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "pipeline.history_lookup_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

        if not history:
            return []

        step_logs: List[StepExecutionLog] = []
        for index, (database, sql) in enumerate(history, start=1):
            # 取消检查点：降级重放的每一步之前
            check_cancelled()
            if not sql.strip():
                return []

            # 权限可能在上次查询之后收紧，重放前必须重新校验
            denied = self._check_table_access(
                database=database, sql=sql, access=access
            )
            if denied:
                return []

            # 历史里存着的 SQL 也可能是修复之前写下的不筛选条件，同一道检查拦下就放弃重放
            execution_result = self._execute_unless_self_comparison(
                database, sql, self.dialects.get(database, "sqlite")
            )
            SQL_EXECUTIONS.inc(
                database=database,
                status="ok" if execution_result.success else "error",
            )
            if not execution_result.success:
                return []

            units, records, metadata = self._provenance(
                database=database, sql=sql, execution_result=execution_result
            )
            step_logs.append(
                StepExecutionLog(
                    database=database,
                    cot_step=None,
                    local_schema="",
                    sql=sql,
                    execution_request={"database": database, "sql": sql},
                    execution_result=execution_result.to_dict(),
                    final_sql=sql,
                    column_units=units,
                    source_records=records,
                    column_metadata=metadata,
                )
            )
            log_event(
                _logger,
                logging.INFO,
                "pipeline.replayed_step",
                step=index,
                database=database,
                row_count=execution_result.row_count,
            )

        return step_logs

    # ------------------------------------------------------------------
    # Schema 热重建
    # ------------------------------------------------------------------

    def _current_fingerprints(self) -> Dict[str, str]:
        """取各业务库当前的 Schema 指纹。"""
        return {
            database.name: database_fingerprint(
                dialect=database.dialect,
                db_path=str(database.db_path),
                dsn=database.dsn,
            )
            for database in self.databases
        }

    def _refresh_view_catalog(self) -> None:
        """
        重扫各库的表与视图目录。扫描失败的库在目录里登记为失败（对受限角色拒绝一切，
        直到下一次扫描成功），所以这里不向上抛。
        """
        for database in self.databases:
            self.view_catalog.refresh(
                database.name,
                database.dialect,
                db_path=str(database.db_path or ""),
                dsn=database.dsn or "",
            )

    def detect_schema_changes(self) -> List[str]:
        """
        对比指纹，返回发生变化的库名。

        指纹只读系统表，成本远低于重建索引，适合定时轮询。
        取不到指纹（库暂时连不上）时返回空串，不触发重建。
        """
        changed: List[str] = []
        for name, fingerprint in self._current_fingerprints().items():
            if not fingerprint:
                continue
            if self.schema_fingerprints.get(name) != fingerprint:
                changed.append(name)
        return changed

    def reload_schema(self, reason: str = "manual", *, force: Optional[bool] = None) -> List[str]:
        """
        重建三层检索索引并原子替换，返回发生变化的库名。

        重建在新对象上进行，构建完成后一次性替换引用——期间进来的请求
        继续用旧索引，不会读到半成品。配了 pgvector 时，没变化的命名空间
        会命中指纹缓存，不重复调用 Embedding。

        `force` 不给时按 `reason` 判断：人手动触发的（`manual`、`manual:<谁>`）一定重建，
        定时轮询只在结构变了时重建。管理接口以前传的是 `manual:<谁>`，
        和这里比的 `manual` 对不上，结构没变就什么都不做——手动重建从来没生效过。
        """
        if force is None:
            force = reason == "manual" or reason.startswith("manual:")
        with self._reload_lock:
            # 指纹只看表，视图的增删它感知不到；目录扫描很轻，每次轮询都做，
            # 不依赖「有没有变化」。
            self._refresh_view_catalog()
            changed = self.detect_schema_changes()
            if not changed and not force:
                return []

            try:
                service = self._build_schema_retrieval_service()
            except Exception as exc:
                SCHEMA_RELOADS.inc(result="error")
                log_event(
                    _logger,
                    logging.ERROR,
                    "schema.reload_failed",
                    reason=reason,
                    changed=changed,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

            # 原子替换：Python 的属性赋值本身是原子的
            self.schema_retrieval_service = service
            self.schema_fingerprints = self._current_fingerprints()

        SCHEMA_RELOADS.inc(result="ok")
        log_event(
            _logger,
            logging.INFO,
            "schema.reloaded",
            reason=reason,
            changed=changed,
            databases=[database.name for database in self.databases],
        )
        return changed

    def _check_table_access(
        self,
        database: str,
        sql: str,
        access: SchemaAccessFilter,
    ) -> List[str]:
        """
        执行前的数据权限预判，返回被拒的表 / 列标识。

        **这不是那道强制关卡**——关卡在每个执行器的 `execute()` 入口
        （`ai_sql_agent.execution.authorization.enforce`，判据见
        `ai_sql_agent.auth.enforcement`），它拦不住就没人拦得住。这里提前判一次是为了
        两件关卡做不了的事：把越权当成一种**不该重试**的失败（改 SQL 也拿不到
        权限，进修复回路只会白烧模型调用），以及把被拒的名字落进审计的
        `denied_tables`。

        判据与关卡同源（`check_sql_access`），所以两处不会给出不同答案。
        """
        decision = check_sql_access(
            access,
            database=database,
            sql=sql,
            dialect=self._dialect_name(database),
            views=self.view_catalog,
        )
        if decision.allowed:
            return []

        denied = list(decision.denied_tables) or list(decision.denied_columns)
        if not denied:
            # 解析失败这类没有具体名字的拒绝：给出理由，别返回空列表——
            # 空列表在调用点等于「放行」。
            denied = [decision.reason]

        log_event(
            _logger,
            logging.WARNING,
            "access.denied",
            database=database,
            tables=list(decision.denied_tables),
            columns=list(decision.denied_columns),
            reason=decision.reason,
            sql=sql,
        )
        return denied

    def _provenance(self, *, database: str, sql: str, execution_result):
        """
        算这一步的单位与来源记录（契约 §4.6.5 / §4.6.6）。

        失败的步骤没有结果列，自然也没有单位可谈。算不出来一律返回空——
        契约把「缺失」定义成「未知」，而不是「没有」。
        """
        if not execution_result.success:
            return {}, [], {}

        dialect = self._dialect_name(database)
        columns = list(execution_result.columns or [])
        units = provenance.column_units(
            self.semantic,
            database=database,
            sql=sql,
            dialect=dialect,
            columns=columns,
        )
        records = provenance.source_records(
            self.semantic,
            database=database,
            sql=sql,
            dialect=dialect,
            columns=columns,
            rows=list(execution_result.rows or []),
        )
        metadata = provenance.column_metadata(self.semantic, database=database, sql=sql,
                                               dialect=dialect, columns=columns)
        return units, records, metadata

    def _dialect_name(self, database: str) -> str:
        """这个库的执行器方言，解析 SQL 时要用。"""
        executor = self.mcp_router.executors.get(database)
        return str(getattr(executor, "dialect", "") or "")

    def _resolve_databases(self) -> List[DatabaseConfig]:
        """
        解析数据库连接配置。

        优先级：显式传入的 `config.databases` > 语义层里声明的连接。
        语义层同时决定「有哪些库」和「怎么连」，接真实业务库时不再需要改代码。

        Demo 数据只在使用内置演示元数据时创建：配了真实语义目录还去建 Demo 库，
        等于往生产路径里塞演示数据。
        """
        databases = self.config.databases

        if databases is None:
            databases = [
                DatabaseConfig(
                    name=database.name,
                    db_path=database.db_path,
                    dialect=database.dialect,
                    dsn=database.resolved_dsn(),
                )
                for database in self.semantic.databases.values()
            ]
            # 兼容既有用法：config.database_name / db_path 覆盖同名库的路径，
            # 让「换一个 SQLite 文件跑」这种测试与 Demo 用法不必构造整份配置。
            if self.config.db_path:
                databases = [
                    DatabaseConfig(
                        name=database.name,
                        db_path=self.config.db_path,
                        dialect=database.dialect,
                        dsn=database.dsn,
                    )
                    if database.name == self.config.database_name
                    else database
                    for database in databases
                ]

        creators = (
            {
                "erp_db": create_erp_demo_database,
                "pm_db": create_pm_demo_database,
                "mes_db": create_mes_demo_database,
                "wms_db": create_wms_demo_database,
            }
            if self.semantic.is_demo
            else {}
        )

        resolved: List[DatabaseConfig] = []
        for database in databases:
            if database.dialect != "sqlite":
                # 非 SQLite 库由外部准备，这里不建 Demo 数据
                resolved.append(database)
                continue

            creator = creators.get(database.name)
            db_path = Path(database.db_path)
            if creator is not None:
                db_path = creator(db_path)
            resolved.append(
                DatabaseConfig(
                    name=database.name,
                    db_path=db_path,
                    dialect=database.dialect,
                    dsn=database.dsn,
                )
            )

        return resolved

    def _resolve_business_metas(self) -> Dict[str, dict]:
        """
        解析各库业务元数据（表/字段的业务含义与别名），全部来自语义层。

        语义层里没有的库拿到空元数据：链路照样能跑，但检索退化成按字段名
        字面匹配，口语问法基本召不回来。这种情况会在启动日志里显式告警，
        因为它的现象是「模型答不准」，很容易被误判成模型能力问题。
        """
        metas: Dict[str, dict] = {}
        missing: List[str] = []
        for database in self.databases:
            semantics = self.semantic.databases.get(database.name)
            if semantics is None or not semantics.tables:
                missing.append(database.name)
                metas[database.name] = {}
                continue
            metas[database.name] = semantics.tables

        if missing:
            log_event(
                _logger,
                logging.WARNING,
                "semantic.missing_metadata",
                databases=missing,
                source=self.semantic.source,
                hint=(
                    "这些库没有业务元数据，检索只能按字段名字面匹配。"
                    "用 python -m ai_sql_agent.semantic export/validate 补齐后配置 AI_SQL_SEMANTIC_DIR"
                ),
            )
        return metas

    def _resolve_schema_store(
        self,
        schema_stores: Dict[str, LocalSchemaStore],
        cot_step: CotStep,
    ) -> Optional[LocalSchemaStore]:
        """
        根据 CoT 步骤的目标数据库选择 SchemaStore。

        数据库名不在检索结果中时，回退到包含步骤所引用表的库；
        仍找不到时使用第一个库，保证流程不中断。

        一个库都没有时返回 None——调用方按「Schema 不足」处理。
        """
        store = schema_stores.get(cot_step.database)
        if store is not None:
            return store

        mentioned = "；".join(
            [
                cot_step.processing_objects,
                cot_step.operation_instruction,
                cot_step.output_target,
            ]
        )

        for candidate in schema_stores.values():
            if any(table_name in mentioned for table_name in candidate.tables):
                return candidate

        return next(iter(schema_stores.values()), None)

    def _execute_unless_self_comparison(self, database: str, sql: str, dialect: str):
        """
        执行一条生成的 SQL；筛选条件里有「字段和它自己比较」的就不执行，当成一次执行失败。

        这种条件恒为真，执行了也不报错，只会悄悄把整张表查出来（见
        `ai_sql_agent.generation.self_comparison`）。当成失败交给修复回路，比一个
        行数正常、内容全错的结果好：修复带着这句报错重写，修不好用户也看得见。
        首次生成和每一次修复都走这里。
        """
        from ai_sql_agent.generation.self_comparison import find_self_comparisons

        found = find_self_comparisons(sql, dialect)
        if found:
            return MCPExecutionResult(
                database=database,
                sql=sql,
                success=False,
                error=(
                    f"筛选条件把字段和它自己比较：{'；'.join(found)}。这种条件不是按取值筛选——"
                    "IN / = 恒为真，会把整张表查出来；NOT IN / != 恒为假，一行都查不出来——"
                    "所以没有执行。改成用具体取值（字面量）的筛选条件。"
                ),
            )
        return self.mcp_router.execute({"database": database, "sql": sql})

    @staticmethod
    def _serialize_step_result(execution_result) -> str:
        """
        序列化当前步骤执行结果，供下一步 SQL 生成回填。

        除了原始 JSON，额外把每一列的取值拼成**可直接粘贴的 IN 字面量**。
        原因：跨库查询的第二步失败样本里，相当一部分是模型看着一段 JSON
        自己又去写跨库子查询或 JOIN。把答案以「直接抄这一行」的形式摆出来，
        比多写几句「请改写为字面量」的指令有效得多——模型跟随格式示例
        远强于跟随文字指令。

        没有行时返回空串。规划里认得出依赖这一步的步骤不会执行（见 `_run_steps`）；
        认不出的照常生成，拿到的是空回填，不是更早那一步的取值。
        """
        if not execution_result.success or not execution_result.rows:
            return ""

        rows = execution_result.rows[:_MAX_BACKFILL_ROWS]
        columns = list(execution_result.columns or [])

        literal_lines = []
        for column in columns:
            literals = _as_sql_literals(row.get(column) for row in rows)
            if literals:
                literal_lines.append(f"  {column} IN ({', '.join(literals)})")

        sections = [f"共 {len(rows)} 行，字段：{', '.join(columns) or '(无)'}"]
        if literal_lines:
            sections.append("可直接用作本步骤的字面量筛选条件：")
            sections.extend(literal_lines)
        sections.append("完整结果（JSON）：")
        sections.append(
            json.dumps(
                {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return "\n".join(sections)

    @property
    def embedding_status(self) -> Dict[str, object]:
        """检索索引实际在用的向量：本地还是远端、哪个模型、几维，以及有没有退回本地。"""
        status = getattr(self.schema_retrieval_service, "embedding_status", None)
        return dict(status) if status else describe_embedding(local_embedding_client())

    def build_schema_retrieval_service(self, embedding_client) -> ThreeTierSchemaRetrievalService:
        """
        用指定的 Embedding 建一份新的检索索引，**不换上**。建不成照常抛错。

        页面换 Embedding 时用：先确认新模型能把整个索引建出来，再保存、再换上
        （`install_schema_retrieval_service`）。中途失败的话正在用的索引一点没动。
        """
        return self._build_schema_retrieval_service(embedding_client)

    def install_schema_retrieval_service(
        self, service: ThreeTierSchemaRetrievalService, reason: str = "embedding"
    ) -> None:
        """换上一份建好的检索索引。正在处理的请求用完旧的，之后的请求用新的。"""
        with self._reload_lock:
            self.schema_retrieval_service = service
            self.schema_fingerprints = self._current_fingerprints()
        log_event(
            _logger,
            logging.INFO,
            "schema.index_installed",
            reason=reason,
            **{f"embedding_{key}": value for key, value in self.embedding_status.items()},
        )

    def _build_schema_retrieval_service(self, embedding_client=None) -> ThreeTierSchemaRetrievalService:
        """
        构建三层 Schema 检索服务。

        不指定 Embedding 时按配置选（`embedding_backend`）。配的是远端、但它调不通或缺 Key 时，
        退回本地哈希向量把服务建起来——Schema 检索建不起来整个服务就起不来，
        而少了语义召回只是口语问法召回得差一些。退回的原因记在 `embedding_status` 里。
        指定了 Embedding（页面切换时）就只用它，失败照常抛。
        """
        if embedding_client is not None:
            service = self._assemble_schema_retrieval_service(embedding_client)
            service.embedding_status = describe_embedding(embedding_client)
            return service

        try:
            client = schema_embedding_client()
            service = self._assemble_schema_retrieval_service(client)
            service.embedding_status = describe_embedding(client)
            return service
        except (ModelCallError, ValueError) as exc:
            if get_settings().embedding_backend != "remote":
                raise
            reason = f"{type(exc).__name__}: {exc}"[:300]
            log_event(
                _logger,
                logging.WARNING,
                "schema.embedding_fallback",
                error_category=type(exc).__name__,
                error=reason,
            )
            client = local_embedding_client()
            service = self._assemble_schema_retrieval_service(client)
            service.embedding_status = describe_embedding(client, fallback=reason)
            return service

    def _assemble_schema_retrieval_service(self, embedding_client) -> ThreeTierSchemaRetrievalService:
        """用给定的 Embedding 把三层检索服务建出来。"""
        rerank_client = build_rerank_client()

        catalog = {item["name"]: item for item in self.semantic.catalog()}

        sources = []
        for database in self.databases:
            catalog_item = catalog.get(database.name, {})
            sources.append(
                DatabaseSource(
                    name=database.name,
                    db_path=str(database.db_path),
                    business_meta=self.business_metas.get(database.name, {}),
                    description=catalog_item.get("description", ""),
                    aliases=list(catalog_item.get("aliases", [])),
                    sample_size=self.config.sample_size,
                    dialect=database.dialect,
                    dsn=database.dsn,
                )
            )

        columns_per_database, min_columns_per_table = column_budget_for(embedding_client)

        return ThreeTierSchemaRetrievalService(
            cross_database_links=self.semantic.cross_database_links,
            metric_disambiguation=self.semantic.metric_disambiguation,
            sources=sources,
            embedding_client=embedding_client,
            rerank_client=rerank_client,
            keyword_extractor=None,
            config=ThreeTierRetrievalConfig(
                max_databases=3,
                max_tables=8,
                column_config=HybridSchemaRetrievalConfig(
                    per_keyword_top_k=20,
                    include_join_columns=True,
                    min_columns_per_table=min_columns_per_table,
                    rrf_config=RRFFusionConfig(
                        rrf_k=60,
                        truncate_multiplier=6,
                        min_fused_top_k=10,
                        max_fused_top_k=50,
                        final_top_k=columns_per_database,
                        route_weights={
                            "keyword": 1.0,
                            "vector": 1.0,
                        },
                    ),
                    rerank_top_multiplier=2,
                    rerank_min_top_n=2,
                    rerank_max_top_n=15,
                ),
            ),
        )

    def _build_cot_planner(self) -> CotPlanner:
        """
        构建 CoT 规划器。
        """
        return CotPlanner(
            thinking_client=ThinkingModelClient(
                ThinkingModelConfig(
                    use_mock_when_no_api_key=True,
                    temperature=0.0,
                )
            )
        )

    def _build_mcp_router(self) -> MCPRouter:
        """
        构建 MCP 路由器，为每个数据库注册执行器。

        use_mcp_protocol=True 时执行走真实 MCP stdio 协议；
        否则按库的 dialect 选择进程内执行器（SQLite / PostgreSQL）。
        """
        router = MCPRouter()

        protocol_executor_cls = None
        if self.config.use_mcp_protocol:
            from ai_sql_agent.execution import load_protocol_executor

            protocol_executor_cls = load_protocol_executor()

        for database in self.databases:
            if protocol_executor_cls is not None:
                executor = protocol_executor_cls(database=database.name)
            elif get_dialect(database.dialect).name == "postgres":
                executor = PostgresMCPExecutor(
                    database=database.name,
                    dsn=database.dsn,
                    readonly=True,
                    max_rows=self.config.max_rows,
                    max_execution_seconds=self.config.max_execution_seconds,
                    max_result_bytes=self.config.max_result_bytes,
                )
            else:
                executor = SQLiteMCPExecutor(
                    database=database.name,
                    db_path=database.db_path,
                    readonly=True,
                    max_rows=self.config.max_rows,
                    max_execution_seconds=self.config.max_execution_seconds,
                    max_result_bytes=self.config.max_result_bytes,
                    max_value_bytes=self.config.max_value_bytes,
                )

            router.register_executor(database=database.name, executor=executor)

        return router
