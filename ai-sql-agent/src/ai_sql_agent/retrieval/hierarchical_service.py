from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ai_sql_agent.retrieval.access import (
    ALLOW_ALL,
    SchemaAccessFilter,
    bind_dialect,
    filter_schema_graph,
)
from ai_sql_agent.retrieval.bm25 import BM25Index
from ai_sql_agent.retrieval.hybrid_schema_retrieval_service import (
    HybridSchemaRetrievalConfig,
    HybridSchemaRetrievalResult,
    HybridSchemaRetrievalService,
)
from ai_sql_agent.retrieval.rrf_fusion_client import (
    RRFFusionClient,
    RRFFusionConfig,
    RouteRecallResult,
)
from ai_sql_agent.core import ModelCallError, get_logger, log_event
from ai_sql_agent.retrieval.vector_backend import create_vector_index, search_or_skip

_logger = get_logger("retrieval")


@dataclass
class DatabaseSource:
    """三层检索中的单个数据库来源。"""

    name: str
    db_path: str = ""
    """SQLite 库文件路径；dialect 为 postgres 时忽略。"""

    business_meta: dict = field(default_factory=dict)
    description: str = ""
    aliases: List[str] = field(default_factory=list)
    sample_size: int = 5

    dialect: str = "sqlite"
    """Schema 来源方言：sqlite 读库文件，postgres 读 information_schema。"""

    dsn: str = ""
    """PostgreSQL 连接串；dialect="postgres" 时必填。"""


@dataclass
class TierHit:
    """库级或表级检索命中。"""

    name: str
    score: float
    sources: List[str] = field(default_factory=list)


@dataclass
class ThreeTierRetrievalConfig:
    """三层检索配置。"""

    max_databases: int = 2
    """库级检索保留的数据库数量。"""

    max_tables: int = 8
    """表级检索保留的表数量（跨库合计）。"""

    tier_per_keyword_top_k: int = 10
    """库级/表级每个关键词每一路召回的 Top-K。"""

    tier_rrf_k: int = 60
    """库级/表级 RRF 融合参数。"""

    database_keyword_guard: bool = True
    """
    库级关键词证据裁剪。

    分词器为提升字段级召回保留了中文单字 Token，副作用是几乎所有
    文档在 BM25 路都会有弱命中。库级候选很少，单字噪声足以把无关库
    带进后续检索，因此这里要求更强的证据：至少一个关键词以 bigram
    及以上长度字面命中库级文本。只要有库满足强证据，就丢弃不满足的库。
    """

    column_config: Optional[HybridSchemaRetrievalConfig] = None
    """字段级检索配置，不传时使用 HybridSchemaRetrievalConfig 默认值。"""


@dataclass
class ThreeTierRetrievalResult:
    """三层检索结果。"""

    query: str
    keywords: List[str]
    database_hits: List[TierHit]
    selected_databases: List[str]
    table_hits: List[TierHit]
    selected_tables: Dict[str, List[str]]
    column_results: Dict[str, HybridSchemaRetrievalResult]
    schema_graphs: Dict[str, object]

    cross_database_notes: str = ""
    """
    跨库约束说明，只在**实际贡献了字段**的库多于一个时非空。

    刻意不放进 to_prompt_context()：Schema 上下文要过 Token 预算裁剪，
    拼在末尾会被第一个裁掉——而这段恰恰是最不能丢的。由调用方在裁剪
    之后追加。
    """

    def to_prompt_context(self, compact: bool = False) -> str:
        """拼接各库 SchemaGraph 的 Prompt 上下文。"""
        return "\n\n".join(self.prompt_sections(compact=compact))

    def prompt_sections(self, compact: bool = False) -> List[str]:
        """按库拆开的上下文片段，供按库分配 Token 预算使用。"""
        return [
            graph.to_prompt_context(compact=compact)
            for graph in self.schema_graphs.values()
            if getattr(graph, "tables", None)
        ]


class ThreeTierSchemaRetrievalService:
    """
    三层 Schema 检索服务：库级 → 表级 → 字段级逐层收窄。

    每一层都是 BM25 关键词召回 + 向量召回 + RRF 融合；
    字段级在收窄后的候选表内检索，并接入 Rerank 精排。
    """

    def _build_cross_database_notes(self, selected_databases: Sequence[str]) -> str:
        """
        构建跨库约束说明。只在命中多个库时生成，避免污染单库查询的 Prompt。

        这段是跨库准确率的关键：不写清楚，模型会把两个库压进一条 SQL
        （执行时报 no such table），或者干脆留在单库里用同名字段硬凑
        （SQL 跑通了、结果是错的，更危险）。
        """
        # 传进来的是**实际有表进入 Schema 图**的库。用 Tier-1 的候选库判定会
        # 误伤：库级检索会保留若干候选，单库问题也常命中 2-3 个候选库，
        # 那样每条查询都会被塞进跨库约束，纯属噪声。
        selected = [db for db in selected_databases if db]
        if len(selected) < 2:
            return ""

        chosen = set(selected)
        lines = [
            "# 跨库约束（本次检索命中多个数据库，务必遵守）",
            "",
            f"本次涉及 {len(selected)} 个数据库：{'、'.join(selected)}。",
            "它们是**物理隔离的独立数据库**，之间没有外键约束：",
            "",
            "1. **禁止在一条 SQL 里引用多个库的表**，也不能写跨库 JOIN 或跨库子查询；",
            "2. 跨库查询必须**拆成多个步骤**，每步只访问一个数据库；",
            "3. 第二步起，把上一步结果中的值**作为字面量条件**写入（如 IN (...)），"
            "不要再去查上一步那个库。",
        ]

        links = [
            link
            for link in (self.cross_database_links or [])
            if link.get("left", "").split(".")[0] in chosen
            and link.get("right", "").split(".")[0] in chosen
        ]
        if links:
            lines += ["", "## 已知跨库关联键（按命名约定推断，非数据库外键）", ""]
            for link in links:
                lines.append(
                    f"- `{link['left']}` ↔ `{link['right']}`：{link['meaning']}"
                )
            lines.append("")
            lines.append(
                "这些关联只用于**跨步骤传值**：先在一个库里查出关联键，"
                "再把值带到另一个库的步骤里，不要直接 JOIN。"
            )

        metrics = self.metric_disambiguation or []
        if metrics:
            lines += ["", "## 指标口径区分（跨库同名字段容易选错）", ""]
            for item in metrics:
                lines.append(
                    f"- 「{item['term']}」→ 用 `{item['correct']}`；"
                    f"**不要**用 {item['wrong']}"
                )

        return "\n".join(lines)

    def __init__(
        self,
        sources: Sequence[DatabaseSource],
        embedding_client,
        rerank_client,
        keyword_extractor=None,
        config: Optional[ThreeTierRetrievalConfig] = None,
        cross_database_links: Optional[Sequence[dict]] = None,
        metric_disambiguation: Optional[Sequence[dict]] = None,
    ):
        if not sources:
            raise ValueError("至少需要一个 DatabaseSource")

        self.config = config or ThreeTierRetrievalConfig()
        self.cross_database_links = list(cross_database_links or [])
        self.metric_disambiguation = list(metric_disambiguation or [])
        self.sources = {source.name: source for source in sources}
        self.keyword_extractor = keyword_extractor
        self.embedding_client = embedding_client

        # 字段级：每个库复用现有的混合检索服务
        self.column_services: Dict[str, HybridSchemaRetrievalService] = {}
        for source in sources:
            if source.dialect == "postgres":
                builder = HybridSchemaRetrievalService.from_postgres
                source_kwargs = {"dsn": source.dsn}
            else:
                builder = HybridSchemaRetrievalService.from_sqlite
                source_kwargs = {"db_path": source.db_path}

            self.column_services[source.name] = builder(
                **source_kwargs,
                database_name=source.name,
                business_meta=source.business_meta,
                embedding_client=embedding_client,
                rerank_client=rerank_client,
                keyword_extractor=None,
                config=self.config.column_config,
                sample_size=source.sample_size,
            )

        self._build_database_tier()
        self._build_table_tier()

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def _build_database_tier(self) -> None:
        """构建库级索引文档。"""
        self.database_names: List[str] = []
        keyword_texts: List[str] = []
        vector_texts: List[str] = []

        for name, source in self.sources.items():
            service = self.column_services[name]
            table_lines = []
            for table_name, table in service.tables.items():
                description = getattr(table, "description", "")
                table_lines.append(f"{table_name} {description}")

            keyword_texts.append(
                "\n".join(
                    part
                    for part in [
                        name,
                        source.description,
                        " ".join(source.aliases),
                        "\n".join(table_lines),
                    ]
                    if part
                )
            )
            vector_texts.append(
                "\n".join(
                    part
                    for part in [
                        f"数据库：{name}",
                        f"数据库含义：{source.description}" if source.description else "",
                        f"数据库别名：{', '.join(source.aliases)}" if source.aliases else "",
                        f"包含数据表：{'；'.join(table_lines)}" if table_lines else "",
                    ]
                    if part
                )
            )
            self.database_names.append(name)

        self.db_keyword_texts = [text.lower() for text in keyword_texts]
        self.db_keyword_index = BM25Index(keyword_texts)
        self.db_vector_index = create_vector_index(
            self.embedding_client, namespace="tier_databases"
        )
        self.db_vector_index.build(vector_texts)

    def _build_table_tier(self) -> None:
        """构建表级索引文档（跨库统一编号）。"""
        self.table_refs: List[tuple[str, str]] = []  # (database, table_name)
        keyword_texts: List[str] = []
        vector_texts: List[str] = []

        for name in self.database_names:
            service = self.column_services[name]
            meta = service.business_meta or {}

            for table_name, table in service.tables.items():
                table_meta = meta.get(table_name, {})
                description = table_meta.get(
                    "description", getattr(table, "description", "")
                )
                aliases = table_meta.get("aliases", getattr(table, "aliases", []) or [])

                column_parts = []
                for column in service.columns:
                    if column.table_name != table_name:
                        continue
                    column_parts.append(column.column_name)
                    column_parts.extend(getattr(column, "aliases", []) or [])

                keyword_texts.append(
                    "\n".join(
                        part
                        for part in [
                            name,
                            table_name,
                            description,
                            " ".join(aliases),
                            " ".join(column_parts),
                        ]
                        if part
                    )
                )
                vector_texts.append(
                    "\n".join(
                        part
                        for part in [
                            f"表名：{table_name}",
                            f"所属数据库：{name}",
                            f"表含义：{description}" if description else "",
                            f"表别名：{', '.join(aliases)}" if aliases else "",
                            f"包含字段：{', '.join(column_parts)}" if column_parts else "",
                        ]
                        if part
                    )
                )
                self.table_refs.append((name, table_name))

        self.table_keyword_index = BM25Index(keyword_texts)
        self.table_vector_index = create_vector_index(
            self.embedding_client, namespace="tier_tables"
        )
        self.table_vector_index.build(vector_texts)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        keywords: Optional[Sequence[str]] = None,
        access: Optional[SchemaAccessFilter] = None,
    ) -> ThreeTierRetrievalResult:
        """
        执行三层检索。

        access 传入数据权限时，无权的库/表/字段在每一层都会被剔除，
        最终不会出现在 SchemaGraph 里——模型看不到，也就写不出来。
        """
        resolved_keywords = self._resolve_keywords(query, keywords)
        access = access or ALLOW_ALL
        self._prefetch_vectors(resolved_keywords)

        # 第一层：库级
        database_hits = self._select_databases(resolved_keywords)

        candidate_hits = database_hits
        if self.config.database_keyword_guard:
            keyword_confirmed = [
                hit
                for hit in database_hits
                if self._has_strong_keyword_evidence(
                    resolved_keywords,
                    self.db_keyword_texts[self.database_names.index(hit.name)],
                )
            ]
            if keyword_confirmed:
                candidate_hits = keyword_confirmed

        selected_databases = [
            hit.name
            for hit in candidate_hits[: self.config.max_databases]
            if self._database_visible(hit.name, access)
        ]
        if not selected_databases:
            selected_databases = [
                name
                for name in self.sources
                if self._database_visible(name, access)
            ]

        # 第二层：表级
        table_hits = self._select_tables(resolved_keywords, selected_databases)
        table_hits = [
            hit
            for hit in table_hits
            if self._allows_table(access, *hit.name.split(".", 1))
        ]
        selected_tables: Dict[str, List[str]] = {}
        for hit in table_hits[: self.config.max_tables]:
            database, table_name = hit.name.split(".", 1)
            selected_tables.setdefault(database, []).append(table_name)

        # 表级没有任何命中时回退到候选库中有权限的全部表
        if not selected_tables:
            for database in selected_databases:
                visible = [
                    table_name
                    for table_name in self.column_services[database].tables
                    if self._allows_table(access, database, table_name)
                ]
                if visible:
                    selected_tables[database] = visible

        # 第三层：字段级（在候选表内检索 + Rerank）
        column_results: Dict[str, HybridSchemaRetrievalResult] = {}
        schema_graphs: Dict[str, object] = {}

        for database, tables in selected_tables.items():
            service = self.column_services[database]
            # tables 按表级检索的排名排好了；库内表的先后就用这个排名
            result = service.retrieve(
                query=query,
                keywords=resolved_keywords,
                allowed_tables=set(tables),
                table_order=tables,
            )

            # 收窄过度导致该库无命中时，回退为不限表的字段级检索
            if not getattr(result.schema_graph, "tables", None):
                result = service.retrieve(
                    query=query, keywords=resolved_keywords, table_order=tables
                )

            # 兜底：无论走哪条分支，进 Prompt 前都按权限再裁一次
            filter_schema_graph(result.schema_graph, self._access_for(access, database))

            column_results[database] = result
            schema_graphs[database] = result.schema_graph

        return ThreeTierRetrievalResult(
            query=query,
            keywords=list(resolved_keywords),
            database_hits=database_hits,
            selected_databases=selected_databases,
            table_hits=table_hits,
            selected_tables=selected_tables,
            column_results=column_results,
            schema_graphs=schema_graphs,
            cross_database_notes=self._build_cross_database_notes(
                [
                    database
                    for database, graph in schema_graphs.items()
                    if getattr(graph, "tables", None)
                ]
            ),
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _access_for(self, access: SchemaAccessFilter, database: str):
        """这份权限在这个库上的视图：名字的大小写按这个库的方言比。"""
        source = self.sources.get(database)
        return bind_dialect(access, getattr(source, "dialect", "") or "")

    def _allows_table(self, access: SchemaAccessFilter, database: str, table: str) -> bool:
        return self._access_for(access, database).allows_table(database, table)

    def _database_visible(self, database: str, access: SchemaAccessFilter) -> bool:
        """库内一张有权限的表都没有时，这个库对当前用户不存在。"""
        service = self.column_services.get(database)
        if service is None:
            return False
        return any(
            self._allows_table(access, database, table_name)
            for table_name in service.tables
        )

    def _resolve_keywords(
        self,
        query: str,
        keywords: Optional[Sequence[str]],
    ) -> List[str]:
        if keywords:
            return [item.strip() for item in keywords if item and item.strip()]

        if self.keyword_extractor is None:
            raise ValueError(
                "未传入 keywords，且未配置 keyword_extractor，无法自动抽取关键词。"
            )

        extracted = self.keyword_extractor.extract(query)
        if not extracted:
            raise ValueError(f"关键词抽取结果为空，query={query}")
        return extracted

    def _prefetch_vectors(self, keywords: Sequence[str]) -> None:
        """
        远端 Embedding：把这个问题的全部关键词一次算好。

        三层召回都按关键词逐个做向量检索，同一个关键词要用好几次；先合成一次请求，
        后面全从缓存里取。算不出来也不在这里报错——各层的向量召回会各自退成只走关键词。
        """
        prefetch = getattr(self.embedding_client, "prefetch", None)
        if not callable(prefetch):
            return
        try:
            prefetch(list(keywords))
        except ModelCallError as exc:
            # Client 进入冷却：这个问题的向量召回都会跳过，只走关键词召回
            log_event(
                _logger,
                logging.WARNING,
                "retrieval.embedding_unavailable",
                error_category=exc.category,
                keywords=len(keywords),
            )

    def _tier_fuse(
        self,
        keywords: Sequence[str],
        keyword_index: BM25Index,
        vector_index: VectorIndex,
        candidate_count: int,
    ):
        """对单层执行关键词 + 向量召回和 RRF 融合。"""
        route_results: List[RouteRecallResult] = []

        for keyword in keywords:
            keyword_hits = keyword_index.search(
                keyword, top_k=self.config.tier_per_keyword_top_k
            )
            route_results.append(
                RouteRecallResult(
                    route_name="keyword",
                    query_term=keyword,
                    ranked_doc_indices=[index for index, _ in keyword_hits],
                )
            )

            vector_hits = search_or_skip(
                vector_index, keyword, top_k=self.config.tier_per_keyword_top_k
            )
            route_results.append(
                RouteRecallResult(
                    route_name="vector",
                    query_term=keyword,
                    ranked_doc_indices=[index for index, _ in vector_hits],
                )
            )

        fusion_client = RRFFusionClient(
            RRFFusionConfig(
                rrf_k=self.config.tier_rrf_k,
                truncate_multiplier=6,
                min_fused_top_k=candidate_count,
                max_fused_top_k=max(candidate_count, 1),
                final_top_k=max(candidate_count, 1),
                route_weights={"keyword": 1.0, "vector": 1.0},
            )
        )
        return fusion_client.fuse(
            route_results=route_results,
            keyword_count=len(keywords),
        )

    def _select_databases(self, keywords: Sequence[str]) -> List[TierHit]:
        fusion_hits = self._tier_fuse(
            keywords,
            self.db_keyword_index,
            self.db_vector_index,
            candidate_count=len(self.database_names),
        )
        return [
            TierHit(
                name=self.database_names[hit.doc_index],
                score=hit.score,
                sources=list(getattr(hit, "sources", []) or []),
            )
            for hit in fusion_hits
        ]

    @staticmethod
    def _has_strong_keyword_evidence(
        keywords: Sequence[str],
        text_lower: str,
    ) -> bool:
        """
        判断是否存在 bigram 及以上长度的关键词字面命中。
        """
        import re

        for keyword in keywords:
            keyword = keyword.strip().lower()
            if not keyword:
                continue

            for token in re.findall(r"[a-z0-9_]+", keyword):
                if len(token) >= 2 and token in text_lower:
                    return True

            for span in re.findall(r"[一-鿿]+", keyword):
                if len(span) == 1:
                    continue
                for i in range(len(span) - 1):
                    if span[i:i + 2] in text_lower:
                        return True

        return False

    def _select_tables(
        self,
        keywords: Sequence[str],
        selected_databases: Sequence[str],
    ) -> List[TierHit]:
        fusion_hits = self._tier_fuse(
            keywords,
            self.table_keyword_index,
            self.table_vector_index,
            candidate_count=len(self.table_refs),
        )

        allowed = set(selected_databases)
        hits: List[TierHit] = []

        for hit in fusion_hits:
            database, table_name = self.table_refs[hit.doc_index]
            if database not in allowed:
                continue
            hits.append(
                TierHit(
                    name=f"{database}.{table_name}",
                    score=hit.score,
                    sources=list(getattr(hit, "sources", []) or []),
                )
            )

        return hits
