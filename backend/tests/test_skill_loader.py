"""Skill 加载与注入。"""
import logging
import pathlib

import pytest

from core.skill_loader import SkillManager

# ── 英文提问同样要拿到 SOP ────────────────────────────────────────────────────
#
# frontmatter 的 keywords 决定 SOP 注不注入。原来几乎全是中文，实测 23 句英文
# 里 16 句拿不到任何 SOP——Agent 在**没有业务规则**的情况下作答，包括「你不出价」
# 这条。而且 SOP 同时是 AnswerPolicy 判定"有无出处"的依据之一，一起丢了。

_EN_BY_AGENT = {
    "billing": ["How much is a quartz countertop per metre?", "What's the price for an island?",
                "I was charged twice.", "When is the balance due?",
                "There's a duplicate charge on my card."],
    "order": ["Where is my order?", "When will it ship?", "Can I cancel my order?",
              "I never received the drawer slides.", "Is it in production yet?"],
    "technical": ["The hinge is loose.", "My countertop is chipped.", "The door won't close.",
                  "There's a scratch on the finish.", "The upper cabinets don't line up."],
    "general": ["What time does your showroom open?", "Can I book a measure appointment?",
                "What's covered under warranty?", "Do you have samples I can look at?"],
}

_ZH_BY_AGENT = {
    "billing": "石英石台面多少钱一米？",
    "order": "订单 SO-2026-0912 生产到哪一步了？",
    "technical": "柜门关不严，铰链怎么调？",
    "general": "你们展厅几点开门？",
}


@pytest.fixture
def manager():
    root = pathlib.Path(__file__).resolve().parent.parent / "skills"
    m = SkillManager(str(root))
    m.load()
    return m


@pytest.mark.parametrize(
    "agent,message",
    [(a, q) for a, qs in _EN_BY_AGENT.items() for q in qs],
)
def test_english_questions_get_their_sop(manager, agent, message):
    assert manager.prompt_for(message, agent), f"{agent} 的英文提问拿不到 SOP: {message}"


@pytest.mark.parametrize("agent,message", list(_ZH_BY_AGENT.items()))
def test_chinese_questions_still_get_their_sop(manager, agent, message):
    assert manager.prompt_for(message, agent), f"{agent} 的中文提问拿不到 SOP: {message}"


def test_shipped_sops_are_not_truncated(manager):
    """
    截断是从尾部切的，而术语对照表就在尾部——补完术语表订单那份正好越线，
    注入进去的 SOP 会悄悄少掉一整节。
    """
    for skill in manager._skills:
        block = skill.to_prompt_block()
        assert not block.rstrip().endswith("..."), f"{skill.name} 被截断了"


def test_every_sop_carries_the_bilingual_glossary(manager):
    """英文作答时术语要统一，不能每轮现编一个译法或者中英夹注。"""
    for skill in manager._skills:
        assert "术语对照" in skill.content, f"{skill.name} 缺少术语对照表"
        assert "| 中文 | English |" in skill.content


def test_truncation_is_logged(manager, caplog):
    """静默截断查不出来：Agent 少看到几条规则，日志里一点痕迹都没有。"""
    skill = manager._skills[0]
    with caplog.at_level(logging.WARNING):
        block = skill.to_prompt_block(max_chars=200)
    assert block.rstrip().endswith("...")
    assert any("截断" in r.getMessage() for r in caplog.records), "截断没有告警"
