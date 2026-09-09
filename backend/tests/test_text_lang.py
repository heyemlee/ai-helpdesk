"""回复语言判定。策略守卫选安全话术、编排器选脚手架措辞，都用这一份。"""
import pytest

from app.core.text_lang import EN, ZH, detect_language


@pytest.mark.parametrize("text,expected", [
    ("送来的门板有划痕，能补发吗？", ZH),
    ("总共 980 元。", ZH),
    ("The cabinet door arrived damaged.", EN),
    ("How much will the replacement cost?", EN),
])
def test_plain_text_is_classified_by_its_own_language(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize("text,expected", [
    # 英文提问里夹中文型号很常见——按"有没有汉字"判会判成中文，
    # 然后给英文客户回中文、插中文安全话术。
    ("Can you send a replacement for the W3012 橱柜?", EN),
    ("The fabrication fee is 800 元.", EN),
    # 反过来，中文提问里夹英文型号更常见，不能因此判成英文
    ("这个 SKU W3012 有色差吗", ZH),
    ("W3012 这块板子对不齐，需要返工吗？", ZH),
])
def test_mixed_text_follows_the_dominant_language(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize("text", ["", "W3012", "12800", "   ", "$12,800.00"])
def test_undecidable_text_falls_back_to_english(text):
    """
    判不出来时回退英文：英文脚手架对中文模型的干扰，
    小于中文脚手架对英文回复的干扰。
    """
    assert detect_language(text) == EN
