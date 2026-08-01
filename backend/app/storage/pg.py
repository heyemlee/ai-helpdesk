"""
Postgres 连接与 schema 管理（asyncpg）。

存储收敛：工单、对话审计、KB 向量、情景记忆、用户画像统一进一个
Postgres 库（pgvector 扩展做向量检索），替换掉 chromadb 容器和
两个 SQLite 文件。Redis 保持不动（工作记忆 / 限流 / 熔断共享）。

知识库片段同时建两套索引：稠密的 pgvector 列走语义匹配，稀疏的 tsvector
列（GIN 索引）走关键词字面匹配，两路在 KnowledgeBase 层用 RRF 融合。
tsvector 用 'simple' 配置——中文分词由 core/lexical.py 在写入前完成
（字符二元组），不依赖 zhparser/pg_bigm 这类需要单独编译的扩展。

片段之上还有一张文档表（kb_documents）：存导入时的原文、来源和时间，
片段用 doc_id 指回去。它不参与检索，只回答"知识库里到底有哪些文档"。

维度契约：向量列的维度在建表时由当前 Embedder 决定。启动时校验现有
表的维度与配置一致，不一致直接抛错——静默把 256 维向量塞进 1536 维列
（或反过来）产生的是无意义的检索结果，宁可 fail fast 逼运维重建向量表。
"""
from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS tickets (
    id                TEXT PRIMARY KEY,
    conv_id           TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    request_id        TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL,
    intent            TEXT NOT NULL DEFAULT '',
    urgency           TEXT NOT NULL DEFAULT '',
    answer_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_message      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'open',
    created_at        DOUBLE PRECISION NOT NULL,
    updated_at        DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_conv   ON tickets (conv_id);

-- 工单通知的待发送记录（outbox）。与 tickets/ticket_store.py 的 SQLite 版同构，
-- 说明见那边。`ticket_id` 的唯一约束就是去重：同一个工单只入队一次，
-- 重试重发的是同一条记录（`id` 同时是发出去的 Idempotency-Key）。
CREATE TABLE IF NOT EXISTS ticket_notifications (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL UNIQUE,
    url             TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE PRECISION NOT NULL,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_due ON ticket_notifications (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS conversation_log (
    seq         BIGSERIAL PRIMARY KEY,
    conv_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    request_id  TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    agent_type  TEXT NOT NULL DEFAULT '',
    created_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_convlog_conv ON conversation_log (conv_id, seq);
CREATE INDEX IF NOT EXISTS idx_convlog_time ON conversation_log (created_at);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL DEFAULT 1,
    ingested_at  DOUBLE PRECISION NOT NULL,
    embedding    vector({dim}) NOT NULL,
    tokens       tsvector,
    doc_id       TEXT NOT NULL DEFAULT ''
);
-- 稀疏检索列。对已存在的老表补列（CREATE TABLE IF NOT EXISTS 不会改已有表），
-- 老数据的 tokens 为 NULL，由 KnowledgeBase.backfill_lexical 在启动时回填。
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS tokens tsvector;
CREATE INDEX IF NOT EXISTS idx_kb_tokens ON kb_chunks USING GIN (tokens);

-- 文档：人读的单位。与 storage/vector_store.py 的 SQLite 版同构，说明见那边。
-- 分片用 doc_id 指回来；老分片补出来的 doc_id 是空串，表示"还不知道属于哪篇"，
-- 由 KnowledgeBase.ensure_documents 在启动时归档。带常量默认值的 ADD COLUMN
-- 不重写表（PG 11+），已有的分片、向量和关键词索引一行不动。
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS doc_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks (doc_id);
CREATE TABLE IF NOT EXISTS kb_documents (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT 'original',
    source_type  TEXT NOT NULL DEFAULT '',
    source_name  TEXT NOT NULL DEFAULT '',
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    created_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_docs_hash ON kb_documents (content_hash);
CREATE INDEX IF NOT EXISTS idx_kb_docs_created ON kb_documents (created_at);

CREATE TABLE IF NOT EXISTS memory_episodic (
    id        TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL,
    conv_id   TEXT NOT NULL DEFAULT '',
    summary   TEXT NOT NULL,
    full_text TEXT NOT NULL DEFAULT '',
    ts        DOUBLE PRECISION NOT NULL,
    embedding vector({dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_user ON memory_episodic (user_id, ts);

CREATE TABLE IF NOT EXISTS memory_profile (
    user_id TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL,
    ts      DOUBLE PRECISION NOT NULL
);
"""

# 数据量到十万级后再建 ivfflat/hnsw 索引；demo 阶段顺序扫描反而更快，
# 且 ivfflat 需要先有数据才能训练聚类中心。DDL 保留在注释里作为运维手册：
#   CREATE INDEX ON kb_chunks USING hnsw (embedding vector_cosine_ops);
#   CREATE INDEX ON memory_episodic USING hnsw (embedding vector_cosine_ops);


class Database:
    """asyncpg 连接池 + schema 初始化。所有 PG 存储实现共用一个池。"""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self, embedding_dim: int) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max, timeout=10.0
        )
        await self._init_schema(embedding_dim)
        logger.info(f"Postgres 已连接，向量维度 {embedding_dim}")

    async def _init_schema(self, dim: int) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA.format(dim=dim))
            await self._check_vector_dim(conn, "kb_chunks", dim)
            await self._check_vector_dim(conn, "memory_episodic", dim)

    @staticmethod
    async def _check_vector_dim(conn: asyncpg.Connection, table: str, expected: int) -> None:
        """现有表的向量维度必须与当前 Embedder 一致，否则 fail fast。"""
        row = await conn.fetchrow(
            "SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a"
            " JOIN pg_class c ON a.attrelid = c.oid"
            " WHERE c.relname = $1 AND a.attname = 'embedding'",
            table,
        )
        if row is None:
            return
        match = re.search(r"vector\((\d+)\)", row["t"] or "")
        if match and int(match.group(1)) != expected:
            raise RuntimeError(
                f"表 {table} 的向量维度是 {match.group(1)}，当前 Embedder 是 {expected} 维。"
                f"换嵌入后端后必须重建向量表（历史向量已无意义）：\n"
                f"  DROP TABLE {table}; 然后重启服务并重新导入。"
            )

    # ── 查询封装 ──────────────────────────────────────────────────────────────

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database 未连接，先调用 connect()")
        return self._pool

    async def execute(self, sql: str, *args: Any) -> str:
        async with self._require_pool().acquire() as conn:
            return await conn.execute(sql, *args)

    async def executemany(self, sql: str, args_list: list[tuple]) -> None:
        if not args_list:
            return
        async with self._require_pool().acquire() as conn:
            await conn.executemany(sql, args_list)

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self._require_pool().acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        async with self._require_pool().acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
