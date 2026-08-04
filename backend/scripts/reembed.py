#!/usr/bin/env python3
"""
换 Embedding 模型之后，重建库里的向量。

什么时候用：改了 EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM——包括从本地
n-gram 向量换成远端模型，或者换回来。库里的向量是旧模型算的，和新模型的查询向量
不在同一个空间里；服务启动时会因为维度或来源对不上而拒绝启动，并指到这里来。

做什么：把知识库分片（kb_chunks）和情景记忆（memory_episodic）的向量用当前配置的
模型重算一遍，一次换上。**只换向量**——文档原文、分片、关键词索引、记忆摘要都不动，
不需要重新导入任何东西。

怎么保证不把库弄坏：
  - 默认只看不改：打印现状和计划，并向 Embedding 要一个向量，确认它是通的、
    维度和 EMBEDDING_DIM 一致（配了远端的话这是一次真实调用）。
  - 加 --apply 才动手。先把全部新向量算完——任何一段算不出来就停，这时库还没碰；
    再整批换上，Postgres 是一个事务，中途失败整个回滚。
  - 算向量期间库里如果又写进了新行，换的时候会发现并放弃，重跑一次即可。

步骤（Docker 部署）：
  1. 在 backend/.env 里改好 EMBEDDING_*，EMBEDDING_DIM 要和模型实际输出的维度一致；
  2. 停掉应用实例。它们还按旧配置跑着，向量换完之后写入和语义检索都会报维度不符；
  3. python scripts/reembed.py           # 看计划
     python scripts/reembed.py --apply   # 执行
  4. 启动应用实例，再跑一遍 scripts/eval_retrieval.py --embedding env 看检索效果，
     并按它报的分布重新定 RAG_EVIDENCE_MIN_SCORE。

连哪个库：默认和服务一样——配了 POSTGRES_DSN 就是它，没配就是 VECTOR_STORE_DB_PATH
指的 SQLite 文件。也可以用 --dsn / --sqlite 明确指定。

用法：
    python scripts/reembed.py
    python scripts/reembed.py --apply
    python scripts/reembed.py --sqlite ./data/vector_store.db --apply
"""
import argparse
import asyncio
import os
import pathlib
import sys
from urllib.parse import urlsplit

_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from app.core.embedder import Embedder, EmbeddingUnavailable  # noqa: E402
from app.storage.vector_store import (  # noqa: E402
    EMBEDDING_SIGNATURE_KEY,
    LocalStore,
    PgStore,
)
from app.tools.knowledge_base import KnowledgeBase  # noqa: E402

# 一次交给 Embedder 多少段。它内部还会按 EMBEDDING_BATCH_SIZE 拆成请求；
# 这里分段只是为了能报进度。
PROGRESS_STEP = 100


async def _open(args: argparse.Namespace, dim: int):
    """按参数或环境变量打开存储，返回 (store, close, 描述)。"""
    dsn = (args.dsn or ("" if args.sqlite else os.getenv("POSTGRES_DSN", ""))).strip()
    if dsn:
        from app.storage.pg import Database

        # 不校验维度：连上来就是为了处理维度对不上的情况
        db = Database(dsn, max_size=2)
        await db.connect(embedding_dim=dim, verify_dim=False)
        parts = urlsplit(dsn)
        where = f"postgres {parts.hostname or 'localhost'}:{parts.port or 5432}{parts.path}"
        return PgStore(db), db.close, where

    path = args.sqlite or os.getenv(
        "VECTOR_STORE_DB_PATH", str(_ROOT / "data" / "vector_store.db")
    )
    if not pathlib.Path(path).is_file():
        raise SystemExit(f"找不到 SQLite 文件 {path}。用 --sqlite 指定，或用 --dsn 指向 Postgres。")
    store = LocalStore(db_path=path)
    return store, store.close, f"sqlite {path}"


async def _embed_all(embedder: Embedder, texts: list[str], label: str) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), PROGRESS_STEP):
        vectors.extend(await embedder.embed_many(texts[start:start + PROGRESS_STEP]))
        print(f"  {label}: {len(vectors)}/{len(texts)}", flush=True)
    return vectors


async def main(args: argparse.Namespace) -> int:
    embedder = Embedder.from_env()
    store, close, where = await _open(args, embedder.dim)
    try:
        state = await store.vector_state()
        recorded = await store.meta_get(EMBEDDING_SIGNATURE_KEY)
        target = embedder.signature

        print(f"库: {where}")
        print(f"现在的向量: {recorded or '没有记录是哪个模型生成的'}")
        for table, info in state.items():
            dim = f"{info['dim']} 维" if info["dim"] else "空表"
            print(f"  {table:<16} {info['rows']:>6} 行  {dim}")
        print(f"要换成:     {target}（{embedder.backend}）")

        same_dim = all(info["dim"] in (None, embedder.dim) for info in state.values())
        if recorded == target and same_dim:
            print("\n库里的向量就是当前配置生成的，不需要重建。")
            return 0
        if recorded is None and same_dim:
            print("\n注意：库里没有记录向量的来源，维度和当前配置一致。如果这些向量本来就是"
                  "当前模型算的，不用重建——服务启动时会把来源补记上。")

        try:
            probe = await embedder.embed("维度探测")
        except EmbeddingUnavailable as ex:
            print(f"\nEmbedding 调不通，或者返回的维度和 EMBEDDING_DIM 不一致：{ex}")
            print("库没有任何改动。")
            return 1
        print(f"\nEmbedding 可用，返回 {len(probe)} 维。")

        total = sum(info["rows"] for info in state.values())
        if not args.apply:
            print(f"计划：重算 {total} 个向量并换上。以上只是计划，没有改动；加 --apply 执行。")
            return 0

        kb_rows = await store.kb_embedding_rows()
        episodic_rows = await store.episodic_embedding_rows()
        print(f"\n重算 {len(kb_rows) + len(episodic_rows)} 个向量……")
        try:
            kb_vectors = await _embed_all(
                embedder,
                [KnowledgeBase.embedding_text(r["title"], r["content"]) for r in kb_rows],
                "知识库分片",
            )
            # 情景记忆的向量是按摘要算的，见 MemoryManager._store_episodic
            episodic_vectors = await _embed_all(
                embedder, [r["summary"] for r in episodic_rows], "情景记忆"
            )
        except EmbeddingUnavailable as ex:
            print(f"\n算到一半 Embedding 不可用了：{ex}")
            print("库没有任何改动。恢复后重跑即可。")
            return 1

        await store.replace_embeddings(
            embedder.dim,
            {r["id"]: vec for r, vec in zip(kb_rows, kb_vectors, strict=True)},
            {r["id"]: vec for r, vec in zip(episodic_rows, episodic_vectors, strict=True)},
            target,
        )
        print(f"\n完成：两张表的向量已换成 {target}。")
        print("现在可以启动应用实例了；还按旧配置跑着的实例需要重启。")
        return 0
    finally:
        await close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="换 Embedding 模型后重建库里的向量")
    parser.add_argument("--apply", action="store_true", help="真的执行。不加只打印计划")
    parser.add_argument("--dsn", default="", help="Postgres 连接串，默认读 POSTGRES_DSN")
    parser.add_argument("--sqlite", default="", help="SQLite 文件路径，给了就不看 POSTGRES_DSN")
    sys.exit(asyncio.run(main(parser.parse_args())))
