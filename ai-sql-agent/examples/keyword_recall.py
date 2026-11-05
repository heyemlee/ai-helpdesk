from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from ai_sql_agent.retrieval.graph_builder import build_schema_graph
from ai_sql_agent.retrieval.retriever import SchemaRetriever
from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader


def create_demo_database() -> Path:
    """
    创建一个最小 Demo 数据库：
    - users: 用户维表
    - orders: 订单事实表
    """
    db_path = Path(tempfile.gettempdir()) / "text2sql_schema_retrieval_demo.db"

    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(
        '''
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            city TEXT,
            gender TEXT,
            register_time TEXT
        );

        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            order_amount REAL NOT NULL,
            order_time TEXT NOT NULL,
            status TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        '''
    )

    conn.executemany(
        '''
        INSERT INTO users (user_id, city, gender, register_time)
        VALUES (?, ?, ?, ?)
        ''',
        [
            (1, "北京", "男", "2024-01-01"),
            (2, "上海", "女", "2024-02-01"),
            (3, "深圳", "女", "2024-03-01"),
        ],
    )

    conn.executemany(
        '''
        INSERT INTO orders (order_id, user_id, order_amount, order_time, status)
        VALUES (?, ?, ?, ?, ?)
        ''',
        [
            (101, 1, 99.0, "2024-04-01", "已支付"),
            (102, 1, 199.0, "2024-04-02", "已支付"),
            (103, 2, 88.0, "2024-04-02", "已取消"),
            (104, 3, 299.0, "2024-04-03", "已支付"),
        ],
    )

    conn.commit()
    conn.close()

    return db_path


def get_business_meta() -> dict:
    """
    表级和字段级业务元数据。

    真实项目里建议从 YAML / JSON / 元数据平台读取。
    """
    return {
        "orders": {
            "description": "订单事实表，记录客户每一笔销售订单，包括订单金额、订单时间、订单状态和下单用户。",
            "aliases": ["订单表", "销售订单表"],
            "columns": {
                "order_id": {
                    "description": "订单唯一标识。",
                    "aliases": ["订单ID", "订单编号"],
                },
                "user_id": {
                    "description": "下单用户ID，用于关联用户表。",
                    "aliases": ["用户ID", "客户ID", "下单用户"],
                },
                "order_amount": {
                    "description": "订单实付金额，可用于计算销售额、GMV、收入、客单价等指标。",
                    "aliases": ["销售额", "GMV", "实付金额", "订单金额", "收入"],
                },
                "order_time": {
                    "description": "订单创建时间，可用于按天、月、季度统计。",
                    "aliases": ["下单时间", "订单时间", "成交时间"],
                },
                "status": {
                    "description": "订单状态，例如已支付、已取消、已退款。",
                    "aliases": ["订单状态", "支付状态"],
                },
            },
        },
        "users": {
            "description": "用户维度表，记录用户的基础属性，包括城市、性别和注册时间。",
            "aliases": ["用户表", "客户表"],
            "columns": {
                "user_id": {
                    "description": "用户唯一标识。",
                    "aliases": ["用户ID", "客户ID"],
                },
                "city": {
                    "description": "用户所在城市，可作为地域分析维度。",
                    "aliases": ["城市", "地区", "用户城市"],
                },
                "gender": {
                    "description": "用户性别。",
                    "aliases": ["性别"],
                },
                "register_time": {
                    "description": "用户注册时间。",
                    "aliases": ["注册时间", "开户时间"],
                },
            },
        },
    }


def main() -> None:
    db_path = create_demo_database()

    print("=" * 80)
    print("Schema Retrieval Demo")
    print("=" * 80)
    print(f"Demo database: {db_path}")

    loader = SQLiteSchemaLoader(
        db_path=db_path,
        database_name="demo_db",
        business_meta=get_business_meta(),
        sample_size=5,
    )

    tables, columns, relations = loader.load()

    print("\n[1] Loaded tables:")
    for table in tables.values():
        print(f"- {table.table_name}: {table.description}")

    print("\n[2] Loaded columns:")
    for column in columns:
        pk_text = " PK" if column.is_primary_key else ""
        fk_text = f" FK->{column.foreign_key_ref}" if column.foreign_key_ref else ""
        print(
            f"- {column.table_name}.{column.column_name} "
            f"({column.data_type}){pk_text}{fk_text}, "
            f"aliases={column.aliases}, samples={column.samples}"
        )

    retriever = SchemaRetriever(
        tables=tables,
        columns=columns,
        relations=relations,
    )
    retriever.build()

    query = "每个城市的销售额是多少？"

    hits = retriever.retrieve(query, top_k=6)

    print("\n[3] Query:")
    print(query)

    print("\n[4] Retrieved fields:")
    for idx, hit in enumerate(hits, start=1):
        col = hit.column
        print(
            f"{idx}. {col.table_name}.{col.column_name} "
            f"score={hit.score:.4f} "
            f"description={col.description}"
        )

    schema_graph = build_schema_graph(
        hits=hits,
        tables=tables,
        all_columns=columns,
        relations=relations,
        include_join_columns=True,
    )

    print("\n[5] Schema graph prompt context:")
    print(schema_graph.to_prompt_context())


if __name__ == "__main__":
    main()
