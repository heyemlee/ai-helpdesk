"""
检索评测脚本（scripts/eval_retrieval.py）和它的数据文件。

用例里的 gold 和 evidence 是照着语料抄的。语料改了一个标题、一句话，对应的用例
就悬空了：它会永远"没召回"，看起来像检索变差，其实是用例坏了。所以两份文件
对不对得上放在这里查，不等到有人跑脚本才发现。
"""
import argparse
import importlib.util
import json
import pathlib

import pytest

from app.tools.knowledge_base import _DEFAULT_DOCS, CHUNK_SIZE, KnowledgeBase


@pytest.fixture(scope="module")
def ev():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "eval_retrieval.py"
    spec = importlib.util.spec_from_file_location("scripts_eval_retrieval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(ev, tmp_path, suite: str) -> argparse.Namespace:
    return argparse.Namespace(
        suite=suite, backend="sqlite", dsn="", embedding="local",
        corpus=str(ev.CORPUS_PATH), cases=str(ev.CASES_PATH),
        top_k=3, recall_k=10, verbose=False, json=str(tmp_path / "out.json"),
    )


# ── 数据文件 ──────────────────────────────────────────────────────────────────

def test_cases_and_corpus_agree(ev):
    docs = _DEFAULT_DOCS + json.loads(ev.CORPUS_PATH.read_text(encoding="utf-8"))
    titles = [d["title"] for d in docs]
    assert len(set(titles)) == len(titles), "标题是用例认文档的依据，不能重名"

    cases = ev._load_cases(ev.CASES_PATH)
    assert len({c.id for c in cases}) == len(cases)
    chunks = [
        {"title": d["title"], "content": chunk}
        for d in docs for chunk in KnowledgeBase._chunk_text(d["content"], CHUNK_SIZE)
    ]
    ev._check_cases(cases, chunks)    # 对不上会 SystemExit，并列出是哪几条


def test_dangling_evidence_is_reported_not_scored_as_a_miss(ev):
    chunks = [{"title": "退换货政策", "content": "未拆封的标准五金可以在签收后 30 天内退货"}]
    stale = ev.Case(id="x", type="direct", lang="zh", query="q",
                    gold=[{"title": "退换货政策", "evidence": "签收后 60 天内"}])
    missing = ev.Case(id="y", type="direct", lang="zh", query="q", gold=[{"title": "不存在的文档"}])

    with pytest.raises(SystemExit) as raised:
        ev._check_cases([stale, missing], chunks)
    assert "x: 《退换货政策》里找不到证据原文" in str(raised.value)
    assert "y: 语料里没有《不存在的文档》" in str(raised.value)


# ── 名次怎么算 ────────────────────────────────────────────────────────────────

ROWS = [
    {"title": "保养指南", "content": "柜体和门板：用软布擦拭"},
    {"title": "自提须知", "content": "尾款须在提货前到账"},
    {"title": "保养指南", "content": "石英石台面：不要把刚离火的锅直接放在台面上"},
    {"title": "暂存规则", "content": "工厂免费存放 14 天"},
]


def test_passage_rank_only_counts_the_chunk_that_holds_the_answer(ev):
    """长文档切成了几片：召回了那篇文档，不等于召回了有答案的那一片。"""
    case = ev.Case(id="p", type="paraphrase", lang="zh", query="q",
                   gold=[{"title": "保养指南", "evidence": "刚离火的锅"}])
    assert ev._case_rank(ROWS, case) == 1
    assert ev._case_rank(ROWS, case, passage=True) == 3


def test_match_all_takes_the_last_document_to_arrive(ev):
    """要两篇都在才答得全：名次取靠后的那篇，缺一篇就是没召回。"""
    both = ev.Case(id="m", type="multi", lang="zh", query="q", match_all=True,
                   gold=[{"title": "自提须知"}, {"title": "暂存规则"}])
    assert ev._case_rank(ROWS, both) == 4
    assert ev._case_rank(ROWS[:3], both) == 0

    either = ev.Case(id="a", type="direct", lang="zh", query="q",
                     gold=[{"title": "暂存规则"}, {"title": "自提须知"}])
    assert ev._case_rank(ROWS, either) == 2


def test_threshold_report_counts_missed_answers_as_evidence_absent(ev):
    """
    阈值判的是"拿回来的前 K 条够不够"。有答案但没召回的，和没有答案的一样，
    都应该被判成不够。
    """
    def row(answerable, rank, score):
        return {"answerable": answerable, "rank": {"served": rank}, "top_score": score}

    report = ev._threshold_report(
        [row(True, 1, 0.8), row(True, 2, 0.7), row(True, 7, 0.3), row(False, 0, 0.2)], top_k=3
    )
    assert report["present"]["n"] == 2 and report["absent"]["n"] == 2
    assert report["auc"] == 1.0
    assert 0.3 < report["best"]["threshold"] < 0.7
    assert report["best"]["present_passed"] == 2 and report["best"]["absent_rejected"] == 2


# ── 两套用例都能离线跑通 ──────────────────────────────────────────────────────

async def test_smoke_suite_reproduces_the_recorded_numbers(ev, tmp_path, capsys):
    """docs/evaluation/retrieval-smoke.md 和 README 里写着这组数。"""
    assert await ev.main(_args(ev, tmp_path, "smoke")) == 0

    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    summary = report["summary"]["all"]
    assert summary["n"] == 8
    assert (summary["dense"]["hitk"], summary["dense"]["mrr"]) == (6, 0.583)
    assert (summary["lexical"]["hitk"], summary["lexical"]["mrr"]) == (7, 0.844)
    assert (summary["rrf"]["hitk"], summary["rrf"]["mrr"]) == (8, 0.812)
    # 本地向量下服务返回的是"关键词在前"的排序：这 8 条上和纯关键词一样
    assert report["served"] == "lexical_first"
    assert (summary["served"]["hitk"], summary["served"]["mrr"]) == (7, 0.844)


async def test_ca_suite_measures_the_same_path_the_service_uses(ev, tmp_path, capsys):
    assert await ev.main(_args(ev, tmp_path, "ca")) == 0

    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert report["corpus"]["total_documents"] == 52
    assert report["summary"]["all"]["n"] == 108
    assert sum(1 for c in report["cases"] if not c["answerable"]) == 14
    # served 一列的前 3 要和 KnowledgeBase.search_async 给出的是同一批片段，
    # 否则量的就不是线上那条路
    assert report["differs_from_search_async"] == []
    # 这一轮改融合方式的理由，钉住方向：本地向量下，关键词在前不能比 RRF 差
    served, rrf = report["summary"]["all"]["served"], report["summary"]["all"]["rrf"]
    assert served["hitk"] > rrf["hitk"] and served["mrr"] > rrf["mrr"]
