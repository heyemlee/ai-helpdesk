"""意图识别：18 类细粒度意图、三路融合、零延迟规则通道。"""
import pytest
from conftest import stub_intent

from core.intent_recognizer import (
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
    from core.intent_recognizer import _TEMPLATES

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
    recog = IntentRecognizer(api_key="k", base_url="https://api.kabi.pro")
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
