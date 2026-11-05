from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

from ai_sql_agent.retrieval.bm25 import BM25Index
from ai_sql_agent.retrieval.graph_builder import build_schema_graph
from ai_sql_agent.retrieval.objects import SchemaHit
from ai_sql_agent.retrieval.rrf_fusion_client import (
    RRFFusionClient,
    RRFFusionConfig,
    RouteRecallResult,
)
from ai_sql_agent.retrieval.rerank_client import (
    AliyunRerankClient,
    AliyunRerankConfig,
    RerankDocument,
)
from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader
# 同目录示例模块：以 `python examples/<name>.py` 运行时可直接 import
from vector_recall import (
    AliyunEmbeddingClient,
    AliyunEmbeddingConfig,
    KeywordExtractor,
    KeywordExtractorConfig,
    VectorIndex,
)


@dataclass
class HybridFieldDocument:
    """
    混合检索字段文档。

    keyword_text 用于 BM25。
    vector_text 用于 Embedding。
    rerank_text 用于 Rerank。
    """

    doc_id: str
    column: object
    keyword_text: str
    vector_text: str
    rerank_text: str


def create_demo_database() -> Path:
    """
    创建 Demo 数据库：ERP 订单与采购域的两张核心表。
    - supplier: 供应商（含准时交付率）
    - purchase_order: 采购单（po_amt 为 ERP 缩写命名，字面无业务语义）
    """
    db_path = Path(tempfile.gettempdir()) / "text2sql_hybrid_rerank_schema_demo.db"

    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        '''
        CREATE TABLE supplier (
            supplier_id   TEXT PRIMARY KEY,
            supplier_name TEXT NOT NULL,
            category      TEXT NOT NULL,
            on_time_rate  REAL NOT NULL
        );

        CREATE TABLE purchase_order (
            po_no       TEXT PRIMARY KEY,
            job_no      TEXT NOT NULL,
            supplier_id TEXT NOT NULL,
            po_amt      REAL NOT NULL,
            po_date     TEXT NOT NULL,
            po_status   TEXT NOT NULL,
            FOREIGN KEY (supplier_id) REFERENCES supplier(supplier_id)
        );
        '''
    )
    conn.executemany(
        "INSERT INTO supplier VALUES (?, ?, ?, ?)",
        [
            ("SUP-01", "华鹏板材", "板材", 0.95),
            ("SUP-02", "东顺五金", "五金", 0.88),
            ("SUP-03", "杰森石英石", "台面", 0.72),
            ("SUP-04", "立信涂料", "涂料", 0.91),
            ("SUP-05", "广辉玻璃", "玻璃", 0.65),
        ],
    )
    conn.executemany(
        "INSERT INTO purchase_order VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("PO-24-0101", "J24001", "SUP-01", 386000.00, "2024-01-25", "received"),
            ("PO-24-0102", "J24001", "SUP-02", 92000.00, "2024-01-26", "received"),
            ("PO-24-0103", "J24001", "SUP-03", 158000.00, "2024-02-02", "received"),
            ("PO-24-0301", "J24003", "SUP-01", 742000.00, "2024-04-01", "partial"),
            ("PO-24-0303", "J24003", "SUP-05", 88000.00, "2024-04-10", "ordered"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path

def get_business_meta() -> dict:
    """
    Demo 业务元数据。

    三类文本：keyword_text 关键词召回 / vector_text 向量召回 / rerank_text 精排。
    注意 po_amt 这类 ERP 缩写命名：字面无业务语义，向量检索对它表现差，
    必须靠 keyword_text 补业务口语别名让 BM25 那一路命中。
    """
    return {
        "supplier": {
            "description": "供应商表，记录供应商名称、供货品类与准时交付率。",
            "aliases": ["供应商表", "供方表"],
            "columns": {
                "supplier_id": {
                    "description": "供应商编号。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "用于表关联。",
                    "keyword_text": "supplier_id 供应商ID 供应商编号 关联字段",
                    "vector_text": "字段名：supplier_id。所属表：supplier。该字段是供应商唯一编号，用于关联采购单表。",
                    "rerank_text": "字段：supplier.supplier_id。含义：供应商编号。角色：join_key。",
                },
                "supplier_name": {
                    "description": "供应商名称。",
                    "aliases": ["供应商", "供应商名称", "供方名称", "厂家"],
                    "semantic_role": "output_dimension",
                    "samples": ["华鹏板材", "东顺五金", "杰森石英石"],
                    "keyword_text": "supplier_name 供应商 供应商名称 供方 厂家",
                    "vector_text": "字段名：supplier_name。所属表：supplier。该字段表示供应商名称，常作为查询结果输出。",
                    "rerank_text": "字段：supplier.supplier_name。含义：供应商名称。用途：询问是哪家供应商时作为输出字段。",
                },
                "category": {
                    "description": "供货品类。",
                    "aliases": ["品类", "供货品类", "材料类别"],
                    "semantic_role": "dimension",
                    "value_range": "板材 / 五金 / 台面 / 涂料 / 玻璃",
                    "samples": ["板材", "五金", "台面", "涂料", "玻璃"],
                    "business_usage": "按品类筛选供应商。",
                    "keyword_text": "category 品类 供货品类 材料类别 板材 五金 台面 涂料 玻璃",
                    "vector_text": "字段名：category。所属表：supplier。该字段表示供应商的供货品类。",
                    "rerank_text": "字段：supplier.category。含义：供货品类。取值：板材、五金、台面、涂料、玻璃。",
                },
                "on_time_rate": {
                    "description": "准时交付率，0-1 之间。",
                    "aliases": ["准时率", "准时交付率", "及时率", "交付准时率"],
                    "semantic_role": "metric_filter",
                    "value_range": "0-1",
                    "data_distribution": "多数供应商在 0.85 以上，长尾供应商可能低于 0.7。",
                    "business_usage": "评估供应商交付表现，筛选准时率低的供应商。",
                    "samples": ["0.65", "0.72", "0.88", "0.95"],
                    "keyword_text": "on_time_rate 准时率 准时交付率 及时率 交付准时率 低于 供应商表现",
                    "vector_text": "字段名：on_time_rate。所属表：supplier。该字段表示供应商的准时交付率，用于筛选交付表现差的供应商。",
                    "rerank_text": "字段：supplier.on_time_rate。含义：准时交付率（0-1）。别名：准时率、及时率。用途：筛选准时率低于阈值的供应商。",
                    "index_texts": {
                        "vector_text": "字段名：on_time_rate。所属表：supplier。该字段表示供应商的准时交付率。"
                    },
                },
            },
        },
        "purchase_order": {
            "description": "采购单表，记录向供应商下达的采购单与含税采购金额。",
            "aliases": ["采购单表", "采购订单表", "PO表"],
            "columns": {
                "po_no": {
                    "description": "采购单号。",
                    "aliases": ["采购单号", "PO号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识采购单。",
                },
                "job_no": {
                    "description": "工程号，ERP 内部编码。",
                    "aliases": ["工程号", "项目号"],
                    "semantic_role": "join_key",
                    "business_usage": "按工程归集采购。",
                    "keyword_text": "job_no 工程号 项目号 ERP编码",
                    "vector_text": "字段名：job_no。所属表：purchase_order。该字段是采购单归属的工程编号。",
                },
                "supplier_id": {
                    "description": "供应商编号。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联供应商表。",
                },
                "po_amt": {
                    "description": "采购金额，含税。ERP 缩写命名，字面无业务语义。",
                    "aliases": ["采购金额", "采购额", "进货金额", "采购成本"],
                    "semantic_role": "metric_filter",
                    "value_range": "> 0",
                    "business_usage": "统计采购支出、按工程归集材料成本。",
                    "samples": ["88000.00", "158000.00", "742000.00"],
                    "keyword_text": "po_amt 采购金额 采购额 进货金额 采购成本 含税 采购单表 PO表",
                    "vector_text": "字段名：po_amt。所属表：purchase_order。该字段表示含税的采购金额，用于统计采购支出。",
                    "rerank_text": "字段：purchase_order.po_amt。含义：含税采购金额（ERP 缩写码）。别名：采购金额、采购额、采购成本。",
                    "index_texts": {
                        "vector_text": "字段名：po_amt。所属表：purchase_order。该字段表示含税的采购金额。"
                    },
                },
                "po_date": {
                    "description": "采购下单日期。",
                    "aliases": ["采购日期", "下单日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间统计采购。",
                },
                "po_status": {
                    "description": "采购单状态。",
                    "aliases": ["采购状态", "到货状态"],
                    "semantic_role": "filter",
                    "value_range": "ordered / partial / received",
                    "business_usage": "筛选未到货或已到货的采购单。",
                },
            },
        },
    }

def enrich_columns_with_business_meta(columns: list, business_meta: dict) -> None:
    """
    将业务元数据补充回 ColumnSchema。
    """
    for column in columns:
        table_meta = business_meta.get(column.table_name, {})
        column_meta = table_meta.get("columns", {}).get(column.column_name, {})

        if not column_meta:
            continue

        column.description = column_meta.get("description", getattr(column, "description", ""))
        column.aliases = column_meta.get("aliases", getattr(column, "aliases", []))
        column.semantic_role = column_meta.get("semantic_role", getattr(column, "semantic_role", ""))
        column.value_range = column_meta.get("value_range", getattr(column, "value_range", ""))
        column.data_distribution = column_meta.get(
            "data_distribution",
            getattr(column, "data_distribution", ""),
        )
        column.business_usage = column_meta.get(
            "business_usage",
            getattr(column, "business_usage", ""),
        )

        if "samples" in column_meta:
            column.samples = [str(item) for item in column_meta["samples"]]


def build_hybrid_documents(columns: list, business_meta: dict) -> List[HybridFieldDocument]:
    """
    构建混合检索字段文档。
    """
    documents: List[HybridFieldDocument] = []

    for column in columns:
        table_meta = business_meta.get(column.table_name, {})
        column_meta = table_meta.get("columns", {}).get(column.column_name, {})

        keyword_text = column_meta.get("keyword_text") or build_default_keyword_text(column)
        vector_text = column_meta.get("vector_text") or build_default_vector_text(column)
        rerank_text = column_meta.get("rerank_text") or build_default_rerank_text(column)

        documents.append(
            HybridFieldDocument(
                doc_id=column.full_name,
                column=column,
                keyword_text=keyword_text,
                vector_text=vector_text,
                rerank_text=rerank_text,
            )
        )

    return documents


def build_default_keyword_text(column: object) -> str:
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


def build_default_vector_text(column: object) -> str:
    """
    构建默认向量索引文本。
    """
    parts = [
        f"字段名：{getattr(column, 'column_name', '')}",
        f"所属表：{getattr(column, 'table_name', '')}",
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


def build_default_rerank_text(column: object) -> str:
    """
    构建默认 Rerank 文本。

    Rerank 文本可以比 keyword_text 和 vector_text 更详细。
    """
    parts = [
        f"字段：{getattr(column, 'table_name', '')}.{getattr(column, 'column_name', '')}",
        f"数据类型：{getattr(column, 'data_type', '')}",
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


def build_route_results(
    keywords: List[str],
    keyword_index: BM25Index,
    vector_index: VectorIndex,
    per_keyword_top_k: int,
) -> List[RouteRecallResult]:
    """
    执行关键词召回和向量召回。
    """
    route_results: List[RouteRecallResult] = []

    for keyword in keywords:
        keyword_hits = keyword_index.search(
            keyword,
            top_k=per_keyword_top_k,
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

        vector_hits = vector_index.search(
            keyword,
            top_k=per_keyword_top_k,
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


def calculate_rerank_top_n(
    keyword_count: int,
    multiplier: int,
    min_top_n: int,
    max_top_n: int,
    candidate_count: int,
) -> int:
    """
    根据关键词数量动态计算 Rerank 输出数量。

    rerank_top_n = keyword_count * multiplier
    """
    dynamic_top_n = keyword_count * multiplier

    top_n = max(
        min_top_n,
        min(dynamic_top_n, max_top_n),
    )

    return min(top_n, candidate_count)


def main() -> None:
    db_path = create_demo_database()
    business_meta = get_business_meta()

    print("=" * 80)
    print("混合检索 + RRF 融合 + Rerank + Schema 图 Demo")
    print("=" * 80)
    print(f"Demo database: {db_path}")

    loader = SQLiteSchemaLoader(
        db_path=db_path,
        database_name="demo_erp",
        business_meta=business_meta,
        sample_size=5,
    )

    tables, columns, relations = loader.load()
    enrich_columns_with_business_meta(columns, business_meta)

    documents = build_hybrid_documents(
        columns=columns,
        business_meta=business_meta,
    )

    print("\n[1] Hybrid documents:")
    for index, doc in enumerate(documents):
        print(f"{index}. {doc.doc_id}")
        print(f"   keyword_text: {doc.keyword_text}")
        print(f"   vector_text : {doc.vector_text}")
        print(f"   rerank_text : {doc.rerank_text}")

    # -------------------------------------------------------------------------
    # 构建关键词索引
    # -------------------------------------------------------------------------
    keyword_texts = [
        doc.keyword_text
        for doc in documents
    ]

    keyword_index = BM25Index(keyword_texts)

    # -------------------------------------------------------------------------
    # 构建向量索引
    # -------------------------------------------------------------------------
    embedding_client = AliyunEmbeddingClient(
        AliyunEmbeddingConfig(
            api_key="",
            embedding_url="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            model="text-embedding-v4",
            dimensions=1024,
        )
    )

    vector_index = VectorIndex(embedding_client)

    vector_texts = [
        doc.vector_text
        for doc in documents
    ]

    print("\n[2] Building vector index...")
    vector_index.build(vector_texts)

    if embedding_client.config.api_key:
        print("Vector index built by Aliyun Embedding API.")
    else:
        print("Vector index built by local mock embedding because DASHSCOPE_API_KEY is not set.")

    # -------------------------------------------------------------------------
    # LLM 抽取关键词
    # -------------------------------------------------------------------------
    query = "查询准时率低于0.9的供应商名称"

    print("\n[3] Query:")
    print(query)

    keyword_extractor = KeywordExtractor(
        KeywordExtractorConfig(
            api_key="",
            chat_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            model="qwen-plus",
        )
    )

    keywords = keyword_extractor.extract(query)

    print("\n[4] Extracted keywords:")
    print("，".join(keywords))

    # -------------------------------------------------------------------------
    # 可调参数位置
    # -------------------------------------------------------------------------
    per_keyword_top_k = 20

    rrf_client = RRFFusionClient(
        RRFFusionConfig(
            rrf_k=60,

            # RRF 候选截断：
            # fused_top_k = 关键词数量 * truncate_multiplier
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

    rerank_top_multiplier = 2
    """
    Rerank 输出数量倍数。

    这里就是你说的：
        rerank_top_k = 关键词数量 * 2

    可改为 3 或 4。
    """

    rerank_min_top_n = 2
    rerank_max_top_n = 20

    print("\n[5] Config:")
    print(f"per_keyword_top_k = {per_keyword_top_k}")
    print(f"rrf_truncate_multiplier = {rrf_client.config.truncate_multiplier}")
    print(f"rrf_fused_top_k = {rrf_client.calculate_fused_top_k(len(keywords))}")
    print(f"rrf_final_top_k = {rrf_client.config.final_top_k}")
    print(f"rerank_top_multiplier = {rerank_top_multiplier}")
    print(f"rerank_min_top_n = {rerank_min_top_n}")
    print(f"rerank_max_top_n = {rerank_max_top_n}")

    # -------------------------------------------------------------------------
    # 关键词召回 + 向量召回
    # -------------------------------------------------------------------------
    route_results = build_route_results(
        keywords=keywords,
        keyword_index=keyword_index,
        vector_index=vector_index,
        per_keyword_top_k=per_keyword_top_k,
    )

    print("\n[6] Route recall results:")
    for route_result in route_results:
        print(
            f"route={route_result.route_name}, "
            f"term={route_result.query_term}, "
            f"ranked_doc_indices={route_result.ranked_doc_indices}"
        )

    # -------------------------------------------------------------------------
    # RRF 融合
    # -------------------------------------------------------------------------
    fusion_hits = rrf_client.fuse(
        route_results=route_results,
        keyword_count=len(keywords),
    )

    print("\n[7] RRF fused results:")
    for rank, hit in enumerate(fusion_hits, start=1):
        doc = documents[hit.doc_index]
        col = doc.column

        print(
            f"{rank}. {col.table_name}.{col.column_name} "
            f"rrf_score={hit.score:.6f} "
            f"sources={hit.sources} "
            f"matched_terms={hit.matched_terms} "
            f"best_rank_by_source={hit.best_rank_by_source}"
        )

    # -------------------------------------------------------------------------
    # Rerank
    # -------------------------------------------------------------------------
    rerank_candidates = [
        RerankDocument(
            doc_index=hit.doc_index,
            text=documents[hit.doc_index].rerank_text,
        )
        for hit in fusion_hits
    ]

    rerank_top_n = calculate_rerank_top_n(
        keyword_count=len(keywords),
        multiplier=rerank_top_multiplier,
        min_top_n=rerank_min_top_n,
        max_top_n=rerank_max_top_n,
        candidate_count=len(rerank_candidates),
    )

    rerank_client = AliyunRerankClient(
        AliyunRerankConfig(
            api_key="",

            # qwen3-rerank 需要业务空间 ID。
            # 建议通过环境变量设置：
            # export DASHSCOPE_WORKSPACE_ID="你的WorkspaceId"
            workspace_id="",

            # 如果你想直接写完整 URL，也可以设置 rerank_url。
            rerank_url="",

            # 官方当前模型名为 qwen3-rerank。
            # 如果你账号里有 qwen-rerank 别名，可以改成 "qwen-rerank"。
            model="qwen3-rerank",
        )
    )

    rerank_results = rerank_client.rerank(
        query=query,
        documents=rerank_candidates,
        top_n=rerank_top_n,
    )

    print("\n[8] Rerank results:")
    for rank, result in enumerate(rerank_results, start=1):
        doc = documents[result.doc_index]
        col = doc.column

        print(
            f"{rank}. {col.table_name}.{col.column_name} "
            f"rerank_score={result.rerank_score:.6f}"
        )
        print(f"   rerank_text: {doc.rerank_text}")

    # -------------------------------------------------------------------------
    # Rerank 结果转 SchemaHit
    # -------------------------------------------------------------------------
    schema_hits: List[SchemaHit] = []

    for result in rerank_results:
        doc = documents[result.doc_index]

        schema_hits.append(
            SchemaHit(
                doc_id=doc.doc_id,
                score=result.rerank_score,
                column=doc.column,
                rerank_score=result.rerank_score,
            )
        )

    # -------------------------------------------------------------------------
    # 构建 Schema 图
    # -------------------------------------------------------------------------
    schema_graph = build_schema_graph(
        tables=tables,
        all_columns=columns,
        relations=relations,
        hits=schema_hits,
        include_join_columns=True,
    )

    print("\n[9] Schema graph prompt context:")
    print(schema_graph.to_prompt_context())


if __name__ == "__main__":
    main()