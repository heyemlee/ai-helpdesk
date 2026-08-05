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
文档另有一个分类（category），只给工作台整理和浏览用，检索不看它——改分类
只改文档表的这一列，分片、向量和关键词索引都不动。

向量的来源：storage_meta 里记着库里的向量是哪个 Embedding 模型算的。换了模型
（哪怕维度一样）旧向量就不能再用——它们和新模型的查询向量不在一个空间里，
余弦算得出来，但和内容无关。check_embedding_signature 在启动时把关，
replace_embeddings 给 scripts/reembed.py 整批换向量用。
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
    created_at   REAL NOT NULL,             -- 导入时间
    category     TEXT NOT NULL DEFAULT ''   -- 大类的 key（见 knowledge_base.CATEGORIES）；空 = 未分类
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
CREATE TABLE IF NOT EXISTS storage_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL             -- 目前只有 embedding_signature 一条
);
"""

# 带向量列的两张表。知识库分片和情景记忆用的是同一个 Embedder，要换一起换。
_VECTOR_TABLES = ("kb_chunks", "memory_episodic")
EMBEDDING_SIGNATURE_KEY = "embedding_signature"


class VectorsChangedError(RuntimeError):
    """算新向量的这段时间里表里多了行：这次不换，重读一遍再算。"""


def _require_vectors(
    table: str, ids: set[str], vectors: dict[str, list[float]], dim: int
) -> None:
    """
    换向量之前的两道检查：表里每一行都得有新向量，新向量都得是说好的维度。

    读完全表去算向量的这段时间里如果又写进了新行，那几行就没有新向量——
    换上去之后它们还留着旧空间的向量。宁可整个不换，让调用方重跑。
    """
    missing = ids - set(vectors)
    if missing:
        raise VectorsChangedError(
            f"{table} 有 {len(missing)} 行没有对应的新向量（重算期间有新写入？），"
            "什么都没有改，重跑一次即可。"
        )
    wrong = next((len(v) for v in vectors.values() if len(v) != dim), None)
    if wrong is not None:
        raise RuntimeError(f"{table} 的新向量里有 {wrong} 维的，说好的是 {dim} 维，什么都没有改。")


# 文档列表返回的列。正文只取开头一段（列表页要它算摘要），整篇正文走详情。
DOC_HEAD_CHARS = 600
_DOC_COLUMNS = "id, title, origin, source_type, source_name, chunk_count, created_at, category"
# kb_doc_update 允许改的列。写死白名单：列名是拼进 SQL 的，不能来自调用方。
_DOC_UPDATABLE = (
    "title", "content", "content_hash", "origin", "source_type", "source_name", "chunk_count",
    "category",
)


def like_pattern(term: str) -> str:
    """把一个检索词变成 LIKE 的包含匹配模式，`%` `_` `\\` 按字面处理。"""
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def doc_filter_sql(
    terms: list[str],
    mark,
    category_in: list[str] | None = None,
    category_not_in: list[str] | None = None,
) -> tuple[str, str, list[Any]]:
    """
    文档筛选的 WHERE、ORDER BY 和参数，两个后端共用同一个形状。

    每个词都要在标题或正文里出现（AND）：这是"筛选"，词越多结果越少。
    标题里就能凑齐所有词的排前面——按标题找文档是最常见的用法。
    分类条件和词是 AND 的关系：category_in 只要这几类，category_not_in 除了这几类
    （"未分类"就是"不在任何一个现有大类里"，空串和已经撤掉的旧分类都算）。
    `mark(i)` 给出第 i 个参数（从 0 起）的占位符，SQLite 是 `?1`，Postgres 是 `$1`，
    两边都支持同一个参数引用多次。
    """
    params: list[Any] = [like_pattern(t) for t in terms]
    esc = "ESCAPE '\\'"
    conds = [
        f"(lower(title) LIKE {mark(i)} {esc} OR lower(content) LIKE {mark(i)} {esc})"
        for i in range(len(terms))
    ]
    for keys, op in ((category_in, "IN"), (category_not_in, "NOT IN")):
        if keys is None:
            continue
        if not keys:
            # IN () 是语法错误：空的"只要"什么都不要，空的"除了"什么都不排除
            if op == "IN":
                conds.append("1 = 0")
            continue
        marks = ", ".join(mark(len(params) + j) for j in range(len(keys)))
        conds.append(f"category {op} ({marks})")
        params.extend(keys)

    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    order_tail = "created_at DESC, title, id"
    if not terms:
        return where, order_tail, params
    title_all = " AND ".join(f"lower(title) LIKE {mark(i)} {esc}" for i in range(len(terms)))
    return where, f"CASE WHEN {title_all} THEN 0 ELSE 1 END, {order_tail}", params


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
            self._migrate_document_columns()
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

    def _migrate_document_columns(self) -> None:
        """
        给已存在的 kb_documents 补 category 列（加分类时加的）。

        补出来是空串，也就是"未分类"：已有文档原样留在库里，等人在工作台里归类。
        不按标题猜分类——猜错了的文档会躲进一个没人去找的大类里。
        """
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(kb_documents)")}
        if "category" not in cols:
            self._db.execute("ALTER TABLE kb_documents ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            logger.info("kb_documents 已补 category 列（已有文档都是未分类）")

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
        self, tokens: list[str], embedding: list[float] | None, top_k: int
    ) -> list[dict[str, Any]]:
        """
        BM25 关键词检索。分数为 0（无字面命中）的片段直接不返回。

        embedding 只用来顺带算余弦分；拿不到查询向量时传 None，score 就是 None
        ——关键词这一路本身不依赖它，Embedding 服务挂了照样能召回。
        """
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
                    "score": (cosine(embedding, json.loads(r["embedding"]))
                              if embedding is not None else None),
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
                    "  chunk_count, created_at, category)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc["id"], doc["title"], doc["content"], doc["content_hash"],
                     doc.get("origin", "original"), doc.get("source_type", ""),
                     doc.get("source_name", ""), doc.get("chunk_count", 0), doc["created_at"],
                     doc.get("category", "")),
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
        category_in: list[str] | None = None, category_not_in: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        文档列表，返回 (本页, 符合条件的总数)。

        terms 为空是浏览全部；非空时每个词都要在标题或正文里出现。
        分类条件见 doc_filter_sql。
        `chunks` 是现在真的挂在这篇文档下的分片数，和导入时记下的 chunk_count
        分开给——两个数对不上，就是有分片在库里被动过。
        """
        where, order, params = doc_filter_sql(
            terms, lambda i: f"?{i + 1}", category_in, category_not_in
        )
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

    async def kb_doc_category_counts(self, terms: list[str]) -> dict[str, int]:
        """按分类数文档（存的是什么就按什么数），筛选词和 kb_doc_list 同一个口径。"""
        where, _, params = doc_filter_sql(terms, lambda i: f"?{i + 1}")

        def _q() -> dict[str, int]:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT category, COUNT(*) AS n FROM kb_documents {where} GROUP BY category",
                    params,
                ).fetchall()
            return {r["category"]: r["n"] for r in rows}
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

    # ── 向量来源与重建 ────────────────────────────────────────────────────────

    async def meta_get(self, key: str) -> str | None:
        def _q() -> str | None:
            with self._lock:
                row = self._db.execute(
                    "SELECT value FROM storage_meta WHERE key = ?", (key,)
                ).fetchone()
            return row["value"] if row else None
        return await asyncio.to_thread(_q)

    async def meta_set(self, key: str, value: str) -> None:
        def _w() -> None:
            with self._lock:
                self._db.execute(
                    "INSERT INTO storage_meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
                self._db.commit()
        await asyncio.to_thread(_w)

    async def vector_state(self) -> dict[str, dict[str, int | None]]:
        """两张向量表各有多少行、向量现在是多少维（空表的维度是 None）。"""
        def _q() -> dict[str, dict[str, int | None]]:
            state: dict[str, dict[str, int | None]] = {}
            with self._lock:
                for table in _VECTOR_TABLES:
                    rows = self._db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                    sample = self._db.execute(f"SELECT embedding FROM {table} LIMIT 1").fetchone()
                    state[table] = {
                        "rows": rows,
                        "dim": len(json.loads(sample["embedding"])) if sample else None,
                    }
            return state
        return await asyncio.to_thread(_q)

    async def kb_embedding_rows(self) -> list[dict[str, Any]]:
        """全部分片的 id、标题和正文：重算向量的输入。"""
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, title, content FROM kb_chunks ORDER BY id"
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_q)

    async def episodic_embedding_rows(self) -> list[dict[str, Any]]:
        """全部情景记忆的 id 和摘要：向量是按摘要算的。"""
        def _q() -> list[dict[str, Any]]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, summary FROM memory_episodic ORDER BY id"
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_q)

    async def replace_embeddings(
        self, dim: int, kb: dict[str, list[float]], episodic: dict[str, list[float]],
        signature: str,
    ) -> None:
        """
        把两张表的向量整批换成新的，并记下新向量的来源。要么全换，要么一行不动。

        kb / episodic 是 {行 id: 新向量}。只换向量：标题、正文、关键词索引、
        文档归属都不碰。
        """
        def _w() -> None:
            with self._lock:
                try:
                    for table, vectors in (("kb_chunks", kb), ("memory_episodic", episodic)):
                        ids = {r["id"] for r in self._db.execute(f"SELECT id FROM {table}")}
                        _require_vectors(table, ids, vectors, dim)
                        self._db.executemany(
                            f"UPDATE {table} SET embedding = ? WHERE id = ?",
                            [(json.dumps(vec), row_id) for row_id, vec in vectors.items()],
                        )
                    self._db.execute(
                        "INSERT INTO storage_meta (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (EMBEDDING_SIGNATURE_KEY, signature),
                    )
                    self._db.commit()
                except BaseException:
                    self._db.rollback()
                    raise
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
        self, tokens: list[str], embedding: list[float] | None, top_k: int
    ) -> list[dict[str, Any]]:
        """
        tsvector 关键词检索，GIN 索引 + ts_rank_cd 排序。

        token 之间用 `|`（OR）而不是 `&`：客服问句里多数 token 是无关紧要的
        修饰语（"这个""怎么办"），AND 会让任何一个噪声 token 就把整条查询打空。
        排序交给 ts_rank_cd —— 命中得多、命中得密的片段自然排前面。

        embedding 为 None（拿不到查询向量）时 score 是 NULL，召回和排序不受影响。
        """
        if not tokens:
            return []
        rows = await self._db.fetch(
            "SELECT id, title, content, chunk_index, doc_id,"
            "       ts_rank_cd(tokens, q) AS lex_score,"
            "       1 - (embedding <=> $2::vector) AS score"
            " FROM kb_chunks, to_tsquery('simple', $1) AS q"
            " WHERE tokens @@ q ORDER BY lex_score DESC LIMIT $3",
            " | ".join(tokens), self._vec(embedding) if embedding is not None else None, top_k,
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
            "  chunk_count, created_at, category)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
            " ON CONFLICT (id) DO NOTHING RETURNING id",
            doc["id"], doc["title"], doc["content"], doc["content_hash"],
            doc.get("origin", "original"), doc.get("source_type", ""),
            doc.get("source_name", ""), doc.get("chunk_count", 0), doc["created_at"],
            doc.get("category", ""),
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
        category_in: list[str] | None = None, category_not_in: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where, order, params = doc_filter_sql(
            terms, lambda i: f"${i + 1}", category_in, category_not_in
        )
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

    async def kb_doc_category_counts(self, terms: list[str]) -> dict[str, int]:
        where, _, params = doc_filter_sql(terms, lambda i: f"${i + 1}")
        rows = await self._db.fetch(
            f"SELECT category, COUNT(*) AS n FROM kb_documents {where} GROUP BY category", *params
        )
        return {r["category"]: r["n"] for r in rows}

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

    # ── 向量来源与重建（与 LocalStore 同构，说明见那边）──────────────────────

    async def meta_get(self, key: str) -> str | None:
        row = await self._db.fetchrow("SELECT value FROM storage_meta WHERE key = $1", key)
        return row["value"] if row else None

    async def meta_set(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO storage_meta (key, value) VALUES ($1, $2)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value,
        )

    async def vector_state(self) -> dict[str, dict[str, int | None]]:
        state: dict[str, dict[str, int | None]] = {}
        async with self._db.transaction() as conn:
            for table in _VECTOR_TABLES:
                state[table] = {
                    "rows": await conn.fetchval(f"SELECT COUNT(*) FROM {table}"),
                    # 维度写在列类型里，空表也有
                    "dim": await self._db.vector_dim(conn, table),
                }
        return state

    async def kb_embedding_rows(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch("SELECT id, title, content FROM kb_chunks ORDER BY id")
        return [dict(r) for r in rows]

    async def episodic_embedding_rows(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch("SELECT id, summary FROM memory_episodic ORDER BY id")
        return [dict(r) for r in rows]

    async def replace_embeddings(
        self, dim: int, kb: dict[str, list[float]], episodic: dict[str, list[float]],
        signature: str,
    ) -> None:
        """
        一个事务里把两张表的向量列换成新维度、新向量。

        维度写在列类型里，改不了，只能加一列新的、填好、删掉旧的再改名。Postgres 的
        DDL 是事务性的：中途任何一步失败整个回滚，旧向量原样还在。事务期间两张表
        被独占锁住，读写都要等它提交。提交之后还按旧维度跑着的服务会在写入和稠密
        检索上报维度不符——换向量要配合改配置和重启，见 scripts/reembed.py。
        手工建过的向量索引会随旧列一起删掉，需要重建。
        """
        async with self._db.transaction() as conn:
            for table, vectors in (("kb_chunks", kb), ("memory_episodic", episodic)):
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN embedding_next vector({int(dim)})"
                )
                # 加列拿到了独占锁，这之后表里不会再多出行来
                ids = {r["id"] for r in await conn.fetch(f"SELECT id FROM {table}")}
                _require_vectors(table, ids, vectors, dim)
                if vectors:
                    await conn.executemany(
                        f"UPDATE {table} SET embedding_next = $2::vector WHERE id = $1",
                        [(row_id, self._vec(vec)) for row_id, vec in vectors.items()],
                    )
                await conn.execute(f"ALTER TABLE {table} DROP COLUMN embedding")
                await conn.execute(
                    f"ALTER TABLE {table} RENAME COLUMN embedding_next TO embedding"
                )
                await conn.execute(f"ALTER TABLE {table} ALTER COLUMN embedding SET NOT NULL")
            await conn.execute(
                "INSERT INTO storage_meta (key, value) VALUES ($1, $2)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                EMBEDDING_SIGNATURE_KEY, signature,
            )

    async def close(self) -> None:
        """连接池归 Database 管，这里不关。"""


async def check_embedding_signature(store: Any, embedder: Any) -> None:
    """
    库里的向量和当前的 Embedder 是不是同一个模型算的。不是就别起来。

    维度不同的情况 Postgres 的列类型会拦住（见 pg.Database._check_vector_dim），
    这里拦的是它拦不住的：维度一样、模型不一样，以及 SQLite 后端（向量存成 JSON，
    没有列类型可查）。两种都不会报错，只是语义检索从此返回和问题无关的片段。

    没有记录的库（加这道检查之前建的，或者全新的）记下当前的来源就放行。
    """
    current = embedder.signature
    recorded = await store.meta_get(EMBEDDING_SIGNATURE_KEY)
    if recorded == current:
        return
    if recorded is None:
        state = await store.vector_state()
        if embedder.backend == "remote" and any(t["rows"] for t in state.values()):
            # 没法倒推这些向量当初是谁算的。一直配着同一个远端模型的库不该被拦，
            # 所以放行；但如果是刚从本地向量换过来、维度又恰好相同，就得重建。
            logger.warning(
                f"库里已有向量，但没有记录它们是哪个模型生成的；按当前配置 {current} 记下。"
                "如果换过 Embedding 模型，请运行 scripts/reembed.py --apply 重建向量。"
            )
        await store.meta_set(EMBEDDING_SIGNATURE_KEY, current)
        return
    raise RuntimeError(
        f"库里的向量是 {recorded} 生成的，当前的 Embedding 配置是 {current}。"
        f"两者不在同一个向量空间里，直接用会让语义检索返回和问题无关的片段：\n"
        f"  python scripts/reembed.py          # 先看要重建什么\n"
        f"  python scripts/reembed.py --apply  # 重算向量并换上，文档内容不动\n"
        f"要退回原来的模型，把 EMBEDDING_* 改回去即可。"
    )
