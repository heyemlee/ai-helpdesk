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

文档与分片：检索的单位是分片（kb_chunks），人读的单位是文档（kb_documents）。
文档表只存"导入时拿到了什么"——标题、原文、来源、导入时间——分片用 doc_id
指回去。升级前写入的分片 doc_id 为空，由 KnowledgeBase.ensure_documents 归档
成"历史"文档；那一步只用这里的原子操作拼出来，两个后端走的是同一段逻辑。
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

from app.core.embedder import cosine
from app.core.lexical import bm25_scores

if TYPE_CHECKING:
    from app.storage.pg import Database

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
    tokens       TEXT NOT NULL DEFAULT '',  -- 空格分隔的稀疏检索 token
    doc_id       TEXT NOT NULL DEFAULT ''   -- 所属文档；空 = 升级前写入、尚未归档
);
CREATE TABLE IF NOT EXISTS kb_documents (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,             -- 导入时的原文；历史文档是由分片拼回来的
    content_hash TEXT NOT NULL,             -- sha256(标题 + 正文)，重复导入靠它幂等
    origin       TEXT NOT NULL DEFAULT 'original',  -- original / legacy / legacy_partial
    source_type  TEXT NOT NULL DEFAULT '',  -- manual / upload / seed / legacy
    source_name  TEXT NOT NULL DEFAULT '',  -- 上传的文件名；没有就是空
    chunk_count  INTEGER NOT NULL DEFAULT 0,  -- 导入时切成了几片
    created_at   REAL NOT NULL              -- 导入时间
);
CREATE INDEX IF NOT EXISTS idx_kb_docs_hash ON kb_documents (content_hash);
CREATE INDEX IF NOT EXISTS idx_kb_docs_created ON kb_documents (created_at);
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


# 文档列表返回的列。正文只取开头一段（列表页要它算摘要），整篇正文走详情。
DOC_HEAD_CHARS = 600
_DOC_COLUMNS = "id, title, origin, source_type, source_name, chunk_count, created_at"
# kb_doc_update 允许改的列。写死白名单：列名是拼进 SQL 的，不能来自调用方。
_DOC_UPDATABLE = ("title", "content", "content_hash", "origin", "source_type", "source_name", "chunk_count")


def like_pattern(term: str) -> str:
    """把一个检索词变成 LIKE 的包含匹配模式，`%` `_` `\\` 按字面处理。"""
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def doc_filter_sql(terms: list[str], mark) -> tuple[str, str]:
    """
    文档筛选的 WHERE 和 ORDER BY，两个后端共用同一个形状。

    每个词都要在标题或正文里出现（AND）：这是"筛选"，词越多结果越少。
    标题里就能凑齐所有词的排前面——按标题找文档是最常见的用法。
    `mark(i)` 给出第 i 个参数（从 0 起）的占位符，SQLite 是 `?1`，Postgres 是 `$1`，
    两边都支持同一个参数引用多次。
    """
    order_tail = "created_at DESC, title, id"
    if not terms:
        return "", order_tail
    esc = "ESCAPE '\\'"
    where = " AND ".join(
        f"(lower(title) LIKE {mark(i)} {esc} OR lower(content) LIKE {mark(i)} {esc})"
        for i in range(len(terms))
    )
    title_all = " AND ".join(f"lower(title) LIKE {mark(i)} {esc}" for i in range(len(terms)))
    return f"WHERE {where}", f"CASE WHEN {title_all} THEN 0 ELSE 1 END, {order_tail}"


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
            self._migrate_chunk_columns()
            self._db.commit()

    def _migrate_chunk_columns(self) -> None:
        """
        给已存在的 kb_chunks 补后来才加的列。

        CREATE TABLE IF NOT EXISTS 对已有表是空操作，所以老的 vector_store.db
        里没有这些列：
          - tokens：升级到混合检索时加的，没有它稀疏检索直接报 no such column。
            补出来是空值，由 KnowledgeBase.backfill_lexical 在启动时回填。
          - doc_id：加文档表时加的。补出来是空值，表示"还不知道属于哪篇文档"，
            由 KnowledgeBase.ensure_documents 归档。已有的分片和向量一行不动。
        doc_id 的索引也在这里建，不放进建表脚本：脚本在补列之前执行，
        对着一张还没有 doc_id 的老表建索引会失败。
        """
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(kb_chunks)")}
        if "tokens" not in cols:
            self._db.execute("ALTER TABLE kb_chunks ADD COLUMN tokens TEXT NOT NULL DEFAULT ''")
            logger.info("kb_chunks 已补 tokens 列（稀疏检索索引待回填）")
        if "doc_id" not in cols:
            self._db.execute("ALTER TABLE kb_chunks ADD COLUMN doc_id TEXT NOT NULL DEFAULT ''")
            logger.info("kb_chunks 已补 doc_id 列（历史分片待归档成文档）")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks (doc_id)")

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
        """chunks: [{id,title,content,chunk_index,total_chunks,ingested_at,embedding,tokens,doc_id}]"""
        def _w() -> None:
            with self._lock:
                self._db.executemany(
                    "INSERT OR IGNORE INTO kb_chunks"
                    " (id, title, content, chunk_index, total_chunks, ingested_at, embedding,"
                    "  tokens, doc_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(c["id"], c["title"], c["content"], c["chunk_index"],
                      c["total_chunks"], c["ingested_at"], json.dumps(c["embedding"]),
                      c.get("tokens", ""), c.get("doc_id", ""))
                     for c in chunks],
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def kb_search(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, title, content, chunk_index, doc_id, embedding FROM kb_chunks"
                ).fetchall()
            scored = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "chunk_index": r["chunk_index"],
                    "doc_id": r["doc_id"],
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
                    "SELECT id, title, content, chunk_index, doc_id, embedding, tokens"
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
                    "doc_id": r["doc_id"],
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

    # ── 文档 ──────────────────────────────────────────────────────────────────

    async def kb_doc_add(self, doc: dict[str, Any]) -> bool:
        """登记一篇文档。id 已存在时不覆盖，返回 False。"""
        def _w() -> bool:
            with self._lock:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO kb_documents"
                    " (id, title, content, content_hash, origin, source_type, source_name,"
                    "  chunk_count, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc["id"], doc["title"], doc["content"], doc["content_hash"],
                     doc.get("origin", "original"), doc.get("source_type", ""),
                     doc.get("source_name", ""), doc.get("chunk_count", 0), doc["created_at"]),
                )
                self._db.commit()
                return cur.rowcount > 0
        return await asyncio.to_thread(_w)

    async def kb_doc_get(self, doc_id: str) -> dict[str, Any] | None:
        def _q() -> dict[str, Any] | None:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {_DOC_COLUMNS}, content, content_hash, length(content) AS chars,"
                    " (SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id = d.id) AS chunks"
                    " FROM kb_documents d WHERE id = ?",
                    (doc_id,),
                ).fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(_q)

    async def kb_doc_find_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        def _q() -> dict[str, Any] | None:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {_DOC_COLUMNS},"
                    " (SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id = d.id) AS chunks"
                    " FROM kb_documents d WHERE content_hash = ?"
                    " ORDER BY created_at, id LIMIT 1",
                    (content_hash,),
                ).fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(_q)

    async def kb_doc_list(
        self, terms: list[str], limit: int, offset: int = 0, with_content: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        文档列表，返回 (本页, 符合条件的总数)。

        terms 为空是浏览全部；非空时每个词都要在标题或正文里出现。
        `chunks` 是现在真的挂在这篇文档下的分片数，和导入时记下的 chunk_count
        分开给——两个数对不上，就是有分片在库里被动过。
        """
        where, order = doc_filter_sql(terms, lambda i: f"?{i + 1}")
        params = [like_pattern(t) for t in terms]
        body = ", content" if with_content else ""

        def _q() -> tuple[list[dict[str, Any]], int]:
            with self._lock:
                total = self._db.execute(
                    f"SELECT COUNT(*) AS n FROM kb_documents {where}", params
                ).fetchone()["n"]
                rows = self._db.execute(
                    f"SELECT {_DOC_COLUMNS}, substr(content, 1, {DOC_HEAD_CHARS}) AS head,"
                    f" length(content) AS chars{body},"
                    " (SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id = d.id) AS chunks"
                    f" FROM kb_documents d {where} ORDER BY {order}"
                    f" LIMIT ?{len(params) + 1} OFFSET ?{len(params) + 2}",
                    [*params, limit, offset],
                ).fetchall()
            return [dict(r) for r in rows], total
        return await asyncio.to_thread(_q)

    async def kb_doc_briefs(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        """按 id 取标题和正文开头，给检索结果配上"出自哪篇文档"。"""
        if not doc_ids:
            return []

        def _q() -> list[dict[str, Any]]:
            marks = ",".join("?" * len(doc_ids))
            with self._lock:
                rows = self._db.execute(
                    f"SELECT {_DOC_COLUMNS}, substr(content, 1, {DOC_HEAD_CHARS}) AS head"
                    f" FROM kb_documents WHERE id IN ({marks})",
                    doc_ids,
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_q)

    async def kb_doc_update(self, doc_id: str, fields: dict[str, Any]) -> None:
        cols = [c for c in _DOC_UPDATABLE if c in fields]
        if not cols:
            return

        def _w() -> None:
            with self._lock:
                self._db.execute(
                    f"UPDATE kb_documents SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                    [*(fields[c] for c in cols), doc_id],
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def kb_doc_delete(self, doc_id: str) -> None:
        """删掉一篇文档和它名下的分片。只用于收拾没导完的半成品和空壳记录。"""
        def _w() -> None:
            with self._lock:
                self._db.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
                self._db.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
                self._db.commit()
        await asyncio.to_thread(_w)

    async def kb_doc_stats(self) -> dict[str, int]:
        def _q() -> dict[str, int]:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) AS total,"
                    " COALESCE(SUM(CASE WHEN origin <> 'original' THEN 1 ELSE 0 END), 0) AS legacy"
                    " FROM kb_documents"
                ).fetchone()
            return {"total": row["total"], "legacy": row["legacy"]}
        return await asyncio.to_thread(_q)

    async def kb_doc_chunks(self, doc_id: str) -> list[dict[str, Any]]:
        """一篇文档名下的分片，按原文顺序。`indexed` 表示有没有关键词索引。"""
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, chunk_index, total_chunks, content, ingested_at,"
                    " (tokens <> '') AS indexed"
                    " FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index, id",
                    (doc_id,),
                ).fetchall()
            return [{**dict(r), "indexed": bool(r["indexed"])} for r in rows]
        return await asyncio.to_thread(_q)

    async def kb_chunks_unfiled(self) -> list[dict[str, Any]]:
        """
        还没有归到任何文档下的分片：doc_id 为空（升级前写入的），
        或者指向一篇不存在的文档（归档做到一半进程退出）。
        """
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, title, content, chunk_index, total_chunks, ingested_at, doc_id"
                    " FROM kb_chunks c WHERE doc_id = ''"
                    " OR NOT EXISTS (SELECT 1 FROM kb_documents d WHERE d.id = c.doc_id)"
                    " ORDER BY title, ingested_at, total_chunks, chunk_index, id"
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_q)

    async def kb_assign_doc(self, chunk_ids: list[str], doc_id: str) -> None:
        """把一组分片归到一篇文档下。"""
        if not chunk_ids:
            return

        def _w() -> None:
            with self._lock:
                # SQLite 单条语句的参数个数有上限，按 500 一批
                for at in range(0, len(chunk_ids), 500):
                    batch = chunk_ids[at:at + 500]
                    self._db.execute(
                        f"UPDATE kb_chunks SET doc_id = ? WHERE id IN ({','.join('?' * len(batch))})",
                        [doc_id, *batch],
                    )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def kb_chunk_owners(self, chunk_ids: list[str]) -> dict[str, str]:
        """分片 id → 所属文档 id（只返回存在的分片）。"""
        if not chunk_ids:
            return {}

        def _q() -> dict[str, str]:
            marks = ",".join("?" * len(chunk_ids))
            with self._lock:
                rows = self._db.execute(
                    f"SELECT id, doc_id FROM kb_chunks WHERE id IN ({marks})", chunk_ids
                ).fetchall()
            return {r["id"]: r["doc_id"] for r in rows}
        return await asyncio.to_thread(_q)

    async def kb_docs_without_chunks(self, created_before: float) -> list[str]:
        """名下一个分片都没有的文档记录（只看 created_before 之前登记的）。"""
        def _q() -> list[str]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id FROM kb_documents d WHERE created_at < ?"
                    " AND NOT EXISTS (SELECT 1 FROM kb_chunks c WHERE c.doc_id = d.id)",
                    (created_before,),
                ).fetchall()
            return [r["id"] for r in rows]
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
            " (id, title, content, chunk_index, total_chunks, ingested_at, embedding, tokens,"
            "  doc_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7::vector, to_tsvector('simple', $8), $9)"
            " ON CONFLICT (id) DO NOTHING",
            [(c["id"], c["title"], c["content"], c["chunk_index"],
              c["total_chunks"], c["ingested_at"], self._vec(c["embedding"]),
              c.get("tokens", ""), c.get("doc_id", ""))
             for c in chunks],
        )

    async def kb_search(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            "SELECT id, title, content, chunk_index, doc_id,"
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
            "SELECT id, title, content, chunk_index, doc_id,"
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

    # ── 文档（与 LocalStore 同构，说明见那边）────────────────────────────────

    async def kb_doc_add(self, doc: dict[str, Any]) -> bool:
        # ON CONFLICT DO NOTHING + RETURNING：冲突时不返回行。两个实例同时启动、
        # 同时给同一批历史分片归档时，后到的那个在这里落空，不会写出第二份。
        row = await self._db.fetchrow(
            "INSERT INTO kb_documents"
            " (id, title, content, content_hash, origin, source_type, source_name,"
            "  chunk_count, created_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
            " ON CONFLICT (id) DO NOTHING RETURNING id",
            doc["id"], doc["title"], doc["content"], doc["content_hash"],
            doc.get("origin", "original"), doc.get("source_type", ""),
            doc.get("source_name", ""), doc.get("chunk_count", 0), doc["created_at"],
        )
        return row is not None

    async def kb_doc_get(self, doc_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            f"SELECT {_DOC_COLUMNS}, content, content_hash, length(content) AS chars,"
            " (SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id = d.id) AS chunks"
            " FROM kb_documents d WHERE id = $1",
            doc_id,
        )
        return dict(row) if row else None

    async def kb_doc_find_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            f"SELECT {_DOC_COLUMNS},"
            " (SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id = d.id) AS chunks"
            " FROM kb_documents d WHERE content_hash = $1"
            " ORDER BY created_at, id LIMIT 1",
            content_hash,
        )
        return dict(row) if row else None

    async def kb_doc_list(
        self, terms: list[str], limit: int, offset: int = 0, with_content: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        where, order = doc_filter_sql(terms, lambda i: f"${i + 1}")
        params = [like_pattern(t) for t in terms]
        body = ", content" if with_content else ""
        total = await self._db.fetchrow(
            f"SELECT COUNT(*) AS n FROM kb_documents {where}", *params
        )
        rows = await self._db.fetch(
            f"SELECT {_DOC_COLUMNS}, substr(content, 1, {DOC_HEAD_CHARS}) AS head,"
            f" length(content) AS chars{body},"
            " (SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id = d.id) AS chunks"
            f" FROM kb_documents d {where} ORDER BY {order}"
            f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
            *params, limit, offset,
        )
        return [dict(r) for r in rows], total["n"]

    async def kb_doc_briefs(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        rows = await self._db.fetch(
            f"SELECT {_DOC_COLUMNS}, substr(content, 1, {DOC_HEAD_CHARS}) AS head"
            " FROM kb_documents WHERE id = ANY($1::text[])",
            doc_ids,
        )
        return [dict(r) for r in rows]

    async def kb_doc_update(self, doc_id: str, fields: dict[str, Any]) -> None:
        cols = [c for c in _DOC_UPDATABLE if c in fields]
        if not cols:
            return
        sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        await self._db.execute(
            f"UPDATE kb_documents SET {sets} WHERE id = $1",
            doc_id, *(fields[c] for c in cols),
        )

    async def kb_doc_delete(self, doc_id: str) -> None:
        await self._db.execute("DELETE FROM kb_chunks WHERE doc_id = $1", doc_id)
        await self._db.execute("DELETE FROM kb_documents WHERE id = $1", doc_id)

    async def kb_doc_stats(self) -> dict[str, int]:
        row = await self._db.fetchrow(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(CASE WHEN origin <> 'original' THEN 1 ELSE 0 END), 0) AS legacy"
            " FROM kb_documents"
        )
        return {"total": row["total"], "legacy": row["legacy"]}

    async def kb_doc_chunks(self, doc_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            "SELECT id, chunk_index, total_chunks, content, ingested_at,"
            " (tokens IS NOT NULL AND tokens <> ''::tsvector) AS indexed"
            " FROM kb_chunks WHERE doc_id = $1 ORDER BY chunk_index, id",
            doc_id,
        )
        return [dict(r) for r in rows]

    async def kb_chunks_unfiled(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            "SELECT id, title, content, chunk_index, total_chunks, ingested_at, doc_id"
            " FROM kb_chunks c WHERE doc_id = ''"
            " OR NOT EXISTS (SELECT 1 FROM kb_documents d WHERE d.id = c.doc_id)"
            " ORDER BY title, ingested_at, total_chunks, chunk_index, id"
        )
        return [dict(r) for r in rows]

    async def kb_assign_doc(self, chunk_ids: list[str], doc_id: str) -> None:
        if not chunk_ids:
            return
        # 一条语句归完一整篇：另一个实例此刻读到的要么是全没归、要么是全归了，
        # 不会看到半篇——半篇会被当成"不完整的历史分片"另立一篇文档。
        await self._db.execute(
            "UPDATE kb_chunks SET doc_id = $1 WHERE id = ANY($2::text[])", doc_id, chunk_ids
        )

    async def kb_chunk_owners(self, chunk_ids: list[str]) -> dict[str, str]:
        if not chunk_ids:
            return {}
        rows = await self._db.fetch(
            "SELECT id, doc_id FROM kb_chunks WHERE id = ANY($1::text[])", chunk_ids
        )
        return {r["id"]: r["doc_id"] for r in rows}

    async def kb_docs_without_chunks(self, created_before: float) -> list[str]:
        rows = await self._db.fetch(
            "SELECT id FROM kb_documents d WHERE created_at < $1"
            " AND NOT EXISTS (SELECT 1 FROM kb_chunks c WHERE c.doc_id = d.id)",
            created_before,
        )
        return [r["id"] for r in rows]

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
