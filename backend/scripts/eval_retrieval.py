#!/usr/bin/env python3
"""
检索层评测：稠密 / 稀疏 / 混合三种召回策略的对照。

为什么要单独测检索层：run_eval.py 测的是端到端答案质量，LLM 会把检索的
好坏吸收掉一部分——检索排第 3 和排第 1，只要都进了 top_k，答案可能一样。
检索退化因此很难从端到端分数上看出来。这个脚本只测"正确的那篇文档排第几"，
不调用任何生成模型，所以它跑得快、结果确定，可以在每次改检索链路后跑一遍。

指标：
  MRR    正确文档排名的倒数平均。衡量排序质量，排第 1 和排第 3 有区别。
  hit@3  正确文档是否进了前 3。**这是真正决定答案的指标**——/chat 只把
         top-3 片段喂给 Agent，排第 4 和排第 40 对答案的影响完全一样。

用法：
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --top-k 5
"""
import argparse
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from core.embedder import Embedder  # noqa: E402
from core.lexical import query_tokens  # noqa: E402
from mcp.knowledge_base import KnowledgeBase, rrf_fuse  # noqa: E402
from storage.vector_store import LocalStore  # noqa: E402

# (查询, 应当召回的文档标题)。标题取自 knowledge_base._DEFAULT_DOCS。
# 用词刻意贴近真实客服问句：口语、省略主语、混着领域专名。
CASES = [
    ("补件需要提供什么材料",   "缺件漏发与补件流程"),
    ("柜门关不严怎么办",       "安装与常见现场问题"),
    ("标准交期是几周",         "交期说明"),
    ("五金件质保多久",         "保修范围与除外情形"),
    ("已经排产了还能改尺寸吗", "报价构成与变更加价"),
    ("量尺需要什么条件",       "定制流程与节点"),
    ("台面拼缝多宽算正常",     "安装与常见现场问题"),
    ("定金交了多久开始排产",   "定制流程与节点"),
]


def _rank_of(rows: list[dict], title: str) -> int:
    for i, row in enumerate(rows, start=1):
        if row["title"] == title:
            return i
    return 0   # 完全没召回


async def main(top_k: int, recall_k: int) -> int:
    embedder = Embedder.from_env()
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalStore(db_path=f"{tmp}/eval_retrieval.db")
        kb = KnowledgeBase(store=store, embedder=embedder)
        await kb.ensure_seeded()

        print(f"语料: {await kb.doc_count_async()} 个片段 | "
              f"Embedding: {embedder.backend} {embedder.dim} 维 | hit@{top_k}\n")
        print(f"{'查询':<24}{'目标文档':<20}{'dense':>7}{'lexical':>9}{'hybrid':>8}")
        print("-" * 68)

        ranks: dict[str, list[int]] = {"dense": [], "lexical": [], "hybrid": []}
        for query, gold in CASES:
            embedding = await embedder.embed(query)
            dense = await store.kb_search(embedding, recall_k)
            lexical = await store.kb_search_lexical(query_tokens(query), embedding, recall_k)
            hybrid = rrf_fuse({"dense": dense, "lexical": lexical}, top_k=recall_k)

            row = {"dense": _rank_of(dense, gold), "lexical": _rank_of(lexical, gold),
                   "hybrid": _rank_of(hybrid, gold)}
            for name, rank in row.items():
                ranks[name].append(rank)
            print(f"{query:<24}{gold:<20}"
                  + "".join(f"{(r or '—')!s:>{w}}" for r, w in
                            ((row['dense'], 7), (row['lexical'], 9), (row['hybrid'], 8))))

        print("-" * 68)
        n = len(CASES)
        best_hit = max(sum(1 for r in v if 0 < r <= top_k) for v in ranks.values())
        for name, rs in ranks.items():
            mrr = sum(1.0 / r for r in rs if r) / n
            hit = sum(1 for r in rs if 0 < r <= top_k)
            flag = "  ← 最优" if hit == best_hit else ""
            print(f"{name:<10} MRR={mrr:.3f}   hit@{top_k}={hit}/{n}{flag}")

        await store.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="检索层评测：dense / lexical / hybrid 对照")
    parser.add_argument("--top-k", type=int, default=3, help="hit@K 的 K，默认 3（与 /chat 一致）")
    parser.add_argument("--recall-k", type=int, default=10, help="每路召回深度，默认 10")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.top_k, args.recall_k)))
