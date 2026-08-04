"""
混合检索（稠密 + 稀疏 RRF）与检索自纠错的行为测试。

这一层测的是"检索为什么召得回/召不回"，全部不发网络请求：
稀疏检索和 RRF 是纯计算，自纠错的控制流用打桩的重排结果驱动。
"""
import pytest

from app.core.lexical import bm25_scores, index_text, query_tokens, tokenize
from app.core.reranker import Reranker
from app.storage.vector_store import LocalStore
from app.tools.knowledge_base import (
    KnowledgeBase,
    fuse_candidates,
    lexical_first_fuse,
    rrf_fuse,
)
from app.tools.tool_manager import RerankOutcome, RerankPayload, Tool, ToolManager

# ── 分词 ──────────────────────────────────────────────────────────────────────

def test_cjk_is_tokenized_into_bigrams():
    """中文切二元组：单字歧义太大，二元组不用词典就能逼近词级匹配。"""
    assert tokenize("补件流程") == ["补件", "件流", "流程"]
    assert tokenize("柜") == ["柜"]


def test_ascii_runs_split_on_punctuation_and_lowercase():
    """订单号这类精确串按字母数字段切开，大小写归一。"""
    assert tokenize("SO-2024-0117") == ["so", "2024", "0117"]


def test_mixed_text_keeps_both_sides():
    assert tokenize("订单 SO-99 缺件") == ["订单", "so", "99", "缺件"]


def test_query_tokens_are_deduped():
    assert query_tokens("补件 补件") == ["补件"]


def test_tokenize_ignores_punctuation_only_input():
    assert tokenize("！？——…… ") == []


def test_index_text_covers_title():
    """标题往往是最凝练的关键词，正文里未必原样出现。"""
    assert "补件" in index_text("补件流程", "缺件需登记")


# ── BM25 ──────────────────────────────────────────────────────────────────────

def test_bm25_scores_only_documents_that_match():
    docs = [tokenize("补件流程说明"), tokenize("送货排期说明")]
    scores = bm25_scores(tokenize("补件"), docs)
    assert scores[0] > 0
    assert scores[1] == 0


def test_bm25_idf_never_goes_negative_on_small_corpora():
    """
    小语料上，出现在多数文档里的词会让教科书版 idf 变负，
    结果是"命中得越多分越低"。夹住下界后不会出现这种反常。
    """
    docs = [tokenize("交期说明"), tokenize("交期说明"), tokenize("交期说明")]
    assert all(s >= 0 for s in bm25_scores(tokenize("交期"), docs))


# ── RRF 融合 ──────────────────────────────────────────────────────────────────

def test_rrf_ranks_dual_source_hit_above_single_source_top1():
    """
    融合的核心行为：两路都认可 > 一路极度认可。
    b 在两路都是第 2，应当排在只有稠密第 1 的 a 前面。
    """
    dense = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.7}]
    lexical = [{"id": "c", "lex_score": 3.0}, {"id": "b", "lex_score": 2.0}]
    fused = rrf_fuse({"dense": dense, "lexical": lexical}, top_k=3)
    assert [row["id"] for row in fused][0] == "b"
    assert fused[0]["retrieval"] == "hybrid"


def test_rrf_labels_single_source_results():
    fused = rrf_fuse({"dense": [{"id": "a"}], "lexical": [{"id": "b"}]}, top_k=2)
    assert {row["id"]: row["retrieval"] for row in fused} == {"a": "dense", "b": "lexical"}


def test_rrf_keeps_real_cosine_score_from_whichever_side_has_it():
    """稀疏路给了 0 分不该覆盖稠密路的真实相似度——下游要靠它判证据是否充分。"""
    fused = rrf_fuse(
        {"lexical": [{"id": "a", "score": 0.0}], "dense": [{"id": "a", "score": 0.83}]},
        top_k=1,
    )
    assert fused[0]["score"] == 0.83


def test_rrf_truncates_to_top_k():
    rows = [{"id": str(i)} for i in range(10)]
    assert len(rrf_fuse({"dense": rows}, top_k=3)) == 3


# ── 本地向量：关键词在前，向量补位 ────────────────────────────────────────────

def _row(key: str, **extra) -> dict:
    return {"id": key, "title": key, "content": f"{key} 的正文", **extra}


def test_lexical_first_keeps_keyword_order_and_appends_dense_only_hits():
    """
    本地 n-gram 向量和关键词一路量的是同一种东西（字符重合），不是独立的判断。
    让它投票，两路都排在后面的 b 会压过关键词一路的第 1 名 a。
    """
    dense = [_row("b", score=0.2), _row("c", score=0.1)]
    lexical = [_row("a", lex_score=3.0, score=0.05), _row("b", lex_score=1.0, score=0.2)]

    fused = lexical_first_fuse(dense, lexical)

    assert [r["id"] for r in fused] == ["a", "b", "c"]
    assert [r["retrieval"] for r in fused] == ["lexical", "hybrid", "dense"]
    # 同样的输入交给 RRF：b 两路都有，排到了 a 前面
    assert [r["id"] for r in rrf_fuse({"dense": dense, "lexical": lexical}, top_k=3)][0] == "b"


def test_lexical_first_falls_back_to_dense_when_nothing_matches_literally():
    """问句和文档没有共同的字面时，关键词一路是空的：这时向量结果就是全部结果。"""
    dense = [_row("b", score=0.2), _row("c", score=0.1)]
    assert [r["id"] for r in lexical_first_fuse(dense, [])] == ["b", "c"]


def test_fusion_follows_whether_the_vectors_carry_meaning():
    dense = [_row("b", score=0.2), _row("c", score=0.1)]
    lexical = [_row("a", score=0.05), _row("b", score=0.2)]

    assert [r["id"] for r in fuse_candidates(dense, lexical, semantic=True)][0] == "b"    # RRF
    assert [r["id"] for r in fuse_candidates(dense, lexical, semantic=False)][0] == "a"   # 关键词在前


def test_fusion_drops_repeated_passages_whichever_way_it_ranks():
    """同名文档的两个版本共用一段原文：检索结果里这段文字只出现一次。"""
    twin = {"title": "保修政策", "content": "柜体质保 5 年"}
    dense = [{"id": "v1", **twin, "score": 0.3}]
    lexical = [{"id": "v2", **twin, "score": 0.3}, _row("other", score=0.1)]

    for semantic in (True, False):
        fused = fuse_candidates(dense, lexical, semantic=semantic)
        assert sorted(r["title"] for r in fused) == ["other", "保修政策"]


# ── 存储层稀疏检索 ────────────────────────────────────────────────────────────

@pytest.fixture
async def kb(tmp_path):
    store = LocalStore(db_path=str(tmp_path / "kb.db"))
    from app.core.embedder import Embedder
    yield KnowledgeBase(store=store, embedder=Embedder())
    await store.close()


async def test_lexical_search_finds_exact_domain_term(kb):
    await kb.add_documents_async([
        {"title": "安装调整", "content": "柜门关不严绝大多数是铰链未调到位"},
        {"title": "送货预约", "content": "送货需提前预约并具备卸货条件"},
    ])
    embedding = await kb._embedder.embed("铰链")
    hits = await kb._store.kb_search_lexical(query_tokens("铰链"), embedding, top_k=5)
    assert [h["title"] for h in hits] == ["安装调整"]


async def test_lexical_search_finds_order_id(kb):
    """
    订单号几乎没有语义可言，稠密向量对它基本无能为力——
    这类精确串正是补一路关键词检索的主要理由。
    """
    await kb.add_documents_async([
        {"title": "工单记录", "content": "订单 SO-2024-0117 已进入排产队列"},
        {"title": "交期说明", "content": "交期从图纸确认日起算"},
    ])
    embedding = await kb._embedder.embed("SO-2024-0117")
    hits = await kb._store.kb_search_lexical(query_tokens("SO-2024-0117"), embedding, top_k=5)
    assert [h["title"] for h in hits] == ["工单记录"]


async def test_lexical_search_is_empty_for_untokenizable_query(kb):
    await kb.add_documents_async([{"title": "交期", "content": "标准交期 4 周"}])
    embedding = await kb._embedder.embed("？？？")
    assert await kb._store.kb_search_lexical(query_tokens("？？？"), embedding, top_k=5) == []


async def test_hybrid_search_reports_which_leg_recalled_each_hit(kb):
    await kb.ensure_seeded()
    results = await kb.search_async("补件需要提供什么", top_k=3)
    assert results
    assert all(r["retrieval"] in {"dense", "lexical", "hybrid"} for r in results)
    # 余弦下界是 -1 而不是 0：本地 n-gram 兜底向量是带符号的哈希，
    # 毫不相关的两段文本会算出小的负相似度。
    assert all(-1 <= r["score"] <= 1 for r in results)


async def test_hybrid_rescues_a_hit_dense_alone_would_miss(kb):
    """
    这是补一路关键词检索的理由，钉成回归用例：
    "补件需要提供什么材料" 在纯稠密召回里排第 4（掉出 top-3，等于喂给 Agent 的
    出处里根本没有它），关键词那一路排第 1，所以最终结果里有它。
    完整对照见 scripts/eval_retrieval.py --suite smoke：这 8 条里纯稠密进前 3 的
    有 6 条，关键词 7 条。
    """
    await kb.ensure_seeded()
    gold = "缺件漏发与补件流程"
    query = "补件需要提供什么材料"

    embedding = await kb._embedder.embed(query)
    dense = await kb._store.kb_search(embedding, 10)
    assert gold not in [r["title"] for r in dense[:3]]

    assert gold in [r["title"] for r in await kb.search_async(query, top_k=3)]


async def test_local_vectors_do_not_push_the_keyword_winner_out_of_the_top_three(kb):
    """
    语料长到几十个片段之后，本地 n-gram 向量参与 RRF 投票会帮倒忙："报价单有效期是多久"
    的目标文档在关键词一路排第 1，融合之后掉到第 5，交给 Agent 的前三条里没有它。
    本地向量因此不参与排序。完整对照见 scripts/eval_retrieval.py（默认的 ca 套）
    和 docs/evaluation/retrieval-ca-factory.md。
    """
    import json
    import pathlib

    corpus = (pathlib.Path(__file__).resolve().parents[1]
              / "data" / "demo_docs" / "sample_ca_factory_knowledge.json")
    await kb.ensure_seeded()
    await kb.add_documents_async(json.loads(corpus.read_text(encoding="utf-8")))
    gold = "报价有效期与价格调整"
    query = "报价单有效期是多久"

    embedding = await kb._embedder.embed(query)
    dense = await kb._store.kb_search(embedding, 10)
    lexical = await kb._store.kb_search_lexical(query_tokens(query), embedding, 10)
    assert lexical[0]["title"] == gold
    voted = rrf_fuse({"dense": dense, "lexical": lexical}, top_k=20)
    assert gold not in [r["title"] for r in voted[:3]]      # 两路各一票的结果

    assert (await kb.search_async(query, top_k=3))[0]["title"] == gold


async def test_backfill_builds_lexical_index_for_pre_upgrade_chunks(kb):
    """
    升级前写入的片段 tokens 为空，只能被稠密路召回。症状是"新文档关键词
    搜得到、老文档搜不到"，很难往索引上想，所以启动时必须回填。
    """
    await kb.add_documents_async([{"title": "保修范围", "content": "五金件质保 3 年"}])
    embedding = await kb._embedder.embed("五金件")

    # 清空 tokens，把这条片段还原成升级前的样子
    stored = await kb._store.kb_search(embedding, top_k=1)
    await kb._store.kb_set_tokens([(stored[0]["id"], "")])
    assert await kb._store.kb_search_lexical(query_tokens("五金件"), embedding, top_k=5) == []

    assert await kb.backfill_lexical() == 1
    hits = await kb._store.kb_search_lexical(query_tokens("五金件"), embedding, top_k=5)
    assert [h["title"] for h in hits] == ["保修范围"]
    assert await kb.backfill_lexical() == 0   # 幂等


# ── 重排：cross-encoder 优先，LLM 回落 ────────────────────────────────────────

def test_reranker_disabled_without_config():
    """没配端点就是禁用，调用方回落 LLM 重排，行为与加它之前一致。"""
    assert Reranker().enabled is False
    assert Reranker().backend == "llm_listwise"
    assert Reranker(base_url="https://x/v1", api_key="k").enabled is True


async def test_cross_encoder_reorders_and_llm_is_not_called():
    class FakeReranker:
        enabled = True

        async def rerank(self, query, documents, top_k):
            return [(2, 0.9), (0, 0.4)]

    manager = ToolManager(api_key="test-key", reranker=FakeReranker())
    manager._llm_rerank = _boom_llm

    items = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    outcome = await manager._rerank("q", items, top_k=2)
    assert [i["title"] for i in outcome.items] == ["c", "a"]
    assert outcome.reranked is True
    assert outcome.sufficient is None   # cross-encoder 判不了充分性，交给分数阈值


async def test_rerank_is_skipped_when_there_is_nothing_to_reorder():
    manager = ToolManager(api_key="test-key")
    manager._llm_rerank = _boom_llm
    outcome = await manager._rerank("q", [{"title": "a"}], top_k=3)
    assert outcome.reranked is False   # 候选数 <= top_k，没有可排的东西


async def _boom_llm(*args, **kwargs):
    raise AssertionError("不该调用 LLM 重排")


# ── 重排响应解析 ──────────────────────────────────────────────────────────────

class _FakeParseMessages:
    """按结构化输出的形状返回：parsed_output 是一个已校验的 Pydantic 实例。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return type("R", (), {"parsed_output": self.payload, "stop_reason": "end_turn"})()


def _manager_returning(payload) -> ToolManager:
    manager = ToolManager(api_key="test-key")
    manager._client = type("C", (), {"messages": _FakeParseMessages(payload)})()
    return manager


async def test_llm_rerank_applies_order_and_verdict():
    manager = _manager_returning(
        RerankPayload(order=[2, 0], sufficient=False, missing="缺少补件时效")
    )

    items = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    outcome = await manager._llm_rerank("q", items, top_k=2)

    assert [i["title"] for i in outcome.items] == ["c", "a"]
    assert outcome.reranked is True
    assert outcome.sufficient is False
    assert outcome.missing == "缺少补件时效"


async def test_llm_rerank_drops_out_of_range_indices():
    """
    schema 保证得了"是一个整数数组"，保证不了每个整数落在候选范围内。

    不过滤的话越界索引会 IndexError 掉进 except，把一次本来可用的重排
    整个降级成"返回原始顺序"。
    """
    manager = _manager_returning(RerankPayload(order=[9, 1, -1], sufficient=True, missing=""))

    items = [{"title": "a"}, {"title": "b"}]
    outcome = await manager._llm_rerank("q", items, top_k=2)

    assert [i["title"] for i in outcome.items] == ["b"]
    assert outcome.reranked is True


async def test_llm_rerank_sends_no_sampling_parameters():
    """SDK 1.x 下 temperature 是 TypeError，会被 except 吃掉后静默退回原始顺序。"""
    manager = _manager_returning(RerankPayload(order=[0], sufficient=True, missing=""))

    await manager._llm_rerank("q", [{"title": "a"}, {"title": "b"}], top_k=1)

    sent = manager._client.messages.calls[0]
    assert not {"temperature", "top_p", "top_k"} & sent.keys()
    assert sent["output_format"] is RerankPayload


def _rerank_prompt(manager: ToolManager) -> str:
    return manager._client.messages.calls[0]["messages"][0]["content"]


async def test_llm_rerank_shows_the_model_the_end_of_each_passage():
    """
    条款的条件和例外多半写在后半段。以前每条候选只给序列化后的前 200 个字符，
    模型是在没读到"已开料的不退"的情况下排的序、判的"证据够不够"。
    """
    clause = "已开料的订单不能取消，客户须支付已完成部分的材料和人工费用"
    passage = "签约后 3 个工作日内且订单尚未排产的，可以无条件取消。" * 12 + clause
    assert 300 < len(passage) <= 500   # 一个正常分片的长度，结论在最后一句
    manager = _manager_returning(RerankPayload(order=[0, 1], sufficient=True, missing=""))

    await manager._llm_rerank(
        "开料了还能退吗",
        [{"title": "订单取消与定金退还", "content": passage, "score": 0.12, "doc_id": "doc_1"},
         {"title": "交期说明", "content": "标准线的标准交期为 4 周"}],
        top_k=1,
    )

    prompt = _rerank_prompt(manager)
    assert clause in prompt
    assert "[0] 订单取消与定金退还" in prompt and "[1] 交期说明" in prompt
    # 给模型的是标题和正文，不是整条记录：分数和出处帮不上"相不相关"的判断
    assert "doc_1" not in prompt and "score" not in prompt


async def test_llm_rerank_caps_how_much_of_a_passage_it_sends():
    """prompt 的长度随候选数线性增长，单条候选得有个上限。"""
    from app.tools.tool_manager import PASSAGE_CHARS

    manager = _manager_returning(RerankPayload(order=[0], sufficient=True, missing=""))
    long_passage = "头" * PASSAGE_CHARS + "尾巴不该出现"

    await manager._llm_rerank(
        "q", [{"title": "长文", "content": long_passage}, {"title": "b", "content": "短"}], top_k=1
    )

    prompt = _rerank_prompt(manager)
    assert "头" * PASSAGE_CHARS in prompt
    assert "尾巴不该出现" not in prompt


async def test_llm_rerank_still_handles_candidates_that_are_not_passages():
    """重排是工具层的通用能力，候选不一定是 {title, content}。"""
    manager = _manager_returning(RerankPayload(order=[1, 0], sufficient=True, missing=""))

    outcome = await manager._llm_rerank("q", [{"sku": "W3036"}, "纯文本候选"], top_k=2)

    prompt = _rerank_prompt(manager)
    assert '[0] {"sku": "W3036"}' in prompt and "[1] " in prompt and "纯文本候选" in prompt
    assert outcome.items == ["纯文本候选", {"sku": "W3036"}]


# ── 证据充分性判定 ────────────────────────────────────────────────────────────

def test_model_verdict_wins_over_score_threshold():
    outcome = RerankOutcome(items=[{"score": 0.95}], reranked=True, sufficient=False)
    assert ToolManager._evidence_ok(outcome) is False


def test_score_threshold_is_used_when_model_gave_no_verdict():
    assert ToolManager._evidence_ok(RerankOutcome([{"score": 0.8}], True)) is True
    assert ToolManager._evidence_ok(RerankOutcome([{"score": 0.1}], True)) is False


def test_empty_result_is_never_sufficient():
    assert ToolManager._evidence_ok(RerankOutcome([], False, sufficient=True)) is False


def test_scoreless_items_are_treated_as_sufficient():
    """取不到分数就别拿一个假信号去多打一整轮检索。"""
    assert ToolManager._evidence_ok(RerankOutcome([{"title": "a"}], True)) is True


# ── 检索自纠错 ────────────────────────────────────────────────────────────────

def _search_manager(verdicts):
    """构造一个把改写和重排都打好桩的 manager，verdicts 按轮次给出充分性判定。"""
    manager = ToolManager(api_key="test-key")
    manager.register(Tool(
        name="kb",
        description="stub",
        handler=_stub_handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        supports_rerank=True,
    ))

    calls = {"rewrite": [], "rerank": 0}

    async def _rewrite(query, n=3, hint=""):
        calls["rewrite"].append(hint)
        return [f"{query}-{hint or 'base'}"]

    async def _rerank(query, items, top_k):
        verdict = verdicts[min(calls["rerank"], len(verdicts) - 1)]
        calls["rerank"] += 1
        return RerankOutcome(items=items[:top_k], reranked=True, sufficient=verdict,
                             missing="补件时效" if verdict is False else "")

    manager.rewrite_query = _rewrite
    manager._rerank = _rerank
    return manager, calls


def test_merge_unique_dedupes_same_content_with_different_scores():
    """
    同一 chunk 被三个改写子查询召回，分数各不相同。之前 md5(str(item)) 把 score
    算进身份，去重从来没生效过——top-5 里同一段出现三次。
    """
    items = [
        {"title": "保修", "content": "柜体保修 5 年", "score": -0.0154},
        {"title": "安装", "content": "门缝 2-3mm", "score": 0.1283},
        {"title": "保修", "content": "柜体保修 5 年", "score": 0.0361},
        {"title": "保修", "content": "柜体保修 5 年", "score": 0.0068},
        {"title": "安装", "content": "门缝 2-3mm", "score": 0.0},
    ]
    merged = ToolManager._merge_unique(items)
    assert [m["content"] for m in merged] == ["柜体保修 5 年", "门缝 2-3mm"]   # 保序
    assert merged[0]["score"] == 0.0361      # 同内容保留最高分
    assert merged[1]["score"] == 0.1283
    assert items[0]["score"] == -0.0154      # 不改动入参


def test_merge_unique_falls_back_to_id_then_whole_item():
    by_id = ToolManager._merge_unique([{"id": "a", "score": 0.1}, {"id": "a", "score": 0.9}])
    assert len(by_id) == 1 and by_id[0]["score"] == 0.9
    plain = ToolManager._merge_unique(["x", "x", "y"])
    assert plain == ["x", "y"]
    scoreless = ToolManager._merge_unique([{"foo": 1, "score": 1}, {"foo": 1, "score": 2}, {"foo": 2}])
    assert len(scoreless) == 2


async def _stub_handler(params, context):
    # 内容跟着查询走：候选的身份是内容（入库与去重同一口径），
    # 常量内容会让两轮召回被当成同一条合并掉——那是夹具失真，不是链路的问题。
    return [{"title": params["query"], "content": f"{params['query']} 的内容", "score": 0.9}]


async def test_sufficient_evidence_short_circuits_the_loop():
    manager, calls = _search_manager([True])
    result = await manager.search_with_rewrite("kb", "补件流程", top_k=3)
    assert result.corrected is False
    assert result.evidence_ok is True
    assert calls["rewrite"] == [""]      # 只改写了一次，没有补检索


async def test_insufficient_evidence_triggers_exactly_one_extra_round():
    """
    回路的作用是捞回"检索角度没选对"的那一类，不是无限逼近。
    第二轮仍然不足就交给转人工，检索不再空转。
    """
    manager, calls = _search_manager([False, False])
    result = await manager.search_with_rewrite("kb", "补件流程", top_k=3)
    assert result.corrected is True
    assert result.evidence_ok is False
    assert calls["rerank"] == 2
    assert calls["rewrite"] == ["", "补件时效"]   # 第二轮带着缺口去改写


async def test_correction_round_merges_instead_of_replacing_candidates():
    """第一轮的结果未必没用，只是不够——补检索是补充候选池，不是推翻它。"""
    manager, calls = _search_manager([False, True])
    result = await manager.search_with_rewrite("kb", "补件流程", top_k=5)
    titles = [item["title"] for item in result.data]
    assert "补件流程-base" in titles and "补件流程-补件时效" in titles
    assert result.evidence_ok is True


async def test_self_correct_can_be_turned_off():
    manager, calls = _search_manager([False])
    result = await manager.search_with_rewrite("kb", "补件流程", top_k=3, self_correct=False)
    assert result.corrected is False
    assert calls["rerank"] == 1


async def test_empty_recall_fails_without_burning_a_correction_round():
    manager, calls = _search_manager([True])

    async def _empty(params, context):
        return []

    manager._tools["kb"].handler = _empty
    result = await manager.search_with_rewrite("kb", "补件流程", top_k=3)
    assert result.success is False
    assert calls["rerank"] == 0


# ── 工具统计口径 ──────────────────────────────────────────────────────────────

async def test_in_flight_call_does_not_dilute_success_rate():
    """
    在途请求不能把成功率拉成 0。

    total 如果在调用开始就加、success 等结束才加，那么一个跑十几秒的工具
    （order_data_query 这类跨服务委派）在 Monitor 的 10s 采样窗口里几乎必然
    被抓在在途状态，成功率报成 0.000，触发一串假告警。Agent 侧已经这么修过。
    """
    import asyncio

    manager = ToolManager(api_key="test-key")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(params, context):
        started.set()
        await release.wait()
        return ["ok"]

    manager.register(Tool(
        name="slow", description="慢工具", handler=_slow,
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
    ))
    stats = manager._tools["slow"].stats

    task = asyncio.create_task(manager.call("slow", {"query": "q"}))
    await started.wait()
    assert stats.success_rate == 1.0, "在途请求不该被记成失败"
    assert stats.total == 0, "total 要等调用结束才记"

    release.set()
    await task
    assert (stats.total, stats.success) == (1, 1)


async def test_failed_call_is_counted():
    """结束时记账不能把失败漏掉。"""
    manager = ToolManager(api_key="test-key")

    async def _boom(params, context):
        raise RuntimeError("下游挂了")

    manager.register(Tool(
        name="boom", description="必失败", handler=_boom,
        schema={"type": "object", "properties": {}},
    ))
    await manager.call("boom", {})
    stats = manager._tools["boom"].stats
    assert (stats.total, stats.failed, stats.success_rate) == (1, 1, 0.0)
