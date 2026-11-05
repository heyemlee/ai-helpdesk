"""
字段级混合检索服务 Demo：BM25 + 向量召回 → RRF 融合 → 交叉编码精排 → SchemaGraph。

复用 hybrid_rerank_schema.py 里的演示库与业务元数据。

    python examples/hybrid_schema_retrieval.py

需要 Embedding / Rerank 凭证（DASHSCOPE_API_KEY 等），没有凭证无法运行。
"""
from __future__ import annotations

import os

from ai_sql_agent.retrieval.embedding_client import AliyunEmbeddingClient, AliyunEmbeddingConfig
from ai_sql_agent.retrieval.hybrid_schema_retrieval_service import (
    HybridSchemaRetrievalConfig,
    HybridSchemaRetrievalService,
)
from ai_sql_agent.retrieval.keyword_extractor_client import (
    AliyunKeywordExtractor,
    AliyunKeywordExtractorConfig,
)
from ai_sql_agent.retrieval.rerank_client import AliyunRerankClient, AliyunRerankConfig
from ai_sql_agent.retrieval.rrf_fusion_client import RRFFusionConfig

# 同目录示例模块：以 `python examples/hybrid_schema_retrieval.py` 运行时可直接 import
from hybrid_rerank_schema import create_demo_database, get_business_meta  # noqa: E402


def main() -> None:
    db_path = create_demo_database()
    business_meta = get_business_meta()

    embedding_client = AliyunEmbeddingClient(
        AliyunEmbeddingConfig(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            embedding_url="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            model="text-embedding-v4",
            dimensions=1024,
        )
    )

    # 如果你想使用大模型自动抽取关键词，就配置 keyword_extractor。
    # 如果 retrieve() 中直接传 keywords，则可以不配置 keyword_extractor。
    keyword_extractor = None

    if os.getenv("DASHSCOPE_API_KEY"):
        keyword_extractor = AliyunKeywordExtractor(
            AliyunKeywordExtractorConfig(
                api_key=os.getenv("DASHSCOPE_API_KEY", ""),
                model="qwen-plus",
            )
        )

    rerank_client = AliyunRerankClient(
        AliyunRerankConfig(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            workspace_id=os.getenv("DASHSCOPE_WORKSPACE_ID", ""),

            # 这里按你的要求默认使用 qwen-rerank。
            # 如果你的环境使用 qwen3-rerank，可以改为：
            # model="qwen3-rerank"
            model=os.getenv("DASHSCOPE_RERANK_MODEL", "qwen-rerank"),
        )
    )

    service = HybridSchemaRetrievalService.from_sqlite(
        db_path=db_path,
        database_name="demo_finance",
        business_meta=business_meta,
        embedding_client=embedding_client,
        rerank_client=rerank_client,
        keyword_extractor=keyword_extractor,
        config=HybridSchemaRetrievalConfig(
            per_keyword_top_k=20,
            include_join_columns=True,
            rrf_config=RRFFusionConfig(
                rrf_k=60,
                truncate_multiplier=6,
                min_fused_top_k=10,
                max_fused_top_k=50,
                final_top_k=20,
                route_weights={
                    "keyword": 1.0,
                    "vector": 1.0,
                },
            ),
            rerank_top_multiplier=2,
            rerank_min_top_n=2,
            rerank_max_top_n=20,
        ),
        sample_size=5,
    )

    query = "查询准时率低于0.9的供应商名称"

    # 示例 1：手动传关键词。
    # 如果你想强制使用大模型抽取关键词，把 keywords 参数删掉即可。
    result = service.retrieve(
        query=query,
        keywords=["准时率", "供应商名称"],
    )

    print("=" * 80)
    print("Hybrid Schema Retrieval Service Demo")
    print("=" * 80)

    print("\n[1] Query:")
    print(result.query)

    print("\n[2] Keywords:")
    print("，".join(result.keywords))

    print("\n[3] RRF fusion hits:")
    for rank, hit in enumerate(result.fusion_hits, start=1):
        doc = service.documents[hit.doc_index]
        col = doc.column

        print(
            f"{rank}. {col.table_name}.{col.column_name} "
            f"rrf_score={hit.score:.6f} "
            f"sources={hit.sources} "
            f"matched_terms={hit.matched_terms} "
            f"best_rank_by_source={hit.best_rank_by_source}"
        )

    print("\n[4] Rerank results:")
    for rank, item in enumerate(result.rerank_results, start=1):
        doc = service.documents[item.doc_index]
        col = doc.column

        print(
            f"{rank}. {col.table_name}.{col.column_name} "
            f"rerank_score={item.rerank_score:.6f}"
        )

    print("\n[5] Schema graph prompt context:")
    print(result.schema_graph.to_prompt_context())


if __name__ == "__main__":
    main()
