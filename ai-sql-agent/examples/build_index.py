from __future__ import annotations

import os
from itertools import combinations
from typing import List

from ai_sql_agent.indexing.embedding_client import AliyunEmbeddingClient, AliyunEmbeddingConfig
from ai_sql_agent.indexing.index_builder import SchemaIndexBuilder
from ai_sql_agent.indexing.pgvector_client import PgVectorSchemaIndexClient, PgVectorSchemaIndexConfig
from ai_sql_agent.indexing.objects import ColumnSchema, IndexTextBundle, TableRelation
from ai_sql_agent.indexing.text_builder import SchemaIndexTextBuilder


def build_demo_columns() -> List[ColumnSchema]:
    """
    构造 Demo 字段元数据。

    字段只描述字段本身，表关系单独由 TableRelation 表达。
    """
    return [
        ColumnSchema(
            database="showroom_db",
            table_name="sales_order",
            column_name="order_no",
            data_type="int",
            nullable=False,
            table_description="该表记录展厅销售订单信息，包括客户名称、签单金额、定金以及订单状态等内容。",
            table_aliases=["销售订单表", "签单表"],
            description="销售订单号，订单唯一标识。",
            aliases=["订单号", "订单编号"],
            semantic_role="join_key",
            business_usage="用于唯一标识销售订单表中的订单。",
        ),
        ColumnSchema(
            database="showroom_db",
            table_name="sales_order",
            column_name="order_amount",
            data_type="decimal",
            nullable=False,
            table_description="该表记录展厅销售订单信息，包括客户名称、签单金额、定金以及订单状态等内容。",
            table_aliases=["销售订单表", "签单表"],
            description="订单签单金额。",
            aliases=["签单金额", "订单金额", "成交金额", "合同金额"],
            semantic_role="metric_filter",
            value_range="> 0",
            data_distribution="零售单多在2万-20万区间，工程单可达百万以上。",
            business_usage="用于衡量订单规模，筛选大额订单或工程单。",
            samples=["23000.00", "86000.00", "380000.00", "2100000.00"],
            index_texts=IndexTextBundle(
                keyword_text=(
                    "order_amount 签单金额 订单金额 成交金额 "
                    "合同金额 大额订单 销售订单表 签单表"
                ),
                vector_text=(
                    "字段名：order_amount。所属表：sales_order。"
                    "该字段表示订单签单金额，用于衡量订单规模，"
                    "可用于筛选大额订单。"
                ),
                rerank_text=(
                    "表名：sales_order\n"
                    "字段名：order_amount\n"
                    "数据类型：decimal\n"
                    "字段含义：订单签单金额。\n"
                    "字段别名：签单金额、订单金额、成交金额、合同金额。\n"
                    "取值范围：>0。\n"
                    "数据分布：工程单可达百万以上。\n"
                    "业务用途：用于筛选签单金额大于某个阈值的订单。"
                ),
            ),
        ),
        ColumnSchema(
            database="showroom_db",
            table_name="order_item",
            column_name="order_no",
            data_type="int",
            nullable=False,
            table_description="该表记录订单柜体明细，包括柜型、门板材质以及柜体数量等内容。",
            table_aliases=["订单柜体明细表", "柜体明细表"],
            description="销售订单号，订单唯一标识。",
            aliases=["订单号", "订单编号"],
            semantic_role="join_key",
            business_usage="用于唯一标识柜体明细表中的订单。",
        ),
        ColumnSchema(
            database="showroom_db",
            table_name="order_item",
            column_name="door_material",
            data_type="varchar",
            nullable=False,
            table_description="该表记录订单柜体明细，包括柜型、门板材质以及柜体数量等内容。",
            table_aliases=["订单柜体明细表", "柜体明细表"],
            description="订单柜体的门板材质。",
            aliases=["门板材质", "门板", "材质", "门板工艺"],
            semantic_role="output_dimension",
            value_range="烤漆 / 膜压 / 实木 / 双饰面",
            data_distribution="零售单以烤漆、膜压为主，工程单以膜压、双饰面为主。",
            business_usage="用于展示或分析订单使用的门板材质。",
            samples=["烤漆", "膜压", "实木", "双饰面"],
            index_texts=IndexTextBundle(
                keyword_text=(
                    "door_material 门板材质 门板 材质 门板工艺 "
                    "订单柜体明细表 柜体明细表"
                ),
                vector_text=(
                    "字段名：door_material。所属表：order_item。"
                    "该字段表示订单柜体的门板材质，通常作为查询结果输出。"
                ),
                rerank_text=(
                    "表名：order_item\n"
                    "字段名：door_material\n"
                    "数据类型：varchar\n"
                    "字段含义：订单柜体的门板材质。\n"
                    "字段别名：门板材质、门板、材质、门板工艺。\n"
                    "取值：烤漆、膜压、实木、双饰面。\n"
                    "业务用途：当用户询问订单用的什么门板材质时，通常作为输出字段。"
                ),
            ),
        ),
    ]


def build_demo_relations() -> List[TableRelation]:
    """
    构造 Demo 表关系。
    """
    return [
        TableRelation(
            database="showroom_db",
            source_table="sales_order",
            source_column="order_no",
            target_table="order_item",
            target_column="order_no",
            description="通过订单号关联销售订单信息与柜体明细信息。",
        )
    ]

def main() -> None:
    """
    运行示例。

    前置：启动 pgvector 并配置连接串

        docker compose --project-directory . -f deploy/docker-compose.pgvector.yml up -d
        export AI_SQL_PGVECTOR_DSN="postgresql://ai_sql:ai_sql@localhost:5432/ai_sql"

    执行：
        python examples/build_index.py

    如果要使用阿里云真实 Embedding：
        export DASHSCOPE_API_KEY="你的APIKey"
    """
    embedding_dim = 1024

    embedding_client = AliyunEmbeddingClient(
        AliyunEmbeddingConfig(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            model="text-embedding-v4",
            dimensions=embedding_dim,
            use_mock_when_no_api_key=True,
        )
    )

    try:
        index_client = PgVectorSchemaIndexClient(
            PgVectorSchemaIndexConfig(
                field_table_name=os.getenv(
                    "SCHEMA_FIELD_TABLE", "schema_field_index_demo"
                ),
                relation_table_name=os.getenv(
                    "SCHEMA_RELATION_TABLE", "schema_relation_index_demo"
                ),
                embedding_dim=embedding_dim,
                recreate_table=True,
            )
        )
    except Exception as exc:
        print(f"pgvector 不可用（{exc}）。")
        print("请先启动：docker compose --project-directory . -f deploy/docker-compose.pgvector.yml up -d")
        print('并设置：export AI_SQL_PGVECTOR_DSN="postgresql://ai_sql:ai_sql@localhost:5432/ai_sql"')
        return

    index_builder = SchemaIndexBuilder(
        text_builder=SchemaIndexTextBuilder(),
        embedding_client=embedding_client,
        index_client=index_client,
    )

    columns = build_demo_columns()
    relations = build_demo_relations()

    print("=" * 80)
    print("Schema Indexing Demo")
    print("=" * 80)

    print("\n[1] 构建字段三级索引文本：")
    documents = index_builder.build_documents_only(columns)

    for index, doc in enumerate(documents, start=1):
        print(f"\n{index}. {doc.doc_id}")
        print(f"keyword_text: {doc.keyword_text}")
        print(f"vector_text : {doc.vector_text}")
        print(f"rerank_text : {doc.rerank_text}")

    print("\n[2] 写入字段索引和表关系索引：")
    result = index_builder.build(
        columns=columns,
        relations=relations,
    )

    print(f"field_collection_name   : {result.field_collection_name}")
    print(f"relation_collection_name: {result.relation_collection_name}")
    print(f"field_document_count    : {result.field_document_count}")
    print(f"relation_count          : {result.relation_count}")

    print("\n[3] 简单字段向量检索验证：")
    query_vector = embedding_client.embed_texts(["签单金额"])[0].astype(float).tolist()
    search_results = index_client.search_fields(
        query_embedding=query_vector,
        top_k=3,
    )

    hit_tables = []

    for rank, item in enumerate(search_results, start=1):
        table_name = item.get("table_name")
        column_name = item.get("column_name")

        if table_name and table_name not in hit_tables:
            hit_tables.append(table_name)

        print(
            f"{rank}. "
            f"{table_name}.{column_name} "
            f"score={item.get('distance'):.4f}"
        )
        print(f"   vector_text: {item.get('vector_text')}")

    print("\n[4] 根据命中字段所属表查询表关系：")

    # Demo 中人为加入 order_item，模拟两阶段检索同时命中了金额字段和材质字段。
    for table_name in ["order_item"]:
        if table_name not in hit_tables:
            hit_tables.append(table_name)

    table_pairs = list(combinations(hit_tables, 2))

    print(f"命中表集合: {hit_tables}")
    print(f"两两组合  : {table_pairs}")

    graph_relations = index_client.query_relations_by_table_pairs(
        database="showroom_db",
        table_pairs=table_pairs,
    )

    for relation in graph_relations:
        print(f"- {relation.join_condition}；{relation.description}")


if __name__ == "__main__":
    main()
