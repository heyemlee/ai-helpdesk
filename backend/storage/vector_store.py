"""
向量与画像存储的双后端实现。

接口（KB 片段 / 情景记忆 / 用户画像）：
  - PgStore    —— Postgres + pgvector，`<=>` 余弦距离检索，生产形态；
  - LocalStore —— 单文件 SQLite + 纯 Python 余弦暴力扫，零外部依赖，
                  demo / 单测 / 未配置 POSTGRES_DSN 时的兜底。

两个实现的方法签名完全一致，KnowledgeBase 和 MemoryManager 只面向接口，
选哪个后端是 api/main.py 装配时的一行判断。

知识库检索是稠密 + 稀疏双路（融合在 KnowledgeBase 层做）：
  - kb_search          稠密：向量余弦，管"意思相近"；
  - kb_search_lexical  稀疏：PG 走 tsvector + ts_rank_cd，SQLite 走纯
                       Python BM25，管"字面命中"。
稀疏这一路同样返回余弦分数（查询向量顺带传进去算），这样融合后每条结果
都有一个口径一致的 score，下游的证据充分性判断不必区分它来自哪一路。

LocalStore 的暴力扫为什么可接受：demo 知识库百级片段、情景记忆按用户过滤后
更小，纯 Python 余弦在这个量级是亚毫秒。生产数据量走 PgStore。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.embedder import cosine
from core.lexical import bm25_scores

if TYPE_CHECKING:
    from storage.pg import Database

logger = logging.getLogger(__name__)

_LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL DEFAULT 1,
    ingested_at  REAL NOT NULL,
    embedding    TEXT NOT NULL,         -- JSON array
    tokens       TEXT NOT NULL DEFAULT ''  -- 空格分隔的稀疏检索 token
);
CREATE TABLE IF NOT EXISTS memory_episodic (
    id        TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL,
    conv_id   TEXT NOT NULL DEFAULT '',
    summary   TEXT NOT NULL,
    full_text TEXT NOT NULL DEFAULT '',
    ts        REAL NOT NULL,
    embedding TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_user ON memory_episodic (user_id, ts);
CREATE TABLE IF NOT EXISTS memory_profile (
    user_id TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL,
    ts      REAL NOT NULL
);
"""


class LocalStore:
    """SQLite 兜底实现。与 TicketStore 同一套并发策略：单连接 + 锁 + to_thread。"""

    backend = "sqlite_local"

    def __init__(self, db_path: str = "./data/vector_store.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_LOCAL_SCHEMA)
            self._migrate_tokens_column()
            self._db.commit()

    def _migrate_tokens_column(self) -> None:
        """
        给已存在的 kb_chunks 补 tokens 列。

        CREATE TABLE IF NOT EXISTS 对已有表是空操作，所以升级到混合检索后，
        老的 vector_store.db 里没有这一列，稀疏检索会直接报 no such column。
        补出来的列值为空，由 KnowledgeBase.backfill_lexical 在启动时回填。
        """
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(kb_chunks)")}
        if "tokens" not in cols:
            self._db.execute("ALTER TABLE kb_chunks ADD COLUMN tokens TEXT NOT NULL DEFAULT ''")
            logger.info("kb_chunks 已补 tokens 列（稀疏检索索引待回填）")

    async def init(self) -> None:
        """与 PgStore 对齐的异步初始化钩子（SQLite 在构造时已建表）。"""

    # ── KB 片段 ───────────────────────────────────────────────────────────────

    async def kb_existing_ids(self, ids: list[str]) -> set[str]:
        def _q() -> set[str]:
            with self._lock:
                marks = ",".join("?" * len(ids))
                rows = self._db.execute(
                    f"SELECT id FROM kb_chunks WHERE id IN ({marks})", ids
                ).fetchall()
            return {r["id"] for r in rows}
        return await asyncio.to_thread(_q) if ids else set()

    async def kb_add(self, chunks: list[dict[str, Any]]) -> None:
        """chunks: [{id,title,content,chunk_index,total_chunks,ingested_at,embedding,tokens}]"""
        def _w() -> None:
            with self._lock:
                self._db.executemany(
                    "INSERT OR IGNORE INTO kb_chunks"
                    " (id, title, content, chunk_index, total_chunks, ingested_at, embedding, tokens)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(c["id"], c["title"], c["content"], c["chunk_index"],
                      c["total_chunks"], c["ingested_at"], json.dumps(c["embedding"]),
                      c.get("tokens", ""))
                     for c in chunks],
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def kb_search(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, title, content, chunk_index, embedding FROM kb_chunks"
                ).fetchall()
            scored = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "chunk_index": r["chunk_index"],
                    "score": cosine(embedding, json.loads(r["embedding"])),
                }
                for r in rows
            ]
            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:top_k]
        return await asyncio.to_thread(_q)

    async def kb_search_lexical(
        self, tokens: list[str], embedding: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        """BM25 关键词检索。分数为 0（无字面命中）的片段直接不返回。"""
        if not tokens:
            return []

        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, title, content, chunk_index, embedding, tokens"
                    " FROM kb_chunks WHERE tokens <> ''"
                ).fetchall()
            if not rows:
                return []
            lex = bm25_scores(tokens, [r["tokens"].split() for r in rows])
            hits = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "chunk_index": r["chunk_index"],
                    "lex_score": score,
                    "score": cosine(embedding, json.loads(r["embedding"])),
                }
                for r, score in zip(rows, lex, strict=True)
                if score > 0
            ]
            hits.sort(key=lambda x: x["lex_score"], reverse=True)
            return hits[:top_k]
        return await asyncio.to_thread(_q)

    async def kb_rows_missing_tokens(self, limit: int) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, title, content FROM kb_chunks WHERE tokens = '' LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_q)

    async def kb_set_tokens(self, rows: list[tuple[str, str]]) -> None:
        """rows: [(chunk_id, token 串)]"""
        def _w() -> None:
            with self._lock:
                self._db.executemany(
                    "UPDATE kb_chunks SET tokens = ? WHERE id = ?",
                    [(tokens, chunk_id) for chunk_id, tokens in rows],
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def kb_count(self) -> int:
        def _q() -> int:
            with self._lock:
                return self._db.execute("SELECT COUNT(*) AS n FROM kb_chunks").fetchone()["n"]
        return await asyncio.to_thread(_q)

    # ── 情景记忆 ──────────────────────────────────────────────────────────────

    async def episodic_add(
        self, doc_id: str, user_id: str, conv_id: str,
        summary: str, full_text: str, embedding: list[float],
    ) -> None:
        def _w() -> None:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO memory_episodic"
                    " (id, user_id, conv_id, summary, full_text, ts, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, user_id, conv_id, summary, full_text[:500],
                     time.time(), json.dumps(embedding)),
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def episodic_search(
        self, user_id: str, embedding: list[float], top_k: int
    ) -> list[str]:
        def _q() -> list[str]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT summary, embedding FROM memory_episodic WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            scored = [
                (cosine(embedding, json.loads(r["embedding"])), r["summary"]) for r in rows
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            return [s for _, s in scored[:top_k]]
        return await asyncio.to_thread(_q)

    # ── 用户画像（KV，无向量）─────────────────────────────────────────────────

    async def profile_set(self, user_id: str, conv_id: str, profile_json: str) -> None:
        def _w() -> None:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO memory_profile (user_id, conv_id, profile, ts)"
                    " VALUES (?, ?, ?, ?)",
                    (user_id, conv_id, profile_json, time.time()),
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def profile_get(self, user_id: str) -> str | None:
        def _q() -> str | None:
            with self._lock:
                row = self._db.execute(
                    "SELECT profile FROM memory_profile WHERE user_id = ?", (user_id,)
                ).fetchone()
            return row["profile"] if row else None
        return await asyncio.to_thread(_q)

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._db.close()
        await asyncio.to_thread(_close)


class PgStore:
    """Postgres + pgvector 实现。所有检索用 `<=>`（余弦距离）走索引。"""

    backend = "postgres_pgvector"

    def __init__(self, db: Database):
        self._db = db

    async def init(self) -> None:
        """建表已由 Database.init_schema 完成，这里无事可做。"""

    @staticmethod
    def _vec(embedding: list[float]) -> str:
        """pgvector 的文本字面量：'[0.1,0.2,...]'。"""
        return "[" + ",".join(f"{x:.6g}" for x in embedding) + "]"

    # ── KB 片段 ───────────────────────────────────────────────────────────────

    async def kb_existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        rows = await self._db.fetch(
            "SELECT id FROM kb_chunks WHERE id = ANY($1::text[])", ids
        )
        return {r["id"] for r in rows}

    async def kb_add(self, chunks: list[dict[str, Any]]) -> None:
        await self._db.executemany(
            "INSERT INTO kb_chunks"
            " (id, title, content, chunk_index, total_chunks, ingested_at, embedding, tokens)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7::vector, to_tsvector('simple', $8))"
            " ON CONFLICT (id) DO NOTHING",
            [(c["id"], c["title"], c["content"], c["chunk_index"],
              c["total_chunks"], c["ingested_at"], self._vec(c["embedding"]),
              c.get("tokens", ""))
             for c in chunks],
        )

    async def kb_search(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            "SELECT id, title, content, chunk_index,"
            "       1 - (embedding <=> $1::vector) AS score"
            " FROM kb_chunks ORDER BY embedding <=> $1::vector LIMIT $2",
            self._vec(embedding), top_k,
        )
        return [dict(r) for r in rows]

    async def kb_search_lexical(
        self, tokens: list[str], embedding: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        """
        tsvector 关键词检索，GIN 索引 + ts_rank_cd 排序。

        token 之间用 `|`（OR）而不是 `&`：客服问句里多数 token 是无关紧要的
        修饰语（"这个""怎么办"），AND 会让任何一个噪声 token 就把整条查询打空。
        排序交给 ts_rank_cd —— 命中得多、命中得密的片段自然排前面。
        """
        if not tokens:
            return []
        rows = await self._db.fetch(
            "SELECT id, title, content, chunk_index,"
            "       ts_rank_cd(tokens, q) AS lex_score,"
            "       1 - (embedding <=> $2::vector) AS score"
            " FROM kb_chunks, to_tsquery('simple', $1) AS q"
            " WHERE tokens @@ q ORDER BY lex_score DESC LIMIT $3",
            " | ".join(tokens), self._vec(embedding), top_k,
        )
        return [dict(r) for r in rows]

    async def kb_rows_missing_tokens(self, limit: int) -> list[dict[str, Any]]:
        # 空 tsvector 和 NULL 都算"没建索引"：前者来自 to_tsvector('simple', '')，
        # 后者来自升级补列。少判其中一种，回填就会漏掉一半存量数据。
        rows = await self._db.fetch(
            "SELECT id, title, content FROM kb_chunks"
            " WHERE tokens IS NULL OR tokens = ''::tsvector LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    async def kb_set_tokens(self, rows: list[tuple[str, str]]) -> None:
        await self._db.executemany(
            "UPDATE kb_chunks SET tokens = to_tsvector('simple', $2) WHERE id = $1", rows
        )

    async def kb_count(self) -> int:
        row = await self._db.fetchrow("SELECT COUNT(*) AS n FROM kb_chunks")
        return row["n"]

    # ── 情景记忆 ──────────────────────────────────────────────────────────────

    async def episodic_add(
        self, doc_id: str, user_id: str, conv_id: str,
        summary: str, full_text: str, embedding: list[float],
    ) -> None:
        await self._db.execute(
            "INSERT INTO memory_episodic"
            " (id, user_id, conv_id, summary, full_text, ts, embedding)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7::vector)"
            " ON CONFLICT (id) DO UPDATE SET summary = EXCLUDED.summary",
            doc_id, user_id, conv_id, summary, full_text[:500], time.time(),
            self._vec(embedding),
        )

    async def episodic_search(
        self, user_id: str, embedding: list[float], top_k: int
    ) -> list[str]:
        rows = await self._db.fetch(
            "SELECT summary FROM memory_episodic WHERE user_id = $1"
            " ORDER BY embedding <=> $2::vector LIMIT $3",
            user_id, self._vec(embedding), top_k,
        )
        return [r["summary"] for r in rows]

    # ── 用户画像 ──────────────────────────────────────────────────────────────

    async def profile_set(self, user_id: str, conv_id: str, profile_json: str) -> None:
        await self._db.execute(
            "INSERT INTO memory_profile (user_id, conv_id, profile, ts)"
            " VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (user_id) DO UPDATE"
            " SET conv_id = EXCLUDED.conv_id, profile = EXCLUDED.profile, ts = EXCLUDED.ts",
            user_id, conv_id, profile_json, time.time(),
        )

    async def profile_get(self, user_id: str) -> str | None:
        row = await self._db.fetchrow(
            "SELECT profile FROM memory_profile WHERE user_id = $1", user_id
        )
        return row["profile"] if row else None

    async def close(self) -> None:
        """连接池归 Database 管，这里不关。"""
