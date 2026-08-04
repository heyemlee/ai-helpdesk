#!/usr/bin/env python3
"""
检索层评测：稠密、稀疏两路各自的名次，以及两种融合方式的对照。

为什么要单独测检索层：run_eval.py 测的是端到端答案质量，LLM 会把检索的
好坏吸收掉一部分——检索排第 3 和排第 1，只要都进了 top_k，答案可能一样。
检索退化因此很难从端到端分数上看出来。这个脚本只测"正确的那篇文档排第几"，
不调用任何生成模型，所以它跑得快、结果确定，可以在每次改检索链路后跑一遍。

指标：
  MRR    正确文档排名的倒数平均。衡量排序质量，排第 1 和排第 3 有区别。
  hit@3  正确文档是否进了前 3。**这是真正决定答案的指标**——/chat 只把
         top-3 片段喂给 Agent，排第 4 和排第 40 对答案的影响完全一样。

四列：
  dense    只用稠密一路。
  lexical  只用关键词一路。
  rrf      两路按 RRF 融合（两路各一票）。
  served   服务实际返回的排序，也就是 KnowledgeBase.search_async 用的那一种：
           配了真正的 Embedding 模型时就是 rrf；用本地 n-gram 向量时是
           "关键词在前、向量补位"（knowledge_base.lexical_first_fuse）。
           后面的片段级命中、旧版本抢先、证据阈值，看的都是这一列。

两套用例（--suite）：
  ca     默认。加州橱柜工厂背景的仿真知识库：内置的 6 篇 + data/demo_docs/
         sample_ca_factory_knowledge.json，用例在 data/eval/retrieval_cases.json。
         用例分七类，分开报数——合在一起的总分会把"英文问中文文档全军覆没"
         和"字面贴近的都答对了"抹成一个不上不下的数：
           direct 字面贴近 / paraphrase 口语换说法 / code 型号与专名 /
           cross 英文问中文文档 / multi 要两篇都召回 / version 新旧版本并存 /
           unanswerable 知识库里没有答案。
  smoke  最初那 8 条用例、6 个片段，docs/evaluation/retrieval-smoke.md 记的是它。

两个后端（--backend）：
  sqlite    默认。临时文件，稀疏这一路是纯 Python BM25。
  postgres  生产用的后端，稀疏这一路是 tsvector + ts_rank_cd。两边的打分函数
            不是一回事（ts_rank_cd 不看一个词在语料里有多稀有），SQLite 上的数
            不能替 Postgres 说话。评测在一个临时 schema 里建表、导入、跑完删掉，
            不读也不写库里已有的表——所以指向正在用的库也不会碰到它的数据。

Embedding（--embedding）：
  local  默认。强制用本地字符 n-gram 向量，不发任何网络请求，哪怕 .env 里配了远端。
  env    按 EMBEDDING_* 配置走。配了远端就会真的调用它：语料每个片段一次、
         每条查询一次。

除了名次，ca 这套还报两件事：
  - 片段级命中：长文档会被切成几片，召回了那篇文档不等于召回了有答案的那一片。
    用例里的 evidence 是答案所在的原文，片段级只认包含它的那一片。
  - 证据阈值：重排没有给出"证据够不够"的判定时（cross-encoder 重排就给不出），
    ToolManager 退回"前 K 条里最高的余弦分 >= RAG_EVIDENCE_MIN_SCORE"。这个分数
    的分布完全取决于 Embedding 模型，换模型后阈值要重标——这里给出两组分数的
    分布和区分度，照着它定。

用法：
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --suite smoke
    python scripts/eval_retrieval.py --verbose --json /tmp/retrieval.json
    python scripts/eval_retrieval.py --backend postgres \\
        --dsn postgresql://user:pass@127.0.0.1:5433/dbname
"""
import argparse
import asyncio
import json
import os
import pathlib
import secrets
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from app.core.embedder import Embedder  # noqa: E402
from app.core.lexical import query_tokens  # noqa: E402
from app.storage.vector_store import LocalStore  # noqa: E402
from app.tools.knowledge_base import KnowledgeBase, fuse_candidates  # noqa: E402
from app.tools.tool_manager import EVIDENCE_MIN_SCORE  # noqa: E402

CORPUS_PATH = _ROOT / "data" / "demo_docs" / "sample_ca_factory_knowledge.json"
CASES_PATH = _ROOT / "data" / "eval" / "retrieval_cases.json"

LEGS = ("dense", "lexical", "rrf", "served")

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


@dataclass
class Case:
    id: str
    type: str
    lang: str
    query: str
    gold: list[dict[str, str]]          # [{title, evidence?}]；空 = 知识库里没有答案
    match_all: bool = False             # True：gold 里的文档都要进前 K 才算命中
    stale: list[str] = field(default_factory=list)   # 已废止的旧版文档标题

    @property
    def answerable(self) -> bool:
        return bool(self.gold)


def _smoke_cases() -> list[Case]:
    return [
        Case(id=f"smoke-{i:02d}", type="direct", lang="zh", query=query, gold=[{"title": gold}])
        for i, (query, gold) in enumerate(CASES, start=1)
    ]


def _load_cases(path: pathlib.Path) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Case(
            id=c["id"], type=c["type"], lang=c.get("lang", "zh"), query=c["query"],
            gold=list(c.get("gold") or []), match_all=c.get("match") == "all",
            stale=list(c.get("stale") or []),
        )
        for c in raw["cases"]
    ]


def _squash(text: str) -> str:
    """去掉全部空白再比。证据是照着原文抄的，不该因为一个换行或空格对不上。"""
    return "".join((text or "").split())


def _check_cases(cases: list[Case], chunks: list[dict[str, Any]]) -> None:
    """
    用例和语料对不上就别跑。

    标题改了一个字、正文改了一句话，用例里的 gold 和 evidence 就悬空了——
    那条用例会永远"没召回"，看起来像检索变差，其实是用例坏了。
    """
    by_title: dict[str, list[str]] = {}
    for row in chunks:
        by_title.setdefault(row["title"], []).append(_squash(row["content"]))

    problems: list[str] = []
    for case in cases:
        for title in [g["title"] for g in case.gold] + case.stale:
            if title not in by_title:
                problems.append(f"{case.id}: 语料里没有《{title}》")
        for gold in case.gold:
            evidence = gold.get("evidence")
            if evidence and gold["title"] in by_title and not any(
                _squash(evidence) in body for body in by_title[gold["title"]]
            ):
                problems.append(f"{case.id}: 《{gold['title']}》里找不到证据原文 {evidence!r}")
    if problems:
        raise SystemExit("用例和语料对不上：\n  " + "\n  ".join(problems))


# ── 名次 ──────────────────────────────────────────────────────────────────────

def _rank_of(rows: list[dict], title: str, evidence: str | None = None) -> int:
    """目标排第几（从 1 起）；0 是完全没召回。给了 evidence 就只认包含它的那一片。"""
    needle = _squash(evidence) if evidence else ""
    for i, row in enumerate(rows, start=1):
        if row["title"] == title and (not needle or needle in _squash(row["content"])):
            return i
    return 0


def _case_rank(rows: list[dict], case: Case, passage: bool = False) -> int:
    """
    一条用例的名次。

    缺省是"任一篇目标文档"里排得最靠前的那个；match_all 的用例要所有目标都
    召回才算，名次取最靠后的那个——前 3 里只有其中一篇，等于只答得出半个问题。
    """
    ranks = [
        _rank_of(rows, g["title"], g.get("evidence") if passage else None) for g in case.gold
    ]
    if case.match_all:
        return max(ranks) if all(ranks) else 0
    found = [r for r in ranks if r]
    return min(found) if found else 0


class _CachedEmbedder:
    """
    同一段文本只算一次向量。

    一条查询在评测里要用好几回（三路对照，再和线上那条路核对一遍），
    走远端 Embedding 时不该为同一句话付好几次钱。
    """

    def __init__(self, inner: Embedder):
        self._inner = inner
        self._cache: dict[str, list[float]] = {}
        self.calls = 0

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def backend(self) -> str:
        return self._inner.backend

    @property
    def semantic(self) -> bool:
        return self._inner.semantic

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def embed_query(self, text: str) -> list[float]:
        key = f"query\x00{text}"    # 查询可以带前缀，和同一段文本当文档时不是同一个向量
        if key not in self._cache:
            self._cache[key] = await self._inner.embed_query(text)
            self.calls += 1
        return self._cache[key]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            vectors = await self._inner.embed_many(missing)
            self._cache.update(zip(missing, vectors, strict=True))
            self.calls += len(missing)
        return [self._cache[t] for t in texts]


# ── 存储后端 ──────────────────────────────────────────────────────────────────

def _with_search_path(dsn: str, schema: str) -> str:
    """
    把连接的 search_path 指到评测用的 schema。

    asyncpg 把 DSN 里它不认识的查询参数当作服务端设置发过去，所以这样写出来的
    连接池里每条连接都落在这个 schema 上。public 留在后面：vector 扩展通常装在
    那里，类型和操作符要靠它解析。
    """
    parts = urlsplit(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "search_path"]
    query.append(("search_path", f"{schema},public"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _dsn_target(dsn: str) -> str:
    """打印用：主机、端口和库名，不带用户名和密码。"""
    parts = urlsplit(dsn)
    return f"{parts.hostname or 'localhost'}:{parts.port or 5432}{parts.path}"


async def _open_postgres(dsn: str, dim: int):
    """
    在一个临时 schema 里起一套空表，返回 (store, cleanup)。

    不直接用库里现成的表：评测要导入自己的语料，混进正在用的知识库里就成了
    真数据；反过来，库里已有的文档也会搅进评测结果。schema 名带随机后缀，
    跑完整个删掉。
    """
    import asyncpg

    from app.storage.pg import Database
    from app.storage.vector_store import PgStore

    schema = f"eval_{secrets.token_hex(4)}"

    async def _drop() -> None:
        conn = await asyncpg.connect(dsn, timeout=10.0)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await conn.close()

    conn = await asyncpg.connect(dsn, timeout=10.0)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await conn.close()

    db = Database(_with_search_path(dsn, schema), max_size=4)
    try:
        await db.connect(embedding_dim=dim)
    except BaseException:
        await _drop()
        raise

    async def _cleanup() -> None:
        await db.close()
        await _drop()

    return PgStore(db), _cleanup


async def _open_sqlite():
    tmp = tempfile.TemporaryDirectory()
    store = LocalStore(db_path=f"{tmp.name}/eval_retrieval.db")

    async def _cleanup() -> None:
        await store.close()
        tmp.cleanup()

    return store, _cleanup


# ── 汇总 ──────────────────────────────────────────────────────────────────────

def _leg_stats(ranks: list[int], top_k: int) -> dict[str, Any]:
    n = len(ranks)
    return {
        "hit1": sum(1 for r in ranks if r == 1),
        "hitk": sum(1 for r in ranks if 0 < r <= top_k),
        "mrr": round(sum(1.0 / r for r in ranks if r) / n, 3) if n else 0.0,
    }


def _summarize(results: list[dict[str, Any]], top_k: int) -> dict[str, dict[str, Any]]:
    """按用例类型分组，外加一行合计。只算有答案的用例。"""
    answerable = [r for r in results if r["answerable"]]
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in answerable:
        groups.setdefault(r["type"], []).append(r)
    groups["all"] = answerable

    summary: dict[str, dict[str, Any]] = {}
    for name, rows in groups.items():
        summary[name] = {
            "n": len(rows),
            **{leg: _leg_stats([r["rank"][leg] for r in rows], top_k) for leg in LEGS},
            "passage_hitk": sum(1 for r in rows if 0 < r["passage_rank"] <= top_k),
        }
    return summary


def _auc(positives: list[float], negatives: list[float]) -> float | None:
    """随机抽一条"证据在"和一条"证据不在"，前者分数更高的概率。0.5 = 分不开。"""
    if not positives or not negatives:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in positives for n in negatives)
    return round(wins / (len(positives) * len(negatives)), 3)


def _threshold_report(results: list[dict[str, Any]], top_k: int) -> dict[str, Any] | None:
    """
    证据阈值该定在哪。

    判的是"拿回来的前 K 条够不够回答"，所以正例是目标文档确实在前 K 的用例，
    反例是没有答案的用例**加上**有答案但没召回的——后者同样应当触发补检。
    """
    scored = [r for r in results if r["top_score"] is not None]
    present = [r["top_score"] for r in scored if r["answerable"] and 0 < r["rank"]["served"] <= top_k]
    absent = [r["top_score"] for r in scored if not (r["answerable"] and 0 < r["rank"]["served"] <= top_k)]
    if not present or not absent:
        return None

    def _at(threshold: float) -> dict[str, Any]:
        kept = sum(1 for s in present if s >= threshold)
        rejected = sum(1 for s in absent if s < threshold)
        return {
            "threshold": round(threshold, 4),
            "present_passed": kept,
            "absent_rejected": rejected,
            "balanced_accuracy": round((kept / len(present) + rejected / len(absent)) / 2, 3),
        }

    values = sorted(set(present + absent))
    cuts = [(a + b) / 2 for a, b in zip(values, values[1:], strict=False)] or values
    best = max((_at(t) for t in cuts), key=lambda x: x["balanced_accuracy"])

    def _spread(xs: list[float]) -> dict[str, float]:
        return {"min": round(min(xs), 4), "median": round(statistics.median(xs), 4),
                "max": round(max(xs), 4)}

    return {
        "present": {"n": len(present), **_spread(present)},
        "absent": {"n": len(absent), **_spread(absent)},
        "auc": _auc(present, absent),
        "current": _at(EVIDENCE_MIN_SCORE),
        "best": best,
    }


# ── 输出 ──────────────────────────────────────────────────────────────────────

def _print_summary(summary: dict[str, dict[str, Any]], top_k: int) -> None:
    order = [t for t in ("direct", "paraphrase", "code", "cross", "multi", "version") if t in summary]
    order += [t for t in summary if t not in order and t != "all"] + ["all"]
    head = f"hit@1 hit@{top_k}   MRR"
    print(f"{'类型':<12}{'条数':>4}   {'dense':<19}{'lexical':<19}{'rrf':<19}{'served':<19}"
          f"served 片段级 hit@{top_k}")
    print(f"{'':<12}{'':>4}     {head:<19}{head:<19}{head:<19}{head}")
    print("-" * 111)
    for name in order:
        row = summary[name]
        if name == "all":
            print("-" * 111)
        cells = "".join(
            f"{row[leg]['hit1']:>5} {row[leg]['hitk']:>5} {row[leg]['mrr']:>6.3f}  " for leg in LEGS
        )
        print(f"{name:<12}{row['n']:>6}   {cells}{row['passage_hitk']:>5}")


def _print_threshold(report: dict[str, Any] | None, top_k: int) -> None:
    print(f"\n证据阈值（看的是 served 前 {top_k} 条里最高的余弦分）")
    if report is None:
        print("  两组里有一组是空的，没法比。")
        return
    p, a = report["present"], report["absent"]
    print(f"  目标在前 {top_k} 的 {p['n']:>3} 条：最低 {p['min']:.3f}  中位 {p['median']:.3f}  最高 {p['max']:.3f}")
    print(f"  目标不在的   {a['n']:>3} 条：最低 {a['min']:.3f}  中位 {a['median']:.3f}  最高 {a['max']:.3f}"
          "（没有答案的 + 有答案但没召回的）")
    print(f"  区分度 AUC {report['auc']}（0.5 = 这个分数分不开两组）")
    for label, at in (("现行阈值", report["current"]), ("最能分开的阈值", report["best"])):
        print(f"  {label} {at['threshold']:.3f}：目标在的判充分 {at['present_passed']}/{p['n']}，"
              f"目标不在的判不足 {at['absent_rejected']}/{a['n']}")


def _print_cases(results: list[dict[str, Any]], top_k: int, only_missed: bool) -> None:
    rows = [r for r in results if r["answerable"]]
    if only_missed:
        rows = [r for r in rows if not 0 < r["rank"]["served"] <= top_k]
        print(f"\nserved 前 {top_k} 没有目标文档的 {len(rows)} 条"
              "（名次：dense / lexical / rrf / served，— 是没召回）")
    else:
        print("\n逐条名次（dense / lexical / rrf / served / served 片段级，— 是没召回）")
    for r in rows:
        ranks = " ".join(f"{(r['rank'][leg] or '—')!s:>3}" for leg in LEGS)
        passage = f" {(r['passage_rank'] or '—')!s:>3}" if not only_missed else ""
        print(f"  {r['id']:<9}{ranks}{passage}  {r['query']}  →  {' + '.join(r['gold'])}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> int:
    if args.suite == "smoke":
        cases, extra_docs = _smoke_cases(), []
    else:
        cases = _load_cases(pathlib.Path(args.cases))
        extra_docs = json.loads(pathlib.Path(args.corpus).read_text(encoding="utf-8"))

    embedder = _CachedEmbedder(Embedder() if args.embedding == "local" else Embedder.from_env())

    if args.backend == "postgres":
        dsn = (args.dsn or os.getenv("EVAL_POSTGRES_DSN", "")).strip()
        if not dsn:
            raise SystemExit(
                "--backend postgres 需要 --dsn 或 EVAL_POSTGRES_DSN。"
                "不会去读 POSTGRES_DSN——那是服务用的库，评测要你明确指给它。"
            )
        store, cleanup = await _open_postgres(dsn, embedder.dim)
        where = f"postgres {_dsn_target(dsn)}（临时 schema）"
    else:
        store, cleanup = await _open_sqlite()
        where = "sqlite 临时文件"

    try:
        kb = KnowledgeBase(store=store, embedder=embedder)
        await kb.ensure_seeded()
        if extra_docs:
            await kb.add_documents_async(extra_docs)
        stats = await kb.document_stats()
        chunks = await store.kb_search(await embedder.embed_query(cases[0].query), 100_000)
        _check_cases(cases, chunks)

        answerable = sum(1 for c in cases if c.answerable)
        print(f"语料: {stats['total_documents']} 篇文档 / {stats['total_chunks']} 个片段 | "
              f"用例: {len(cases)} 条（有答案 {answerable}，没有答案 {len(cases) - answerable}）")
        print(f"后端: {where} | Embedding: {embedder.backend} {embedder.dim} 维 | "
              f"hit@{args.top_k}，每路召回 {args.recall_k} 条")
        print("served = " + ("RRF 融合（与 rrf 一列相同）" if embedder.semantic
                             else "关键词在前、向量补位（本地 n-gram 向量不参与排序）") + "\n")

        # 默认参数下，这里的 served 前 K 和 /chat 快通道拿到的应当是同一批片段。
        # 顺手核一遍：哪天 search_async 改了召回深度或截断，评测不该悄悄量着另一条路。
        mirrors_search = args.recall_k == max(args.top_k * 3, 10)
        drifted: list[str] = []

        results: list[dict[str, Any]] = []
        for case in cases:
            embedding = await embedder.embed_query(case.query)
            dense = await store.kb_search(embedding, args.recall_k)
            lexical = await store.kb_search_lexical(query_tokens(case.query), embedding, args.recall_k)
            legs = {
                "dense": dense, "lexical": lexical,
                "rrf": fuse_candidates(dense, lexical, semantic=True),
                "served": fuse_candidates(dense, lexical, semantic=embedder.semantic),
            }
            top = legs["served"][:args.top_k]

            if mirrors_search:
                served = await kb.search_async(case.query, top_k=args.top_k)
                if [r["chunk_id"] for r in served] != [r["id"] for r in top]:
                    drifted.append(case.id)

            gold_rank = _case_rank(legs["served"], case)
            stale_ranks = [r for r in (_rank_of(legs["served"], t) for t in case.stale) if r]
            results.append({
                "id": case.id, "type": case.type, "lang": case.lang, "query": case.query,
                "answerable": case.answerable,
                "gold": [g["title"] for g in case.gold],
                "rank": {leg: _case_rank(rows, case) if case.answerable else 0
                         for leg, rows in legs.items()},
                "passage_rank": _case_rank(legs["served"], case, passage=True) if case.answerable else 0,
                # 旧版排在现行版本前面：两篇讲的是同一件事、数字不同，先看到哪篇就答哪篇
                "stale_first": (
                    bool(stale_ranks) and min(stale_ranks) <= args.top_k
                    and (not gold_rank or min(stale_ranks) < gold_rank)
                ) if case.stale else None,
                "top_score": max((float(r.get("score") or 0.0) for r in top), default=None),
                "top_titles": [r["title"] for r in top],
            })

        summary = _summarize(results, args.top_k)
        threshold = _threshold_report(results, args.top_k)
        versioned = [r for r in results if r["stale_first"] is not None]

        _print_summary(summary, args.top_k)
        if versioned:
            print(f"\n旧版文档排在现行版本前面: {sum(1 for r in versioned if r['stale_first'])}/{len(versioned)} 条")
        if any(not c.answerable for c in cases):
            _print_threshold(threshold, args.top_k)
        _print_cases(results, args.top_k, only_missed=not args.verbose)
        if drifted:
            print(f"\n注意：{len(drifted)} 条用例的 served 前 {args.top_k} 与 KnowledgeBase.search_async "
                  f"返回的不一致（{', '.join(drifted[:8])}）——评测和线上那条路对不上了。")
        if embedder.backend != "local_ngram":
            print(f"\n远端 Embedding 一共算了 {embedder.calls} 段文本。")

        if args.json:
            pathlib.Path(args.json).write_text(json.dumps({
                "suite": args.suite, "backend": store.backend,
                "embedding": {"backend": embedder.backend, "dim": embedder.dim},
                "served": "rrf" if embedder.semantic else "lexical_first",
                "top_k": args.top_k, "recall_k": args.recall_k,
                "corpus": stats, "summary": summary, "threshold": threshold,
                "stale_first": {"count": sum(1 for r in versioned if r["stale_first"]),
                                "of": len(versioned)},
                # None：这组参数下没有和 search_async 核对；[]：核对过，一致
                "differs_from_search_async": drifted if mirrors_search else None,
                "cases": results,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"\n逐条结果已写入 {args.json}")
    finally:
        await cleanup()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="检索层评测：dense / lexical / rrf / served 对照")
    parser.add_argument("--suite", choices=("ca", "smoke"), default="ca",
                        help="ca：加州工厂仿真知识库（默认）；smoke：最初的 8 条")
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite",
                        help="存储后端。postgres 在临时 schema 里跑，需要 --dsn 或 EVAL_POSTGRES_DSN")
    parser.add_argument("--dsn", default="", help="Postgres 连接串（库里要装得上 vector 扩展）")
    parser.add_argument("--embedding", choices=("local", "env"), default="local",
                        help="local：强制本地 n-gram，不发网络请求（默认）；env：按 EMBEDDING_* 配置")
    parser.add_argument("--corpus", default=str(CORPUS_PATH), help="ca 套用的语料 JSON")
    parser.add_argument("--cases", default=str(CASES_PATH), help="ca 套用的用例 JSON")
    parser.add_argument("--top-k", type=int, default=3, help="hit@K 的 K，默认 3（与 /chat 一致）")
    parser.add_argument("--recall-k", type=int, default=10, help="每路召回深度，默认 10")
    parser.add_argument("--verbose", action="store_true", help="逐条列出名次，而不只是没命中的")
    parser.add_argument("--json", default="", help="把汇总和逐条结果写成 JSON")
    sys.exit(asyncio.run(main(parser.parse_args())))
