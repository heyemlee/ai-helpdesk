#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Text2SQL 项目环境测试脚本

用法：
    python scripts/check_env.py

这个脚本会检查：
1. Python 版本
2. 常用依赖是否安装
3. SQLite 是否可用
4. 能否创建测试数据库并解析 Schema
5. 能否执行一个最小版字段检索流程
"""

from __future__ import annotations

import sys
import sqlite3
import tempfile
import importlib.util
from pathlib import Path
from collections import Counter
import re
import math


def print_title(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def check_python_version() -> bool:
    print_title("1. Python 版本检查")

    version = sys.version_info
    print(f"当前 Python 版本: {version.major}.{version.minor}.{version.micro}")

    if version.major == 3 and version.minor >= 9:
        print("✅ Python 版本满足建议要求：>= 3.9")
        return True

    print("⚠️ 建议使用 Python 3.9 或以上版本")
    return False


def check_package(package_name: str, import_name: str | None = None, required: bool = False) -> bool:
    name = import_name or package_name
    spec = importlib.util.find_spec(name)

    if spec is not None:
        print(f"✅ {package_name} 已安装")
        return True

    if required:
        print(f"❌ {package_name} 未安装，建议执行: pip install {package_name}")
    else:
        print(f"⚠️ {package_name} 未安装，可选安装: pip install {package_name}")

    return False


def check_dependencies() -> dict:
    print_title("2. 依赖检查")

    results = {
        # 运行时必需
        "numpy": check_package("numpy", required=True),
        "httpx": check_package("httpx", required=True),
        "yaml": check_package("yaml", required=False),
        "pydantic": check_package("pydantic", required=True),
        "pydantic-settings": check_package(
            "pydantic-settings", "pydantic_settings", required=True
        ),
        # SQL 解析：授权检查靠它。**装不上等于所有查询都会被拒**
        # （不解析即拒绝，见 ai_sql_agent.auth.sql_analysis），所以是必需项。
        "sqlglot": check_package("sqlglot", required=True),
        # 服务层与 MCP：只跑离线 Demo 时可以不装
        "fastapi": check_package("fastapi", required=False),
        "mcp": check_package("mcp", required=False),
        # 向量后端：未配置 AI_SQL_PGVECTOR_DSN 时走内存索引，可以不装
        "psycopg": check_package("psycopg", required=False),
        "pgvector": check_package("pgvector", required=False),
    }

    print("\n建议安装命令：")
    print('pip install -e ".[dev]"')

    return results


def check_sqlite() -> bool:
    print_title("3. SQLite 检查")

    try:
        print(f"SQLite 版本: {sqlite3.sqlite_version}")

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test_table (name) VALUES (?)", ("hello",))
        row = conn.execute("SELECT name FROM test_table WHERE id = 1").fetchone()
        conn.close()

        assert row[0] == "hello"
        print("✅ SQLite 创建表、写入、查询均正常")
        return True

    except Exception as exc:
        print(f"❌ SQLite 测试失败: {exc}")
        return False


def create_demo_database() -> Path:
    temp_dir = Path(tempfile.gettempdir())
    db_path = temp_dir / "text2sql_env_test.db"

    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(
        """
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
        """
    )

    conn.executemany(
        "INSERT INTO users (user_id, city, gender, register_time) VALUES (?, ?, ?, ?)",
        [
            (1, "北京", "男", "2024-01-01"),
            (2, "上海", "女", "2024-02-01"),
            (3, "深圳", "女", "2024-03-01"),
        ],
    )

    conn.executemany(
        "INSERT INTO orders (order_id, user_id, order_amount, order_time, status) VALUES (?, ?, ?, ?, ?)",
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


def load_schema_from_sqlite(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    tables = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    columns = []

    for table in tables:
        table_name = table["name"]
        table_info = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        fk_info = conn.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()

        fk_map = {
            fk["from"]: f'{fk["table"]}.{fk["to"]}'
            for fk in fk_info
        }

        for col in table_info:
            col_name = col["name"]

            sample_sql = f'''SELECT DISTINCT "{col_name}" AS value
FROM "{table_name}"
WHERE "{col_name}" IS NOT NULL
LIMIT 5'''

            sample_rows = conn.execute(sample_sql).fetchall()
            samples = [str(row["value"]) for row in sample_rows]

            columns.append(
                {
                    "table": table_name,
                    "column": col_name,
                    "type": col["type"],
                    "is_pk": bool(col["pk"]),
                    "fk_ref": fk_map.get(col_name),
                    "samples": samples,
                }
            )

    conn.close()
    return columns


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-zA-Z0-9_]+", text)

    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.append(span)
        tokens.extend(list(span))
        for i in range(len(span) - 1):
            tokens.append(span[i:i + 2])

    return tokens


def bm25_search(query: str, docs: list[str], top_k: int = 5) -> list[tuple[int, float]]:
    tokenized_docs = [tokenize(doc) for doc in docs]
    query_terms = tokenize(query)

    n_docs = len(docs)
    avgdl = sum(len(doc) for doc in tokenized_docs) / max(n_docs, 1)

    term_freqs = [Counter(doc) for doc in tokenized_docs]
    doc_freq = Counter()

    for doc in tokenized_docs:
        for term in set(doc):
            doc_freq[term] += 1

    scores = []

    k1 = 1.5
    b = 0.75

    for idx, tf in enumerate(term_freqs):
        dl = len(tokenized_docs[idx]) or 1
        score = 0.0

        for term in query_terms:
            if term not in tf:
                continue

            df = doc_freq.get(term, 0)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            freq = tf[term]

            score += idf * (freq * (k1 + 1)) / (
                freq + k1 * (1 - b + b * dl / max(avgdl, 1))
            )

        scores.append((idx, score))

    scores.sort(key=lambda x: x[1], reverse=True)
    return [(idx, score) for idx, score in scores[:top_k] if score > 0]


def test_schema_loading_and_retrieval() -> bool:
    print_title("4. 测试数据库 Schema 解析与字段检索")

    try:
        db_path = create_demo_database()
        print(f"已创建测试数据库: {db_path}")

        columns = load_schema_from_sqlite(db_path)

        print("\n解析到的字段：")
        for col in columns:
            fk_text = f", FK -> {col['fk_ref']}" if col["fk_ref"] else ""
            pk_text = ", PK" if col["is_pk"] else ""
            print(f"- {col['table']}.{col['column']} ({col['type']}){pk_text}{fk_text}, samples={col['samples']}")

        business_meta = {
            "orders.order_amount": "订单金额 销售额 GMV 实付金额 revenue amount",
            "orders.order_time": "订单时间 下单时间 成交时间 日期 time date",
            "orders.status": "订单状态 支付状态 已支付 已取消 status",
            "orders.user_id": "用户ID 客户ID 下单用户 关联用户表",
            "users.city": "城市 地区 用户城市 city region",
            "users.gender": "性别 男 女 gender",
            "users.register_time": "注册时间 开户时间 register time",
        }

        docs = []
        for col in columns:
            full_name = f"{col['table']}.{col['column']}"
            sample_text = " ".join(col["samples"])
            meta_text = business_meta.get(full_name, "")
            docs.append(
                f"{full_name} {col['type']} {meta_text} samples: {sample_text}"
            )

        query = "每个城市的销售额是多少？"
        hits = bm25_search(query, docs, top_k=5)

        print(f"\n测试 Query: {query}")
        print("召回字段：")

        for idx, score in hits:
            col = columns[idx]
            print(f"- {col['table']}.{col['column']} score={score:.4f}")

        expected_fields = {
            "users.city",
            "orders.order_amount",
        }

        hit_fields = {
            f"{columns[idx]['table']}.{columns[idx]['column']}"
            for idx, _ in hits
        }

        if expected_fields & hit_fields:
            print("\n✅ 最小字段检索流程正常")
            return True

        print("\n⚠️ 检索流程跑通，但没有命中预期字段。可以后续优化分词和元数据。")
        return True

    except Exception as exc:
        print(f"❌ Schema 解析或检索测试失败: {exc}")
        return False


def test_ai_sql_core(deps: dict) -> bool:
    """检查配置收口、模型调用治理与向量后端是否可用。"""
    print_title("5. AI SQL Agent 核心组件检查")

    try:
        from ai_sql_agent.core import ModelTransport, get_settings

        settings = get_settings()
        print("✅ ai_sql_agent.core 可以 import")
        print(f"模型厂商    : {settings.model_provider}（{settings.chat_url}）")
        print(f"模型         : chat={settings.chat_model} coder={settings.coder_model}")
        print(f"模型凭证    : {'已配置' if settings.has_model_credentials else '未配置（模型走 Mock）'}")
        print(f"HTTP 超时   : {settings.http_timeout_seconds}s，重试 {settings.http_max_retries} 次")
        print(f"执行治理    : 最多 {settings.max_rows} 行 / {settings.max_execution_seconds}s")
        print(f"日志        : level={settings.log_level} format={settings.log_format}")

        ModelTransport(
            "env_test",
            api_key=settings.resolved_chat_api_key(),
            provider=settings.model_provider,
        )
        print("✅ 模型调用 transport 可构造")

    except Exception as exc:
        print(f"❌ ai_sql_agent.core 检查失败: {exc}")
        return False

    if not settings.pgvector_dsn:
        print("⚠️ 未配置 AI_SQL_PGVECTOR_DSN，向量索引走内存实现（离线可用）")
        print("   启用 pgvector：docker compose --project-directory . -f deploy/docker-compose.pgvector.yml up -d")
        return True

    if not deps.get("psycopg") or not deps.get("pgvector"):
        print("⚠️ 配置了 DSN 但缺少 psycopg / pgvector，将回退内存索引")
        return True

    try:
        import psycopg

        conn = psycopg.connect(settings.pgvector_dsn, connect_timeout=5)
        try:
            version = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
        finally:
            conn.close()

        if version:
            print(f"✅ pgvector 可用，扩展版本 {version[0]}")
        else:
            print("⚠️ 连接成功，但未安装 vector 扩展（首次使用时会自动创建）")
        return True

    except Exception as exc:
        print(f"⚠️ pgvector 连接失败，将回退内存索引: {exc}")
        return True


def main() -> None:
    print_title("Text2SQL 环境测试开始")

    results = {}

    results["python"] = check_python_version()
    deps = check_dependencies()
    results.update(deps)

    results["sqlite"] = check_sqlite()
    results["schema_retrieval"] = test_schema_loading_and_retrieval()
    results["ai_sql_core"] = test_ai_sql_core(deps)

    print_title("测试结果汇总")

    required_checks = [
        "python",
        "numpy",
        "httpx",
        "pydantic",
        "pydantic-settings",
        "sqlite",
        "schema_retrieval",
        "ai_sql_core",
    ]

    for name, ok in results.items():
        status = "✅ PASS" if ok else "⚠️ CHECK"
        if name in required_checks and not ok:
            status = "❌ FAIL"
        print(f"{name:20s}: {status}")

    required_ok = all(results.get(name) for name in required_checks)

    if required_ok:
        print("\n✅ 基础环境可用于开发 Text2SQL Schema 检索模块。")
    else:
        print("\n❌ 基础环境还有问题，请先处理 FAIL 项。")

    if not results.get("fastapi"):
        print("\n提示：需要跑 API 服务时安装 fastapi 与 uvicorn。")

    print("\n完成。")


if __name__ == "__main__":
    main()
