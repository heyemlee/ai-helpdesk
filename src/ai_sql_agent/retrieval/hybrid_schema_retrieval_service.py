from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ai_sql_agent.retrieval.bm25 import BM25Index
from ai_sql_agent.retrieval.embedding_client import (
    AliyunEmbeddingClient,
)
from ai_sql_agent.retrieval.graph_builder import build_schema_graph
from ai_sql_agent.retrieval.keyword_extractor_client import (
    AliyunKeywordExtractor,
)
from ai_sql_agent.retrieval.objects import SchemaHit
from ai_sql_agent.retrieval.rerank_client import (
    AliyunRerankClient,
    RerankDocument,
    RerankResult,
)
from ai_sql_agent.retrieval.rrf_fusion_client import (
    RRFFusionClient,
    RRFFusionConfig,
    RRFFusionHit,
    RouteRecallResult,
)
from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader
from ai_sql_agent.retrieval.vector_backend import create_vector_index, search_or_skip
from ai_sql_agent.retrieval.vector_index import VectorIndex


@dataclass
class HybridFieldDocument:
    """
    混合检索字段文档。

    一个字段会同时维护三类文本：
    - keyword_text：用于 BM25 关键词召回
    - vector_text：用于 Embedding 向量召回
    - rerank_text：用于 Rerank 精排
    """

    doc_id: str
    column: object
    keyword_text: str
    vector_text: str
    rerank_text: str


@dataclass
class HybridSchemaRetrievalConfig:
    """
    混合 Schema 检索服务配置。
    """

    per_keyword_top_k: int = 20
    """每个关键词在每一路召回中的 Top-K 数量。"""

    include_join_columns: bool = True

    guarantee_top_hit_per_keyword: bool = True
    """
    每个关键词保底 1 个字段进入 Schema 图。

    RRF 只用排名不用分数，会把决定性的词面信号抹平：实测「车间」在 BM25 里
    命中 work_order.workshop 得 17.71 分、第二名才 1.86 分，信号极强；但融合后
    它与同表无关字段（finish_date 等）几乎并列，随后被精排挤掉，导致用户明确
    提到的概念反而不在 Schema 图里。

    这个保底是对该局限的定向对冲：代价至多是「关键词个数」个额外字段。
    """

    min_columns_per_table: int = 0
    """
    每张选中表在 Schema 图中至少保留的字段数。

    字段级检索取全局 top-k，多表关联时容易一张表占满配额、另一张表
    只剩 join key，导致第二张表的输出/过滤字段进不了图。
    """
    """构建 SchemaGraph 时是否自动补充 Join 字段。"""

    rrf_config: RRFFusionConfig = field(
        default_factory=lambda: RRFFusionConfig(
            rrf_k=60,
            truncate_multiplier=6,
            min_fused_top_k=10,
            max_fused_top_k=50,
            final_top_k=20,
            route_weights={
                "keyword": 1.0,
                "vector": 1.0,
            },
        )
    )
    """RRF 融合配置。"""

    rerank_top_multiplier: int = 2
    """
    Rerank 输出数量倍数。

    计算方式：
        rerank_top_n = 关键词数量 * rerank_top_multiplier
    """

    rerank_min_top_n: int = 2
    """Rerank 输出数量下限。"""

    rerank_max_top_n: int = 20
    """Rerank 输出数量上限。"""


@dataclass
class HybridSchemaRetrievalResult:
    """
    混合 Schema 检索服务返回结果。
    """

    query: str
    keywords: List[str]
    route_results: List[RouteRecallResult]
    fusion_hits: List[RRFFusionHit]
    rerank_results: List[RerankResult]
    schema_hits: List[SchemaHit]
    schema_graph: object


class HybridSchemaRetrievalService:
    """
    混合 Schema 检索服务。

    完整流程：
    1. 用户 Query
    2. LLM 抽取关键词
    3. BM25 关键词召回
    4. Embedding 向量召回
    5. RRF 融合
    6. Rerank 精排
    7. SchemaHit
    8. SchemaGraph
    """

    def __init__(
        self,
        tables: dict,
        columns: list,
        relations: list,
        business_meta: Optional[dict],
        embedding_client: AliyunEmbeddingClient,
        rerank_client: AliyunRerankClient,
        keyword_extractor: Optional[AliyunKeywordExtractor] = None,
        config: Optional[HybridSchemaRetrievalConfig] = None,
    ):
        self.tables = tables
        self.columns = columns
        self.relations = relations
        self.business_meta = business_meta or {}
        self.embedding_client = embedding_client
        self.rerank_client = rerank_client
        self.keyword_extractor = keyword_extractor
        self.config = config or HybridSchemaRetrievalConfig()

        self.documents: List[HybridFieldDocument] = []
        self.keyword_index: Optional[BM25Index] = None
        self.vector_index: Optional[VectorIndex] = None
        self.rrf_client = RRFFusionClient(self.config.rrf_config)

        self._enrich_columns_with_business_meta()
        self._build_documents()
        self._build_indices()

    @classmethod
    def from_sqlite(
        cls,
        db_path,
        database_name: str,
        business_meta: Optional[dict],
        embedding_client: AliyunEmbeddingClient,
        rerank_client: AliyunRerankClient,
        keyword_extractor: Optional[AliyunKeywordExtractor] = None,
        config: Optional[HybridSchemaRetrievalConfig] = None,
        sample_size: int = 5,
    ) -> "HybridSchemaRetrievalService":
        """
        从 SQLite 数据库初始化混合 Schema 检索服务。

        Args:
            db_path: SQLite 数据库路径。
            database_name: 数据库名称。
            business_meta: 表级和字段级业务元数据。
            embedding_client: Embedding Client。
            rerank_client: Rerank Client。
            keyword_extractor: 关键词抽取 Client。不传时需要在 retrieve() 中手动传 keywords。
            config: 服务配置。
            sample_size: 字段样例值抽取数量。

        Returns:
            HybridSchemaRetrievalService: 初始化完成的服务实例。
        """
        loader = SQLiteSchemaLoader(
            db_path=db_path,
            database_name=database_name,
            business_meta=business_meta or {},
            sample_size=sample_size,
        )

        tables, columns, relations = loader.load()

        return cls(
            tables=tables,
            columns=columns,
            relations=relations,
            business_meta=business_meta,
            embedding_client=embedding_client,
            rerank_client=rerank_client,
            keyword_extractor=keyword_extractor,
            config=config,
        )

    @classmethod
    def from_postgres(
        cls,
        dsn: str,
        database_name: str,
        business_meta: Optional[dict],
        embedding_client: AliyunEmbeddingClient,
        rerank_client: AliyunRerankClient,
        keyword_extractor: Optional[AliyunKeywordExtractor] = None,
        config: Optional[HybridSchemaRetrievalConfig] = None,
        sample_size: int = 5,
        schema: str = "public",
    ) -> "HybridSchemaRetrievalService":
        """
        从 PostgreSQL 初始化混合 Schema 检索服务。

        与 from_sqlite 的区别只在 Schema 来源：这里读 information_schema
        与 pg_catalog，后续召回、融合、Rerank 完全共用同一套逻辑。
        """
        from .postgres_loader import PostgresSchemaLoader

        loader = PostgresSchemaLoader(
            dsn=dsn,
            database_name=database_name,
            business_meta=business_meta or {},
            sample_size=sample_size,
            schema=schema,
        )

        tables, columns, relations = loader.load()

        return cls(
            tables=tables,
            columns=columns,
            relations=relations,
            business_meta=business_meta,
            embedding_client=embedding_client,
            rerank_client=rerank_client,
            keyword_extractor=keyword_extractor,
            config=config,
        )

    def retrieve(
        self,
        query: str,
        keywords: Optional[Sequence[str]] = None,
        allowed_tables: Optional[set[str]] = None,
        table_order: Optional[Sequence[str]] = None,
    ) -> HybridSchemaRetrievalResult:
        """
        执行混合检索，并返回 SchemaGraph。

        Args:
            query: 用户自然语言 Query。
            keywords: 可选的核心关键词列表。
                如果传入，则直接使用该关键词列表。
                如果不传，则使用 keyword_extractor 自动抽取。
            allowed_tables: 可选的候选表集合。
                三层检索中由表级检索收窄得到；传入后只有这些表的字段
                会进入 Rerank 精排。
            table_order: 可选的表排名，决定 SchemaGraph 里表的先后。
                三层检索传表级检索的排名；不传时按字段命中的先后。

        Returns:
            HybridSchemaRetrievalResult: 包含关键词、RRF 结果、Rerank 结果和 SchemaGraph。
        """
        resolved_keywords = self._resolve_keywords(query, keywords)

        route_results = self._build_route_results(resolved_keywords)

        fusion_hits = self.rrf_client.fuse(
            route_results=route_results,
            keyword_count=len(resolved_keywords),
        )

        if allowed_tables is not None:
            fusion_hits = [
                hit
                for hit in fusion_hits
                if getattr(self.documents[hit.doc_index].column, "table_name", "")
                in allowed_tables
            ]

        rerank_results = self._rerank(
            query=query,
            fusion_hits=fusion_hits,
            keyword_count=len(resolved_keywords),
        )

        schema_hits = self._build_schema_hits(
            rerank_results=rerank_results,
            fusion_hits=fusion_hits,
        )

        if self.config.guarantee_top_hit_per_keyword:
            schema_hits = self._guarantee_keyword_hits(
                schema_hits=schema_hits,
                route_results=route_results,
                allowed_tables=allowed_tables,
            )

        schema_graph = build_schema_graph(
            hits=schema_hits,
            tables=self.tables,
            all_columns=self.columns,
            relations=self.relations,
            include_join_columns=self.config.include_join_columns,
            min_columns_per_table=self.config.min_columns_per_table,
            table_order=table_order,
        )
        return HybridSchemaRetrievalResult(
            query=query,
            keywords=resolved_keywords,
            route_results=route_results,
            fusion_hits=fusion_hits,
            rerank_results=rerank_results,
            schema_hits=schema_hits,
            schema_graph=schema_graph,
        )

    def _resolve_keywords(
        self,
        query: str,
        keywords: Optional[Sequence[str]],
    ) -> List[str]:
        """
        获取最终使用的核心关键词。
        """
        if keywords:
            return [item.strip() for item in keywords if item and item.strip()]

        if self.keyword_extractor is None:
            raise ValueError(
                "未传入 keywords，且未配置 keyword_extractor，无法自动抽取关键词。"
            )

        extracted_keywords = self.keyword_extractor.extract(query)

        if not extracted_keywords:
            raise ValueError(f"关键词抽取结果为空，query={query}")

        return extracted_keywords

    def _build_route_results(
        self,
        keywords: List[str],
    ) -> List[RouteRecallResult]:
        """
        执行 BM25 关键词召回和 Embedding 向量召回。
        """
        if self.keyword_index is None:
            raise RuntimeError("keyword_index 尚未初始化。")

        if self.vector_index is None:
            raise RuntimeError("vector_index 尚未初始化。")

        route_results: List[RouteRecallResult] = []

        for keyword in keywords:
            keyword_hits = self.keyword_index.search(
                keyword,
                top_k=self.config.per_keyword_top_k,
            )

            route_results.append(
                RouteRecallResult(
                    route_name="keyword",
                    query_term=keyword,
                    ranked_doc_indices=[
                        doc_index
                        for doc_index, _ in keyword_hits
                    ],
                )
            )

            vector_hits = search_or_skip(
                self.vector_index,
                keyword,
                top_k=self.config.per_keyword_top_k,
            )

            route_results.append(
                RouteRecallResult(
                    route_name="vector",
                    query_term=keyword,
                    ranked_doc_indices=[
                        doc_index
                        for doc_index, _ in vector_hits
                    ],
                )
            )

        return route_results

    def _rerank(
        self,
        query: str,
        fusion_hits: List[RRFFusionHit],
        keyword_count: int,
    ) -> List[RerankResult]:
        """
        对 RRF 融合结果进行 Rerank 精排。
        """
        rerank_candidates = [
            RerankDocument(
                doc_index=hit.doc_index,
                text=self.documents[hit.doc_index].rerank_text,
            )
            for hit in fusion_hits
        ]

        rerank_top_n = self._calculate_rerank_top_n(
            keyword_count=keyword_count,
            candidate_count=len(rerank_candidates),
        )

        return self.rerank_client.rerank(
            query=query,
            documents=rerank_candidates,
            top_n=rerank_top_n,
        )

    def _calculate_rerank_top_n(
        self,
        keyword_count: int,
        candidate_count: int,
    ) -> int:
        """
        根据关键词数量动态计算 Rerank 输出数量。
        """
        dynamic_top_n = keyword_count * self.config.rerank_top_multiplier

        top_n = max(
            self.config.rerank_min_top_n,
            min(dynamic_top_n, self.config.rerank_max_top_n),
        )

        return min(top_n, candidate_count)

    def _guarantee_keyword_hits(
        self,
        schema_hits: List[SchemaHit],
        route_results: List[RouteRecallResult],
        allowed_tables: Optional[set[str]],
    ) -> List[SchemaHit]:
        """
        为每个关键词补一个词面最佳命中，若它已被 RRF/精排淘汰。

        只取 keyword（BM25）路：向量路的 top-1 往往与 RRF 结果重合，
        真正会被抹平的是词面强信号。

        选哪一个命中有讲究：**优先取所属表已在图中的那个**，而不是无条件取
        全局 top-1。同名字段常跨表重复（`workshop` 同时存在于 work_order 与
        capacity_stat），全局 top-1 可能落在一张与本次查询无关、只因这个关键词
        才被拉进来的表上。已在图中的表说明查询确实围绕它展开，加固它更合理。
        """
        present_docs = {hit.doc_id for hit in schema_hits}
        present_tables = {
            getattr(hit.column, "table_name", "") for hit in schema_hits
        }
        guaranteed: List[SchemaHit] = []

        for route in route_results:
            if route.route_name != "keyword" or not route.ranked_doc_indices:
                continue

            candidates = []
            for doc_index in route.ranked_doc_indices:
                doc = self.documents[doc_index]
                table_name = getattr(doc.column, "table_name", "")
                if allowed_tables is not None and table_name not in allowed_tables:
                    continue
                candidates.append((doc, table_name))

            if not candidates:
                continue

            # 优先取「表已在图中且该字段尚未进图」的命中；
            # 没有这样的候选时退回第一个尚未进图的命中。
            chosen = next(
                (
                    doc
                    for doc, table in candidates
                    if table in present_tables and doc.doc_id not in present_docs
                ),
                None,
            )
            if chosen is None:
                chosen = next(
                    (doc for doc, _ in candidates if doc.doc_id not in present_docs),
                    None,
                )
            if chosen is None:
                continue

            present_docs.add(chosen.doc_id)
            guaranteed.append(
                SchemaHit(doc_id=chosen.doc_id, score=0.0, column=chosen.column)
            )

        return schema_hits + guaranteed

    def _build_schema_hits(
        self,
        rerank_results: List[RerankResult],
        fusion_hits: List[RRFFusionHit],
    ) -> List[SchemaHit]:
        """
        将 Rerank 结果转换为 SchemaHit。
        """
        fusion_hit_map = {
            hit.doc_index: hit
            for hit in fusion_hits
        }

        schema_hits: List[SchemaHit] = []

        for result in rerank_results:
            doc = self.documents[result.doc_index]
            fusion_hit = fusion_hit_map.get(result.doc_index)

            best_rank_by_source = (
                fusion_hit.best_rank_by_source
                if fusion_hit is not None
                else {}
            )

            try:
                schema_hit = SchemaHit(
                    doc_id=doc.doc_id,
                    score=result.rerank_score,
                    column=doc.column,
                    keyword_rank=best_rank_by_source.get("keyword"),
                    vector_rank=best_rank_by_source.get("vector"),
                    rrf_score=fusion_hit.score if fusion_hit is not None else 0.0,
                    rerank_score=result.rerank_score,
                )
            except TypeError:
                # 兼容旧版 SchemaHit 结构。
                schema_hit = SchemaHit(
                    doc_id=doc.doc_id,
                    score=result.rerank_score,
                    column=doc.column,
                )

            schema_hits.append(schema_hit)

        return schema_hits

    def _build_indices(self) -> None:
        """
        构建 BM25 索引和向量索引。
        """
        keyword_texts = [
            doc.keyword_text
            for doc in self.documents
        ]

        self.keyword_index = BM25Index(keyword_texts)

        vector_texts = [
            doc.vector_text
            for doc in self.documents
        ]

        database_name = ""
        if self.columns:
            database_name = getattr(self.columns[0], "database", "") or ""

        self.vector_index = create_vector_index(
            self.embedding_client,
            namespace=f"columns_{database_name or 'default'}",
        )
        self.vector_index.build(vector_texts)

    def _build_documents(self) -> None:
        """
        构建混合检索字段文档。
        """
        self.documents = []

        for column in self.columns:
            table_meta = self.business_meta.get(column.table_name, {})
            column_meta = table_meta.get("columns", {}).get(column.column_name, {})

            # 自定义索引文本与自动构建文本**合并**，而不是覆盖。
            #
            # 覆盖会让手写过元数据的字段反而更难被检索到：自动文本里的库名、
            # 表名、字段名、类型、样例值全部丢失，只剩手写的那几个词，
            # BM25 少了大量可匹配 token。实测中 po_amt（手写）召不回、
            # 同表 po_date（自动）却能召回，就是这个原因。
            keyword_text = self._merge_index_text(
                column_meta.get("keyword_text")
                or self._get_index_text(column, "keyword_text"),
                self._build_default_keyword_text(column),
            )

            vector_text = self._merge_index_text(
                column_meta.get("vector_text")
                or self._get_index_text(column, "vector_text"),
                self._build_default_vector_text(column),
            )

            rerank_text = self._merge_index_text(
                column_meta.get("rerank_text")
                or self._get_index_text(column, "rerank_text"),
                self._build_default_rerank_text(column),
            )

            self.documents.append(
                HybridFieldDocument(
                    doc_id=column.full_name,
                    column=column,
                    keyword_text=keyword_text,
                    vector_text=vector_text,
                    rerank_text=rerank_text,
                )
            )

    @staticmethod
    def _merge_index_text(custom: str, base: str) -> str:
        """
        合并自定义索引文本与自动构建文本。

        自定义文本放前面（业务口语别名优先命中），自动文本补齐结构信息。
        没有自定义文本时直接返回自动文本。
        """
        custom = (custom or "").strip()
        base = (base or "").strip()
        if not custom:
            return base
        if not base:
            return custom
        return f"{custom}\n{base}"

    def _get_index_text(
        self,
        column: object,
        attr_name: str,
    ) -> str:
        """
        从 ColumnSchema.index_texts 中读取索引文本。
        """
        index_texts = getattr(column, "index_texts", None)

        if index_texts is None:
            return ""

        return getattr(index_texts, attr_name, "") or ""

    def _enrich_columns_with_business_meta(self) -> None:
        """
        将业务元数据补充回 ColumnSchema。
        """
        for column in self.columns:
            table_meta = self.business_meta.get(column.table_name, {})
            column_meta = table_meta.get("columns", {}).get(column.column_name, {})

            if table_meta:
                column.table_description = table_meta.get(
                    "description",
                    getattr(column, "table_description", ""),
                )
                column.table_aliases = table_meta.get(
                    "aliases",
                    getattr(column, "table_aliases", []),
                )

            if not column_meta:
                continue

            column.description = column_meta.get(
                "description",
                getattr(column, "description", ""),
            )
            column.aliases = column_meta.get(
                "aliases",
                getattr(column, "aliases", []),
            )
            column.semantic_role = column_meta.get(
                "semantic_role",
                getattr(column, "semantic_role", ""),
            )
            column.value_range = column_meta.get(
                "value_range",
                getattr(column, "value_range", ""),
            )
            column.data_distribution = column_meta.get(
                "data_distribution",
                getattr(column, "data_distribution", ""),
            )
            column.business_usage = column_meta.get(
                "business_usage",
                getattr(column, "business_usage", ""),
            )

            if "samples" in column_meta:
                column.samples = [
                    str(item)
                    for item in column_meta["samples"]
                ]

    def _build_default_keyword_text(
        self,
        column: object,
    ) -> str:
        """
        构建默认关键词索引文本。
        """
        parts = [
            getattr(column, "database", ""),
            getattr(column, "table_name", ""),
            getattr(column, "column_name", ""),
            getattr(column, "data_type", ""),
            getattr(column, "description", ""),
            " ".join(getattr(column, "aliases", []) or []),
            " ".join(getattr(column, "samples", [])[:5] or []),
        ]

        return "\n".join(part for part in parts if part)

    def _build_default_vector_text(
        self,
        column: object,
    ) -> str:
        """
        构建默认向量索引文本。
        """
        parts = [
            f"字段名：{getattr(column, 'column_name', '')}",
            f"所属表：{getattr(column, 'table_name', '')}",
            f"所属表业务含义：{getattr(column, 'table_description', '')}",
            f"字段含义：{getattr(column, 'description', '')}",
        ]

        aliases = getattr(column, "aliases", []) or []
        if aliases:
            parts.append(f"字段别名：{', '.join(aliases)}")

        business_usage = getattr(column, "business_usage", "")
        if business_usage:
            parts.append(f"业务用途：{business_usage}")

        semantic_role = getattr(column, "semantic_role", "")
        if semantic_role:
            parts.append(f"字段角色：{semantic_role}")

        return "\n".join(part for part in parts if part)

    def _build_default_rerank_text(
        self,
        column: object,
    ) -> str:
        """
        构建默认 Rerank 文本。
        """
        parts = [
            f"字段：{getattr(column, 'table_name', '')}.{getattr(column, 'column_name', '')}",
            f"数据类型：{getattr(column, 'data_type', '')}",
            f"所属表业务含义：{getattr(column, 'table_description', '')}",
            f"字段含义：{getattr(column, 'description', '')}",
        ]

        aliases = getattr(column, "aliases", []) or []
        if aliases:
            parts.append(f"字段别名：{', '.join(aliases)}")

        semantic_role = getattr(column, "semantic_role", "")
        if semantic_role:
            parts.append(f"字段角色：{semantic_role}")

        value_range = getattr(column, "value_range", "")
        if value_range:
            parts.append(f"取值范围：{value_range}")

        data_distribution = getattr(column, "data_distribution", "")
        if data_distribution:
            parts.append(f"数据分布：{data_distribution}")

        business_usage = getattr(column, "business_usage", "")
        if business_usage:
            parts.append(f"业务用途：{business_usage}")

        samples = getattr(column, "samples", []) or []
        if samples:
            parts.append(f"样例值：{', '.join(samples[:8])}")

        return "\n".join(part for part in parts if part)
