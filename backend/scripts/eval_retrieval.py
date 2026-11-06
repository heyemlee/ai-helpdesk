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

完整链路（--full-chain）：
  上面都只量到排序。这个模式把用例当客户的提问，走一遍 /chat 的整条路——意图识别、
  查询改写、重排、补检、作答、答案守卫——再看最后那句回答，**会调用真实模型**
  （对话模型按服务的口径取：控制台保存的那份优先，没有就 .env）。只跑能机械核对的
  用例：答案带数字或编号的（用例的 answer 字段），加上知识库里没有答案的；说法类的
  标"未判"、不跑。判三件事，不用评分模型，口径见下面"完整链路"那一节。顺带记下
  重排前后的 hit@3。存储照旧是临时库，记忆、工单、事件日志都不碰。

用法：
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --suite smoke
    python scripts/eval_retrieval.py --verbose --json /tmp/retrieval.json
    python scripts/eval_retrieval.py --backend postgres \\
        --dsn postgresql://user:pass@127.0.0.1:5433/dbname
    python scripts/eval_retrieval.py --full-chain --embedding env --json /tmp/chain.json
    python scripts/eval_retrieval.py --full-chain --only v-01,n-06    # 先跑几条看看
"""
import argparse
import asyncio
import json
import os
import pathlib
import re
import secrets
import statistics
import sys
import tempfile
import unicodedata
from collections import Counter
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

from app.core import model_settings  # noqa: E402
from app.core.embedder import Embedder  # noqa: E402
from app.core.lexical import query_tokens  # noqa: E402
from app.core.model_runtime import embedder_from  # noqa: E402
from app.storage.vector_store import LocalStore  # noqa: E402
from app.tools.knowledge_base import KnowledgeBase, fuse_candidates  # noqa: E402
from app.tools.tool_manager import EVIDENCE_MIN_SCORE, ToolManager  # noqa: E402

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
    # 完整链路才用（用例的 answer 字段）。每一项是同一个数字或编号的几种写法，第一种照原文抄
    facts: list[list[str]] = field(default_factory=list)        # 答案里每项都要有
    stale_facts: list[list[str]] = field(default_factory=list)  # 只在旧版里有的，出现就是报了旧版

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
    cases = []
    for c in raw["cases"]:
        answer = c.get("answer") or {}
        cases.append(Case(
            id=c["id"], type=c["type"], lang=c.get("lang", "zh"), query=c["query"],
            gold=list(c.get("gold") or []), match_all=c.get("match") == "all",
            stale=list(c.get("stale") or []),
            facts=[list(f) for f in answer.get("facts") or []],
            stale_facts=[list(f) for f in answer.get("stale") or []],
        ))
    return cases


def _squash(text: str) -> str:
    """去掉全部空白再比。证据是照着原文抄的，不该因为一个换行或空格对不上。"""
    return "".join((text or "").split())


# 区间的几种写法：7-10、7–10、7~10、7 到 10、7 至 10、7 to 10
_RANGE_RE = re.compile(r"(?<=\d)(?:-|–|—|~|到|至|to)(?=\d)")
# 千分位：1,200、12,500。后面恰好三位数才算，「3,5,7」那种列举不动
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _fold(text: str) -> str:
    """
    比数字用的写法：全角转半角、小写、去掉空白和千分位、区间连接统一成「-」、分数斜杠统一成「/」。

    「7 到 10 个工作日」「7–10 business days」「7-10个工作日」折完都是 7-10 开头，
    「$8,000」和「8000 美元」里都是 8000，「34½」和「34-1/2」都带 1/2。
    """
    text = unicodedata.normalize("NFKC", text or "").lower().replace("⁄", "/")
    text = re.sub(r"\s+", "", text)
    return _RANGE_RE.sub("-", _THOUSANDS_RE.sub("", text))


def _contains(folded: str, variant: str) -> bool:
    """
    `folded`（已经 _fold 过）里有没有这种写法。

    数字两头不能连着更长的数：「150」不算「1500」「2150」里有，「8:00」不算「18:00」里有，
    「3:30」不算「15:30」里有；小数也算更长的数（「$150」不在「$150.50」里），只有小数部分
    全是 0 的不算（「$150.00」就是 $150）。
    """
    needle = _fold(variant)
    if not needle:
        return False
    pattern = re.escape(needle)
    if needle[0].isdigit():
        pattern = r"(?<![\d.])" + pattern
    if needle[-1].isdigit():
        pattern += r"(?!\d|\.\d*[1-9])"
    return re.search(pattern, folded) is not None


def _check_cases(cases: list[Case], chunks: list[dict[str, Any]]) -> None:
    """
    用例和语料对不上就别跑。

    标题改了一个字、正文改了一句话，用例里的 gold 和 evidence 就悬空了——
    那条用例会永远"没召回"，看起来像检索变差，其实是用例坏了。

    完整链路要核的数字也一样查：每项的第一种写法必须出现在目标文档里；旧版的数字
    必须出现在旧版里、而且现行版本里没有——现行版本里也有的话，答案里出现它说明
    不了什么。
    """
    by_title: dict[str, list[str]] = {}
    for row in chunks:
        by_title.setdefault(row["title"], []).append(_squash(row["content"]))

    def text_of(titles: list[str]) -> str:
        return _fold("\n".join(body for t in titles for body in by_title.get(t, [])))

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
        if (case.facts or case.stale_facts) and not case.answerable:
            problems.append(f"{case.id}: 没有答案的用例不该写 answer")
        if case.stale_facts and not case.stale:
            problems.append(f"{case.id}: 写了旧版的数字，却没写旧版是哪篇（stale）")
        current = text_of([g["title"] for g in case.gold])
        superseded = text_of(case.stale)
        for fact in case.facts:
            if not _contains(current, fact[0]):
                problems.append(f"{case.id}: 目标文档里找不到要核的 {fact[0]!r}")
        for fact in case.stale_facts:
            if not _contains(superseded, fact[0]):
                problems.append(f"{case.id}: 旧版里找不到 {fact[0]!r}")
            elif _contains(current, fact[0]):
                problems.append(f"{case.id}: 现行版本里也有 {fact[0]!r}，分不出答案用的是哪一版")
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
    def inner(self) -> Embedder:
        return self._inner

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


# ── 完整链路（--full-chain）──────────────────────────────────────────────────
#
# 前面量的都是"正确的那篇排第几"。客户看到的是最后那句话，中间还隔着意图识别、改写、
# 重排、补检、作答和答案守卫。守卫只核数字有没有出处（answer_policy._is_grounded），
# 旧版文档被检索到时，旧价格也算有出处——所以排序对了不等于答案对，答案里的数字有出处
# 也不等于对，得看最后那句话本身。
#
# 只判三件能机械核对的事，不用评分模型：
#   correct   答案里有没有正确的数字或编号：用例 answer.facts 的每一项都要出现（写法任一）；
#   stale     有没有报出旧版里的数字：answer.stale 里任一项出现就算；
#   outcome   没有答案的用例有没有编：答案里有语料、SOP、固定话术和问题里都没有的数字，
#             算编了（invented）；没有这种数字、又明说资料里没有或转了人工的，算拒答
#             （declined）；两样都不是的（比如自己下了个"不支持"的结论）是 unclear，
#             列出来给人看，不替它判。
# 有答案的用例也报"资料里没有的数字"（invented），只是旁证，不算进对错。

SKILLS_DIR = _ROOT / "skills"

MODE_LABEL = {"direct": "直检", "rewrite_rerank": "改写+重排", "": "没检索"}

# 明说"资料里没有、我确认不了"的说法。转没转人工看编排器的结果（handoff），不看措辞。
# 两类都不算：「如有疑问请联系客服」这种收尾套话——模型几乎每条都会带一句，编了答案也会；
# 「不支持」「无法提供」「我们没有」这种替知识库下的结论——正是要人看的那一类。
_DECLINE_RE = re.compile(
    r"没有找到|未找到|没找到|找不到|查不到|没有查到|未查到|没查到"
    r"|没有相关(?:的)?(?:信息|资料|说明|规定)|暂无相关|没有这方面的|没有提到|未提及|没有提及|没有说明|未说明"
    r"|没有列出|未列出|资料[里中]?没有|知识库[里中]?没有|无法确认|没法确认|不能确认|确认不了"
    r"|无法回答|回答不了|不清楚|不确定"
    r"|no information|(?:don'?t|do not) have (?:any )?(?:information|details|data)|not sure"
    r"|(?:can'?t|cannot|unable to) confirm|could(?:n'?t| not) find|don'?t know"
    r"|not (?:mentioned|covered|listed|specified) in|doesn'?t (?:cover|mention|say)",
    re.I,
)
# 折过的文本里的一个数：整数、小数、8:00、1/16。「7-10」是两个数
_NUMBER_RE = re.compile(r"\d+(?:[.:/]\d+)*")


def _judge(case: Case, answer: str, known: str, handoff: bool) -> dict[str, Any]:
    """一条回答的判定。known 是折过的"有出处的文本"：语料、SOP、固定话术。"""
    folded = _fold(answer)
    question = _fold(case.query)
    numbers = dict.fromkeys(re.sub(r"\.0+$", "", n) for n in _NUMBER_RE.findall(folded))   # $150.00 就是 150
    invented = [n for n in numbers if not _contains(known, n) and not _contains(question, n)]
    verdict: dict[str, Any] = {"invented": invented}
    if case.facts:
        missing = [fact[0] for fact in case.facts if not any(_contains(folded, v) for v in fact)]
        verdict.update(
            correct=not missing,
            missing=missing,
            stale=[fact[0] for fact in case.stale_facts if any(_contains(folded, v) for v in fact)],
        )
    elif not case.answerable:
        declined = handoff or bool(_DECLINE_RE.search(answer or ""))
        verdict["outcome"] = "invented" if invented else "declined" if declined else "unclear"
    return verdict


def _known_text(chunks: list[dict[str, Any]]) -> str:
    """回答里的数字可以从哪儿来：语料、SOP、专家的角色提示词、编排器的固定话术（转人工、追问）。"""
    from app.agents.agent_orchestrator import (
        _CUSTOMER_TEXT,
        BillingAgent,
        GeneralAgent,
        OrderAgent,
        TechnicalAgent,
    )

    parts = [row["content"] for row in chunks]
    parts += [path.read_text(encoding="utf-8") for path in sorted(SKILLS_DIR.rglob("*.md"))]
    parts += [agent.system_prompt for agent in (GeneralAgent, TechnicalAgent, BillingAgent, OrderAgent)]
    parts += [text for lang in _CUSTOMER_TEXT.values() for text in lang.values()]
    return _fold("\n".join(parts))


class _ProbedTools(ToolManager):
    """
    记下每次重排拿到的候选（按召回顺序）和排出来的结果。只给评测用。

    "重排前的 hit@3"量的是重排拿到的候选的前 3 条，生产代码里没有地方留这个，只能在这里截。
    用例是一条一条串着跑的，所以一个列表就够，每条开始前清空。
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.reranks: list[tuple[list[Any], list[Any]]] = []

    async def _rerank(self, query: str, items: list[Any], top_k: int):
        outcome = await super()._rerank(query, items, top_k)
        self.reranks.append((list(items), list(outcome.items)))
        return outcome


class _Discard:
    """事件只留在记录器自己的 events 里（数模型调用用），不往任何地方写。"""

    def put(self, event: Any) -> None:
        pass


@dataclass
class _Chain:
    cfg: dict[str, Any]
    orchestrator: Any
    tools: _ProbedTools
    deps: Any
    reranker: str     # llm_listwise，或者配了远端的 cross_encoder:<模型>


def _build_chain(kb: KnowledgeBase, embedder: _CachedEmbedder) -> _Chain:
    """
    照 api/main.py 的 lifespan 装一条 /chat：同样的类、同样的配置来源、同样注册的知识检索工具。

    不一样的地方都是为了不碰真数据、不多花调用：知识库是这里的临时库；记忆一律为空（每条
    用例都是一位新客户的第一句话）；不接业务数据委派（客户实例也不接）；工具循环关着（客户
    实例也关着）；每种专家一个实例。
    """
    from app.agents.agent_orchestrator import AgentOrchestrator
    from app.agents.turn_prep import TurnDeps
    from app.core.answer_policy import AnswerPolicy
    from app.core.intent_recognizer import IntentRecognizer
    from app.core.llm_utils import create_llm_client
    from app.core.model_runtime import chat_config_from
    from app.core.reranker import Reranker
    from app.core.skill_loader import SkillManager
    from app.memory.conversation_memory import MemoryContext
    from app.tools.tool_manager import Tool
    from app.tools.turn_tools import Prefetch

    try:
        cfg = chat_config_from(model_settings.load_store(model_settings.settings_path()))
    except RuntimeError as ex:
        raise SystemExit(f"对话模型没配好：{ex}") from ex
    base_url = cfg.get("base_url") or None

    recognizer = IntentRecognizer(
        api_key=cfg["api_key"], base_url=base_url, model=cfg["model"],
        enable_fast_path=os.getenv("ENABLE_INTENT_FAST_PATH", "true").lower() != "false",
        embedder=embedder.inner,
    )
    skills = SkillManager(
        root_dir=str(SKILLS_DIR),
        max_prompt_chars=int(os.getenv("AI_HELPDESK_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skills.load()
    orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"], base_url=base_url, model=cfg["model"], skill_manager=skills,
        policy=AnswerPolicy(enabled=os.getenv("ENABLE_ANSWER_POLICY", "true").lower() != "false"),
        recognizer=recognizer, replicas=1,
    )
    # 和服务一样按 RERANK_* 取：配了远端 cross-encoder 就会真的调它（它用自己的 httpx）
    reranker = Reranker.from_env()
    tools = _ProbedTools(api_key=cfg["api_key"], base_url=base_url, model=cfg["model"],
                         shared_state=None, reranker=reranker)
    tools.register(Tool(
        name="knowledge_search",
        description="搜索知识库（稠密向量 + 关键词双路混合检索，RRF 融合）",
        handler=kb.search_handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                "required": ["query"]},
        cache_ttl=300.0, cache_version=kb.cache_version, supports_rerank=True,
    ))
    # 和 ModelRuntime.apply_chat 一样再换一次客户端：上面是按 .env 的协议造的，
    # 控制台保存的那份可能是另一家
    client = create_llm_client(cfg["api_key"], base_url, provider=cfg["provider"])
    orchestrator.reconfigure_llm(client, cfg["model"])
    tools.reconfigure_llm(client, cfg["model"])

    async def no_memory(user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    deps = TurnDeps(load_memory=no_memory, recognize_intent=orchestrator.recognize_intent,
                    outage_reason=orchestrator.llm_unavailable_reason, prefetch=Prefetch(tool_manager=tools))
    return _Chain(cfg=cfg, orchestrator=orchestrator, tools=tools, deps=deps, reranker=reranker.backend)


def _hit(titles: list[str], case: Case) -> bool:
    gold = [g["title"] for g in case.gold]
    return all(t in titles for t in gold) if case.match_all else any(t in titles for t in gold)


async def _run_case(chain: _Chain, case: Case, top_k: int, known: str) -> dict[str, Any]:
    """一条用例走一遍 /chat 的准备阶段和作答，和 api/main.chat 调的是同两个函数。"""
    from app.agents.turn_prep import prepare_turn
    from app.events import TurnRecorder, bind

    chain.tools.reranks.clear()
    conv_id = f"eval-chain-{case.id}"
    recorder = TurnRecorder(conv_id, instance="eval", conv_id=conv_id, mode="full", sink=_Discard())
    with bind(recorder):
        prepared = await prepare_turn(case.query, user_id="eval-chain", conv_id=conv_id,
                                      request_id=conv_id, deps=chain.deps)
        result = await chain.orchestrator.run(prepared.request)

    knowledge = prepared.evidence.get("knowledge") or {}
    after = [str(s.get("title", "")) for s in knowledge.get("sources") or []][:top_k]
    first_round = chain.tools.reranks[0][0] if chain.tools.reranks else None
    before = [str(i.get("title", "")) for i in first_round[:top_k] if isinstance(i, dict)] \
        if first_round is not None else after
    calls = [json.loads(e.data) for e in recorder.events if e.type == "model_call"]
    intent = prepared.intent
    return {
        "id": case.id, "type": case.type, "lang": case.lang, "query": case.query,
        "judged": "facts" if case.facts else "unanswerable",
        "gold": [g["title"] for g in case.gold],
        "facts": [fact[0] for fact in case.facts],
        "intent": getattr(intent.intent, "value", str(intent.intent)),
        "fast_path": bool(getattr(intent, "fast_path", False)),
        "retrieval": {
            "attempted": bool(knowledge.get("attempted")),
            "mode": knowledge.get("mode") or "",
            "reranked": bool(knowledge.get("reranked")),
            "corrected": bool(knowledge.get("corrected")),
            "evidence_ok": knowledge.get("evidence_ok"),
            "before_rerank": before,
            "after_rerank": after,
        },
        "hit_before": _hit(before, case) if case.answerable else None,
        "hit_after": _hit(after, case) if case.answerable else None,
        "stale_in_context": any(t in after for t in case.stale) if case.stale else None,
        "agent": result.agent_type.value,
        "handoff": result.handoff,
        "clarification": result.needs_clarification,
        "unavailable": result.service_unavailable,
        "unavailable_reason": result.unavailable_reason,
        "violations": [v.to_dict() for v in result.policy_violations],
        "answer": result.response,
        "calls": {
            "total": len(calls),
            "by_purpose": dict(Counter(str(c.get("purpose") or "unknown") for c in calls)),
            "failed": sum(1 for c in calls if c.get("status") != "ok"),
        },
        **_judge(case, result.response, known, result.handoff),
    }


def _chain_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总。模型没答上来的那几条（超时、5xx）单列，不算进对错。"""
    answered = [r for r in results if not r["unavailable"]]
    facts = [r for r in answered if r["judged"] == "facts"]
    versioned = [r for r in facts if r["stale_in_context"] is not None]
    unanswerable = [r for r in answered if r["judged"] == "unanswerable"]

    def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "n": len(rows),
            "correct": sum(1 for r in rows if r["correct"]),
            "hit_before": sum(1 for r in rows if r["hit_before"]),
            "hit_after": sum(1 for r in rows if r["hit_after"]),
        }

    purposes: Counter = Counter()
    for r in results:
        purposes.update(r["calls"]["by_purpose"])
    return {
        "run": len(results),
        "unavailable": len(results) - len(answered),
        "facts": {
            **counts(facts),
            "stale_reported": sum(1 for r in facts if r["stale"]),
            "with_stale_version": len(versioned),
            "stale_in_context": sum(1 for r in versioned if r["stale_in_context"]),
            "invented": sum(1 for r in facts if r["invented"]),
            "handoff": sum(1 for r in facts if r["handoff"]),
            "by_mode": {
                mode: counts([r for r in facts if r["retrieval"]["mode"] == mode])
                for mode in MODE_LABEL if any(r["retrieval"]["mode"] == mode for r in facts)
            },
        },
        "unanswerable": {
            "n": len(unanswerable),
            **{k: sum(1 for r in unanswerable if r["outcome"] == k) for k in ("declined", "invented", "unclear")},
        },
        "calls": {
            "total": sum(r["calls"]["total"] for r in results),
            "by_purpose": dict(purposes.most_common()),
            "failed": sum(r["calls"]["failed"] for r in results),
        },
    }


def _excerpt(text: str, limit: int = 70) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _case_line(r: dict[str, Any]) -> str:
    """一条用例的一行：判定、检索方式、重排前后、调用次数。"""
    if r["unavailable"]:
        verdict = f"模型没答上来（{r['unavailable_reason'] or '?'}）"
    elif r["judged"] == "facts":
        verdict = "答对" if r["correct"] else "缺 " + "、".join(r["missing"])
        if r["stale"]:
            verdict += "；报了旧版 " + "、".join(r["stale"])
        if r["invented"]:
            verdict += "；资料里没有的数 " + "、".join(r["invented"])
    else:
        verdict = {"declined": "拒答", "invented": "编了 " + "、".join(r["invented"]),
                   "unclear": "没拒答，要人看"}[r["outcome"]]
    mode = MODE_LABEL.get(r["retrieval"]["mode"], r["retrieval"]["mode"])
    hits = "" if r["hit_before"] is None else \
        f" 前{'✓' if r['hit_before'] else '✗'}后{'✓' if r['hit_after'] else '✗'}"
    extra = "（转人工）" if r["handoff"] else ""
    return f"{r['id']:<8}{verdict}{extra} | {mode}{hits} | {r['calls']['total']} 次调用"


def _print_chain(summary: dict[str, Any], results: list[dict[str, Any]], skipped: int,
                 top_k: int, verbose: bool) -> None:
    f, u, calls = summary["facts"], summary["unanswerable"], summary["calls"]
    print(f"\n带数字或编号的 {f['n']} 条：答对 {f['correct']}，缺数字 {f['n'] - f['correct']}"
          f"（其中转人工 {f['handoff']} 条）")
    print(f"  有旧版的 {f['with_stale_version']} 条：答案报了旧版数字 {f['stale_reported']} 条，"
          f"旧版片段进了作答上下文 {f['stale_in_context']} 条")
    print(f"  hit@{top_k}：重排前 {f['hit_before']}/{f['n']}，重排后（进上下文的）{f['hit_after']}/{f['n']}")
    for mode, row in f["by_mode"].items():
        print(f"    {MODE_LABEL.get(mode, mode):<6} {row['n']:>3} 条：答对 {row['correct']}，"
              f"hit@{top_k} 前 {row['hit_before']} 后 {row['hit_after']}")
    print(f"  答案里有资料里没有的数字：{f['invented']} 条（旁证，不算进对错）")
    print(f"没有答案的 {u['n']} 条：拒答或转人工 {u['declined']}，编了数字 {u['invented']}，"
          f"没拒答、要人看 {u['unclear']}")
    print(f"未判：{skipped} 条说法类答案，没有能机械核对的数字，这个模式不跑")
    if summary["unavailable"]:
        print(f"模型没答上来（超时、5xx 之类）：{summary['unavailable']} 条，不算进上面的对错")
    purposes = "，".join(f"{k} {v}" for k, v in calls["by_purpose"].items())
    print(f"模型调用 {calls['total']} 次（{purposes or '无'}），失败 {calls['failed']} 次")

    shown = results if verbose else [
        r for r in results
        if r["unavailable"] or (r["judged"] == "facts" and (not r["correct"] or r["stale"]))
        or (r["judged"] == "unanswerable" and r["outcome"] != "declined")
    ]
    if shown:
        print("\n逐条" if verbose else "\n要看的几条（没答对、报了旧版、编了、没拒答的）")
        for r in shown:
            print(f"  {_case_line(r)}  {r['query']}\n      → {_excerpt(r['answer'])}")


async def _full_chain(args: argparse.Namespace, cases: list[Case], kb: KnowledgeBase,
                      chunks: list[dict[str, Any]], embedder: _CachedEmbedder,
                      stats: dict[str, Any], where: str) -> int:
    run = [c for c in cases if c.facts or not c.answerable]
    skipped = len(cases) - len(run)
    if not run:
        raise SystemExit("选中的用例里没有能判的（带 answer 的，或没有答案的）。")
    chain = _build_chain(kb, embedder)
    cfg = chain.cfg
    known = _known_text(chunks)
    print(f"完整链路 | 对话模型 {cfg['model']}（{cfg['provider']}，来源 {cfg['source']}）| "
          f"Embedding {embedder.backend} {embedder.dim} 维 | 重排 {chain.reranker} | 后端 {where}")
    print(f"语料 {stats['total_documents']} 篇 / {stats['total_chunks']} 个片段 | 跑 {len(run)} 条："
          f"带数字或编号 {sum(1 for c in run if c.facts)}，没有答案 {sum(1 for c in run if not c.answerable)}；"
          f"未判 {skipped} 条 | 一条一条串着跑\n")

    results: list[dict[str, Any]] = []
    stopped = ""
    for i, case in enumerate(run, start=1):
        # 凭证被拒之后不再出网：保护挡着的时候，后面每一条都只会拿到一句"暂时不可用"
        stopped = chain.orchestrator.llm_unavailable_reason()
        if stopped:
            break
        row = await _run_case(chain, case, args.top_k, known)
        results.append(row)
        print(f"  [{i:>3}/{len(run)}] {_case_line(row)}", flush=True)
    stopped = stopped or chain.orchestrator.llm_unavailable_reason()
    # 超时、连接失败、5xx 不会让保护挡住后面的调用，这一条只是没答上来。没算进对错的
    # 每一条都要让人看得见：不然全挂了的一轮，汇总是"0 条里答对 0 条"、退出码 0，像是跑通了
    unanswered = [r["id"] for r in results if r["unavailable"]]
    incomplete = stopped or (f"{len(unanswered)} 条模型没答上来" if unanswered else "")

    summary = _chain_summary(results)
    _print_chain(summary, results, skipped, args.top_k, args.verbose)
    if stopped:
        print(f"\n没跑完：模型服务不可用（{stopped}），{len(run)} 条只跑了 {len(results)} 条。"
              "上面的数只来自跑完的那些；脚本不会自己重试。")
    if unanswered:
        print(f"\n{len(unanswered)} 条模型没答上来（超时、5xx 之类），没算进上面的对错。"
              f"补跑：--only {','.join(unanswered)}")
    if embedder.backend != "local_ngram":
        print(f"\n知识库这边远端 Embedding 一共算了 {embedder.calls} 段文本（意图识别自己的不在内）。")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({
            "mode": "full_chain", "suite": args.suite, "backend": kb.backend,
            "model": cfg["model"], "provider": cfg["provider"], "model_source": cfg["source"],
            "embedding": {"backend": embedder.backend, "dim": embedder.dim}, "reranker": chain.reranker,
            "top_k": args.top_k, "corpus": stats,
            "planned": len(run), "not_judged": skipped,
            # 非空就不是完整的一轮：保护挡住之后停了（原因），或者有几条没答上来（unanswered）
            "incomplete": incomplete, "unanswered": unanswered,
            "summary": summary, "cases": results,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n逐条结果已写入 {args.json}")
    return 3 if incomplete else 0


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> int:
    full_chain = getattr(args, "full_chain", False)
    if args.suite == "smoke":
        if full_chain:
            raise SystemExit("--full-chain 只支持 ca 那套用例：smoke 的 8 条没有标要核的数字。")
        cases, extra_docs = _smoke_cases(), []
    else:
        cases = _load_cases(pathlib.Path(args.cases))
        extra_docs = json.loads(pathlib.Path(args.corpus).read_text(encoding="utf-8"))
    only = [x.strip() for x in (getattr(args, "only", "") or "").split(",") if x.strip()]
    if only:
        unknown = sorted(set(only) - {c.id for c in cases})
        if unknown:
            raise SystemExit(f"没有这些用例：{', '.join(unknown)}")
        cases = [c for c in cases if c.id in only]

    if args.embedding == "local":
        inner = Embedder()
    else:
        # 和服务同一个口径：控制台保存的那份优先，没有就按 EMBEDDING_*
        inner, _ = embedder_from(model_settings.load_store(model_settings.settings_path()))
    embedder = _CachedEmbedder(inner)

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
        if full_chain:
            return await _full_chain(args, cases, kb, chunks, embedder, stats, where)

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
                        help="local：强制本地 n-gram，不发网络请求（默认）；"
                             "env：和服务一样，控制台保存的那份优先，没有就按 EMBEDDING_*")
    parser.add_argument("--corpus", default=str(CORPUS_PATH), help="ca 套用的语料 JSON")
    parser.add_argument("--cases", default=str(CASES_PATH), help="ca 套用的用例 JSON")
    parser.add_argument("--top-k", type=int, default=3, help="hit@K 的 K，默认 3（与 /chat 一致）")
    parser.add_argument("--recall-k", type=int, default=10, help="每路召回深度，默认 10")
    parser.add_argument("--verbose", action="store_true", help="逐条列出名次，而不只是没命中的")
    parser.add_argument("--json", default="", help="把汇总和逐条结果写成 JSON")
    parser.add_argument("--full-chain", action="store_true",
                        help="走完整的 /chat 链路看最后的回答（会调用真实模型），只跑能机械核对的用例")
    parser.add_argument("--only", default="", help="只跑这几条，逗号分隔的用例 id，例如 v-01,n-06")
    sys.exit(asyncio.run(main(parser.parse_args())))
