"""
检索评测脚本（scripts/eval_retrieval.py）和它的数据文件。

用例里的 gold 和 evidence 是照着语料抄的。语料改了一个标题、一句话，对应的用例
就悬空了：它会永远"没召回"，看起来像检索变差，其实是用例坏了。所以两份文件
对不对得上放在这里查，不等到有人跑脚本才发现。

完整链路模式（--full-chain）在这里用一个假模型跑：接在 SDK 传输层上，按请求的
用途回话，一个请求都不出网。
"""
import argparse
import importlib.util
import json
import pathlib
import re
import time

import httpx
import pytest

from app.tools.knowledge_base import _DEFAULT_DOCS, CHUNK_SIZE, KnowledgeBase


@pytest.fixture(scope="module")
def ev():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "eval_retrieval.py"
    spec = importlib.util.spec_from_file_location("scripts_eval_retrieval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(ev, tmp_path, suite: str, **extra) -> argparse.Namespace:
    return argparse.Namespace(**{
        "suite": suite, "backend": "sqlite", "dsn": "", "embedding": "local",
        "corpus": str(ev.CORPUS_PATH), "cases": str(ev.CASES_PATH),
        "top_k": 3, "recall_k": 10, "verbose": False, "json": str(tmp_path / "out.json"),
        "full_chain": False, "only": "", **extra,
    })


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


# ── 完整链路：数字怎么比 ──────────────────────────────────────────────────────

def test_numbers_compare_across_spacing_ranges_and_currency_formats(ev):
    def has(text, variant):
        return ev._contains(ev._fold(text), variant)

    assert has("模板测量后 7 到 10 个工作日", "7-10")
    assert has("Installed 7–10 business days after templating", "7-10")
    assert has("满 8,000 美元免运费", "8000美元")
    assert has("Delivery is $150.00 per order", "$150")
    assert has("标准高度是 34½ 英寸", "341/2")
    assert has("Ｃ－６执照", "c-6")
    # 两头不能连着更长的数：这几处出错会把旧价格、别的钟点算成"答对"
    assert not has("运费 1500 美元", "150")
    assert not has("周一至周五 8:00 到 15:30", "5:30")
    assert not has("加收 $150.50", "$150")
    assert not has("C 区 75 到 150 英里", "150美元")


def test_answer_numbers_have_to_be_in_the_documents_they_claim(ev):
    """要核的数字写错了、旧版的数字现行版本里也有，这条用例就判不出东西，跑之前拦下。"""
    chunks = [
        {"title": "送货区域与运费标准（2026 版）", "content": "A 区每单 150 美元，改期须提前 48 小时"},
        {"title": "送货费标准（2025 版，已废止）", "content": "A 区每单 120 美元，改期须提前 48 小时"},
    ]
    gold = [{"title": "送货区域与运费标准（2026 版）"}]
    stale = ["送货费标准（2025 版，已废止）"]
    typo = ev.Case(id="t", type="version", lang="zh", query="q", gold=gold, stale=stale,
                   facts=[["160美元"]])
    shared = ev.Case(id="s", type="version", lang="zh", query="q", gold=gold, stale=stale,
                     facts=[["150美元"]], stale_facts=[["48小时"]])
    orphan = ev.Case(id="o", type="unanswerable", lang="zh", query="q", gold=[], facts=[["1"]])

    with pytest.raises(SystemExit) as raised:
        ev._check_cases([typo, shared, orphan], chunks)
    assert "t: 目标文档里找不到要核的 '160美元'" in str(raised.value)
    assert "s: 现行版本里也有 '48小时'" in str(raised.value)
    assert "o: 没有答案的用例不该写 answer" in str(raised.value)


def test_judge_reports_right_numbers_superseded_numbers_and_inventions(ev):
    known = ev._fold("A 区每单 150 美元。旧版 A 区每单 120 美元。")
    v01 = ev.Case(id="v-01", type="version", lang="zh", query="A 区的送货费是多少",
                  gold=[{"title": "x"}], stale=["y"],
                  facts=[["150美元", "$150"]], stale_facts=[["120美元", "$120"]])

    right = ev._judge(v01, "A 区每单 $150，订单满额免运费。", known, handoff=False)
    assert right == {"invented": [], "correct": True, "missing": [], "stale": []}
    old = ev._judge(v01, "A 区每单 120 美元。", known, handoff=False)
    assert (old["correct"], old["missing"], old["stale"]) == (False, ["150美元"], ["120美元"])
    made_up = ev._judge(v01, "A 区每单 150 美元，满 5000 美元免运费。", known, handoff=False)
    assert made_up["correct"] and made_up["invented"] == ["5000"]

    n06 = ev.Case(id="n-06", type="unanswerable", lang="zh", query="你们公司是哪一年成立的", gold=[])
    assert ev._judge(n06, "我们成立于 1998 年。", known, handoff=False)["outcome"] == "invented"
    assert ev._judge(n06, "资料里没有写成立年份，我帮您转人工确认。", known, handoff=False)["outcome"] == "declined"
    assert ev._judge(n06, "具体情况我这边不方便说。", known, handoff=True)["outcome"] == "declined"
    # 下了结论但没有数字：判不出编没编，交给人看
    assert ev._judge(n06, "我们是一家老牌工厂。", known, handoff=False)["outcome"] == "unclear"
    # 问题里自己带的数不算编
    n12 = ev.Case(id="n", type="unanswerable", lang="zh", query="离工厂 300 英里能送吗", gold=[])
    assert ev._judge(n12, "300 英里我这边确认不了。", known, handoff=False)["invented"] == []


# ── 完整链路：离线跑一遍 ──────────────────────────────────────────────────────

STALE_TITLE = "送货费标准（2025 版，已废止）"


class FakeModel:
    """
    OpenAI 协议的假模型，按请求的用途回话：

    - 意图：一律"咨询"，让每条都去查知识库；
    - 改写：不给子查询，只用客户原话检索；
    - 重排：候选里有已废止的旧版就把它排第一——这正是要量的那个风险；
    - 作答：照抄排第一的那段知识；问成立年份的，编一个年份。
    """

    def __init__(self, status: int = 200):
        self.status = status
        self.hosts: list[str] = []
        self.kinds: list[str] = []

    @staticmethod
    def _text(message) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))

    def __call__(self, request):
        self.hosts.append(request.url.host)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "API key is disabled",
                                                               "type": "invalid_request_error"}})
        body = json.loads(request.content)
        messages = body.get("messages") or []
        prompt = "\n".join(self._text(m) for m in messages)
        schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
        props = schema.get("properties") or {}
        if "intent" in props:
            self.kinds.append("intent")
            content = json.dumps({"intent": "query", "confidence": 0.95, "reasoning": "fake"})
        elif "queries" in props:
            self.kinds.append("rewrite")
            content = json.dumps({"queries": []})
        elif "order" in props:
            self.kinds.append("rerank")
            titles = re.findall(r"^\[(\d+)\] (.+)$", prompt, re.M)
            order = sorted(range(len(titles)), key=lambda i: titles[i][1] != STALE_TITLE)
            content = json.dumps({"order": order, "sufficient": True, "missing": ""})
        elif props:
            self.kinds.append("structured")
            content = json.dumps({})
        else:
            self.kinds.append("answer")
            question = self._text(messages[-1]) if messages else ""
            context = next((self._text(m) for m in messages if "[知识库检索结果]" in self._text(m)), "")
            first = re.search(r"内容: (.+)", context)
            if "成立" in question:
                content = "我们公司成立于 1998 年，是本地的老牌橱柜厂。"
            else:
                content = first.group(1) if first else "资料里没有找到。"
            content += "\n[[handoff:no]]"
        return httpx.Response(200, json={
            "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        })


@pytest.fixture
def fake_model(monkeypatch):
    """之后造出来的模型客户端都接到 FakeModel 上。模型连接按 .env 的口径给一份 openai 的。"""
    from app.core import llm_utils

    model = FakeModel()
    original = llm_utils.anthropic_client_kwargs

    def _kwargs(api_key, base_url=None):
        kwargs = original(api_key, base_url)
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(model))
        kwargs["max_retries"] = 0
        return kwargs

    monkeypatch.setattr(llm_utils, "anthropic_client_kwargs", _kwargs)
    monkeypatch.delenv("AI_HELPDESK_MODEL_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fake-model.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    return model


async def test_full_chain_judges_the_final_answer_offline(ev, tmp_path, capsys, fake_model):
    """
    走完整的准备阶段和作答：旧版被重排到第一、答案照抄它，要报"报了旧版"；
    同一条在重排前后都召回了现行版本（hit@3 不变），可答案还是错的——光看排序看不出来。
    """
    code = await ev.main(_args(ev, tmp_path, "ca", full_chain=True, only="v-01,d-02,n-06"))
    assert code == 0

    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert report["mode"] == "full_chain"
    assert (report["model"], report["provider"], report["model_source"]) == ("fake-model", "openai", "env")
    assert report["planned"] == 3 and report["incomplete"] == ""
    cases = {c["id"]: c for c in report["cases"]}

    v01 = cases["v-01"]
    assert v01["retrieval"]["mode"] == "rewrite_rerank"
    assert v01["retrieval"]["after_rerank"][0] == STALE_TITLE
    assert v01["hit_before"] and v01["hit_after"]
    assert (v01["correct"], v01["missing"], v01["stale"]) == (False, ["150美元"], ["120美元"])
    assert v01["stale_in_context"] is True

    assert cases["d-02"]["correct"] is True
    assert cases["n-06"]["outcome"] == "invented" and cases["n-06"]["invented"] == ["1998"]

    summary = report["summary"]
    assert summary["facts"]["n"] == 2 and summary["facts"]["stale_reported"] == 1
    assert summary["unanswerable"]["invented"] == 1
    # 调用数从这一轮的事件里数出来：和假模型实际收到的请求一一对得上
    assert summary["calls"]["total"] == len(fake_model.hosts)
    assert summary["calls"]["by_purpose"]["answer"] == fake_model.kinds.count("answer") == 3
    assert set(fake_model.hosts) == {"fake-model.test"}
    assert "报了旧版 120美元" in capsys.readouterr().out


async def test_full_chain_stops_once_the_credentials_are_rejected(ev, tmp_path, capsys, fake_model):
    """凭证被停用之后后面的用例一条都不再出网，报告标明没跑完，退出码不是 0。"""
    fake_model.status = 401
    code = await ev.main(_args(ev, tmp_path, "ca", full_chain=True, only="d-02,v-01,n-06"))
    assert code == 3

    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert report["incomplete"]
    assert [c["id"] for c in report["cases"]] == ["d-02"]
    assert report["cases"][0]["unavailable"] is True
    assert report["summary"]["unavailable"] == 1 and report["summary"]["facts"]["n"] == 0
    assert len(fake_model.hosts) == 1
    assert "没跑完" in capsys.readouterr().out


async def test_full_chain_refuses_the_smoke_suite(ev, tmp_path):
    with pytest.raises(SystemExit, match="只支持 ca"):
        await ev.main(_args(ev, tmp_path, "smoke", full_chain=True))
