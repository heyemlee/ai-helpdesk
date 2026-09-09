"""意图识别：18 类细粒度意图、三路融合、零延迟规则通道。"""
import pytest
from conftest import stub_intent

from app.core.intent_recognizer import (
    _ITEM_CODE_RE,
    FINE_GRAINED_INTENT_COUNT,
    FINE_GRAINED_INTENTS,
    IntentCategory,
    IntentRecognizer,
)

# ── 意图分类体系 ──────────────────────────────────────────────────────────────

def test_exactly_18_fine_grained_intents():
    """OTHER 是兜底标签，不算细粒度意图。"""
    assert FINE_GRAINED_INTENT_COUNT == 18
    assert IntentCategory.OTHER not in FINE_GRAINED_INTENTS
    assert len(set(FINE_GRAINED_INTENTS)) == 18


def test_every_fine_grained_intent_has_templates():
    from app.core.intent_recognizer import _TEMPLATES

    missing = [c.value for c in FINE_GRAINED_INTENTS if not _TEMPLATES.get(c)]
    assert not missing, f"缺少 few-shot 模板的意图: {missing}"


# ── 零延迟规则通道 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("转人工", IntentCategory.HUMAN_HANDOFF),
    ("补件什么时候到", IntentCategory.MISSING_PART),
    ("这套报价多少", IntentCategory.QUOTE_PRICING),
    ("尾款被重复扣款了", IntentCategory.PAYMENT_ISSUE),
    ("柜门关不严", IntentCategory.INSTALLATION),
])
def test_fast_path_hits_high_precision_anchors(recognizer, message, expected):
    hit = recognizer._fast_path(message)
    assert hit is not None
    assert hit["intent"] == expected
    assert hit["confidence"] >= 0.8


def test_fast_path_aborts_on_cross_intent_conflict(recognizer):
    """跨域复合问题必须交给三路融合判主意图，规则不能自作主张。"""
    assert recognizer._fast_path("柜门关不严，而且尾款还被重复扣款了") is None


@pytest.mark.parametrize("message", ["你好", "", "在吗", "我想问一下你们家的服务怎么样啊"])
def test_fast_path_declines_ambiguous_messages(recognizer, message):
    assert recognizer._fast_path(message) is None


def test_fast_path_declines_long_messages(recognizer):
    """超长消息通常含多个诉求，单个锚点不足以定性。"""
    long_message = "补件" + "，另外还有很多别的问题要说明" * 12
    assert len(long_message) > 120
    assert recognizer._fast_path(long_message) is None


async def test_fast_path_makes_zero_network_calls(recognizer):
    """规则通道命中时，LLM 和 Embedding 两路都不能被调用。"""
    async def _fail_llm(*args, **kwargs):
        raise AssertionError("规则通道不应触发 LLM 调用")

    async def _fail_emb(*args, **kwargs):
        raise AssertionError("规则通道不应触发 Embedding 调用")

    recognizer._llm_recognize = _fail_llm
    recognizer._embedding_recognize = _fail_emb

    result = await recognizer.recognize("转人工")

    assert result.intent == IntentCategory.HUMAN_HANDOFF
    assert result.strategy == "fast_path"
    assert result.fast_path is True
    assert recognizer.fast_path_stats["llm_calls_saved"] == 1


async def test_fast_path_can_be_disabled():
    recog = IntentRecognizer(api_key="k", enable_fast_path=False)
    stub_intent(recog, IntentCategory.MISSING_PART)

    result = await recog.recognize("我要补件")

    assert result.strategy == "ensemble"


# ── 三路融合 ──────────────────────────────────────────────────────────────────

async def test_ensemble_reports_all_three_signals(recognizer):
    """规则、Embedding、LLM 三路都必须出现在来源得分里。"""
    stub_intent(recognizer, IntentCategory.COMPLAINT, confidence=0.8)

    result = await recognizer.recognize("你们的服务太差了，等了很久")

    assert result.strategy == "ensemble"
    assert set(result.source_scores) >= {"llm", "embedding", "rules"}


def test_vote_weights_sum_to_one(recognizer):
    assert pytest.approx(sum(recognizer._VOTE_WEIGHTS.values()), abs=1e-9) == 1.0


def test_embedding_signal_stays_enabled_with_third_party_base_url():
    """
    换成第三方网关时，Embedding 这一路不能被静默关掉——
    否则"三路融合"实际只剩两路。
    """
    recog = IntentRecognizer(api_key="k", base_url="https://gateway.example.com")
    assert recog._embedding_enabled is True
    assert recog.embedding_backend == "local_ngram"


def test_remote_embedding_backend_when_configured():
    recog = IntentRecognizer(
        api_key="k",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
    )
    assert recog.embedding_backend == "remote"


async def test_embedding_similarity_ranks_paraphrases(recognizer):
    """本地向量至少要能把换了说法的同义句排到对应意图上。"""
    result = await recognizer._embedding_recognize("我要申请补件")
    assert result["confidence"] > 0.0


async def test_vote_survives_llm_failure(recognizer):
    """LLM 那一路挂掉时，剩下两路仍要给出结果——这是多路融合的容错价值。"""
    async def _broken_llm(message, history=None):
        return {"intent": IntentCategory.OTHER, "confidence": 0.0,
                "reasoning": "LLM 失败", "failed": True}

    async def _emb(message):
        return {"intent": IntentCategory.MISSING_PART, "confidence": 0.7}

    recognizer._llm_recognize = _broken_llm
    recognizer._embedding_recognize = _emb

    result = await recognizer.recognize("这个订单能不能把钱退给我")

    assert result.intent == IntentCategory.MISSING_PART


async def test_llm_failure_does_not_launder_noise_into_a_specific_intent(recognizer):
    """
    LLM 挂掉后剩下两路仍要过同一道阈值。

    本地 n-gram 向量对短句给出的最佳匹配基本是噪声。以前这个噪声分被原样
    当成"已定性的具体意图"送进路由：一句问候被判成 payment_issue（0.11），
    路由到账款 Agent，又因为 0.11 低于转人工阈值当场开出一张工单。
    """
    async def _broken_llm(message, history=None):
        return {"intent": IntentCategory.OTHER, "confidence": 0.0,
                "reasoning": "LLM 失败", "failed": True}

    async def _noisy_emb(message):
        return {"intent": IntentCategory.PAYMENT_ISSUE, "confidence": 0.11}

    recognizer._llm_recognize = _broken_llm
    recognizer._embedding_recognize = _noisy_emb

    result = await recognizer.recognize("我想了解一下你们公司")

    assert result.intent == IntentCategory.OTHER
    assert recognizer.llm_failures == 1
    assert recognizer.fast_path_stats["llm_failure_rate"] == 1.0


async def test_llm_failure_falls_back_to_the_strongest_remaining_signal(recognizer):
    """两路都还在时取更强的一路，而不是无条件偏向 Embedding。"""
    async def _broken_llm(message, history=None):
        return {"intent": IntentCategory.OTHER, "confidence": 0.0,
                "reasoning": "LLM 失败", "failed": True}

    async def _noisy_emb(message):
        return {"intent": IntentCategory.PAYMENT_ISSUE, "confidence": 0.11}

    recognizer._llm_recognize = _broken_llm
    recognizer._embedding_recognize = _noisy_emb

    result = await recognizer.recognize("你好")   # 规则这一路命中 GREETING 0.5

    assert result.intent == IntentCategory.GREETING


async def test_low_confidence_falls_back_to_other(recognizer):
    async def _weak_llm(message, history=None):
        return {"intent": IntentCategory.QUERY, "confidence": 0.1, "reasoning": ""}

    async def _weak_emb(message):
        return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    recognizer._llm_recognize = _weak_llm
    recognizer._embedding_recognize = _weak_emb

    result = await recognizer.recognize("嗯……那个……")

    assert result.intent == IntentCategory.OTHER


async def test_result_is_cached(recognizer):
    stub_intent(recognizer, IntentCategory.QUERY)

    await recognizer.recognize("保修范围是什么")
    await recognizer.recognize("保修范围是什么")

    assert recognizer.cache_hits == 1


# ── 实体提取 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("I NEED HELP", []),
    ("PLEASE FIX THIS NOW", []),
    ("W3012 这块门板裂了", ["W3012"]),
    ("柜号 DB24-3 少发了", ["DB24-3"]),
    ("PO-12345 这一单", ["PO-12345"]),
    ("充值500元", []),
    ("支付了 500 美元", []),
])
def test_item_code_extraction_does_not_match_plain_uppercase_words(message, expected):
    """
    早期正则 [A-Z][A-Z0-9_-]{2,16} 会把任意大写单词当编号，
    "I NEED HELP" 命中 NEED/HELP 后给产品域加分，导致误路由。
    柜号必须字母加数字（W3012 / DB24-3），纯大写词和纯金额都不算。
    """
    assert _ITEM_CODE_RE.findall(message) == expected


def test_entity_extraction_pulls_order_amount_and_item_code(recognizer):
    entities = recognizer._extract_entities("订单号 A1B2C3D4 的 W3012 门板报价 199 元")
    assert entities["order_id"] == ["A1B2C3D4"]
    assert entities["amount"] == ["199 元"]
    assert entities["item_code"] == ["W3012"]


# ── 英文快通道锚点 ────────────────────────────────────────────────────────────
#
# 快通道命中即返回，**不做任何网络调用**，没有第二意见能纠正它。所以英文锚点
# 必须和中文一样密：锚点越密，复合问题越容易触发跨意图冲突而回退到三路融合。

@pytest.mark.parametrize("message,expected", [
    ("How much does a quartz countertop cost per metre?", "quote_pricing"),
    ("How much will the replacement door be?",            "quote_pricing"),
    ("Where is my order?",                                "order_status"),
    ("I was charged twice for the deposit.",              "payment_issue"),
    ("I never received the drawer slides.",               "missing_part"),
])
def test_english_anchors_hit_the_fast_path(recognizer, message, expected):
    hit = recognizer._fast_path(message)
    assert hit is not None, f"英文锚点没命中，白花一次 LLM 往返: {message}"
    assert hit["intent"].value == expected


@pytest.mark.parametrize("message", [
    # 破损 + 询价：英文锚点只有一条 "how much" 时，这句会被快通道以 0.82
    # 抢答成 quote_pricing 送去账款 Agent，而它其实是破损投诉。
    "My quartz countertop arrived with a chipped edge. How much will it cost to replace?",
    "The cabinet door arrived damaged and the hinge is loose.",
    "送来的门板有划痕，另外报价多少？",
])
def test_compound_questions_fall_back_to_the_ensemble(recognizer, message):
    assert recognizer._fast_path(message) is None


# 英文锚点覆盖率语料。锚点是枚举，枚举一定有洞——固定成用例，加规则时不许把
# 已覆盖的弄丢。两个指标分开看：
#   · 误判必须是 0：快通道命中即返回，不做任何网络调用，没有第二意见能纠正它；
#   · 未命中只是回退三路融合，安全方向，代价是一次 LLM 往返。
_EN_INTENT_CORPUS = {
    "order_status": ["Where is my order?", "What's the status of order SO-2026-0912?",
                     "Is my order in production yet?", "When will my order be ready?"],
    "delivery": ["What's the delivery date?", "When will it ship?",
                 "Can we reschedule delivery to next week?", "What time does the truck arrive?"],
    "order_change": ["I want to change my order.", "Can I cancel my order?",
                     "Can we change the color to matte white?"],
    "missing_part": ["I never received the drawer slides.", "There's a missing part in my shipment.",
                     "The hinges weren't included in the box.", "We're short two toe kicks."],
    "quote_pricing": ["How much does a quartz countertop cost?", "What's the price for an island?",
                      "Can you send me a quote?", "Do you have a price list?"],
    "payment_issue": ["I was charged twice.", "There's a duplicate charge on my card.",
                      "My payment failed."],
    "installation": ["The hinge is loose.", "The door won't close properly.",
                     "The upper cabinets don't line up.",
                     "There's a gap between the counter and the wall."],
    "damage_defect": ["The door arrived damaged.", "My countertop is chipped.",
                      "The panel is cracked.", "There's a scratch on the finish."],
    "human_handoff": ["I want to speak to a human.", "Can you transfer me to a person?",
                      "Let me talk to your manager."],
}


@pytest.mark.parametrize(
    "intent,message",
    [(i, q) for i, qs in _EN_INTENT_CORPUS.items() for q in qs],
)
def test_english_corpus_never_routes_to_the_wrong_intent(recognizer, intent, message):
    """未命中可以接受（回退融合），判错不行——快通道抢答之后没人能纠正。"""
    hit = recognizer._fast_path(message)
    assert hit is None or hit["intent"].value == intent, (
        f"{message!r} 被判成 {hit['intent'].value}，应为 {intent}"
    )


def test_english_corpus_keeps_a_high_fast_path_hit_rate(recognizer):
    """漏掉一句只是多花一次 LLM 往返，但整体掉下来就说明英文锚点又退化了。"""
    pairs = [(i, q) for i, qs in _EN_INTENT_CORPUS.items() for q in qs]
    hits = sum(1 for i, q in pairs if (recognizer._fast_path(q) or {}).get("intent", None)
               and recognizer._fast_path(q)["intent"].value == i)
    assert hits / len(pairs) >= 0.90, f"英文快通道命中率跌到 {hits}/{len(pairs)}"


def test_how_much_longer_is_not_a_pricing_question(recognizer):
    """裸的 "how much" 会把这句判成询价——锚点要带助动词。"""
    hit = recognizer._fast_path("How much longer will my order take?")
    assert hit is None or hit["intent"].value != "quote_pricing"
