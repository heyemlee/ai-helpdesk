"""记忆上下文进 Prompt 的形态——重点是跨会话召回内容的使用边界。"""
from memory.conversation_memory import MemoryContext, Message, MsgRole


def _ctx(**kw) -> MemoryContext:
    return MemoryContext(
        recent_messages=kw.get("recent_messages", []),
        relevant_history=kw.get("relevant_history", []),
        user_profile=kw.get("user_profile", {}),
        summary=kw.get("summary", ""),
    )


class TestCrossSessionRecallIsLabelled:
    """
    跨会话召回必须标注清楚，否则模型会拿它否定客户。

    实测过的故障：客户在新会话里问 J26-00001 的欠款，模型从相关历史里读到上一个
    会话的 J26-00136，认定那才是「该账户名下的订单」，于是以账户安全为由拒绝回答，
    反过来要客户证明 J26-00001 是自己的。对 builder 客户这是常态——一个客户同时
    有几十个 job，每问一个新工程都会撞上。
    """

    def test_recalled_sections_carry_a_usage_boundary(self):
        text = _ctx(relevant_history=["客户咨询过 J26-00136 的生产进度"]).to_prompt_text()
        assert "跨会话召回" in text
        assert "不属于当前这轮对话" in text
        # 最要紧的一条：不能拿它拒绝客户此刻提的工程号
        assert "不得" in text and "拒绝" in text

    def test_profile_is_labelled_too(self):
        text = _ctx(user_profile={"recent_job": "J26-00136"}).to_prompt_text()
        assert "跨会话累积" in text
        assert "不属于当前这轮对话" in text

    def test_no_caution_when_nothing_was_recalled(self):
        """本轮对话自己的内容不该被这段警告污染——它只管跨会话那两节。"""
        text = _ctx(
            summary="客户询问交期",
            recent_messages=[Message(role=MsgRole.USER, content="我的柜子好了吗")],
        ).to_prompt_text()
        assert "跨会话" not in text
        assert "会话摘要" in text and "最近对话" in text

    def test_current_session_content_is_not_labelled_as_recall(self):
        """会话摘要和最近对话属于当前这轮，不能被归到「跨会话」里去。"""
        text = _ctx(
            summary="客户询问交期",
            relevant_history=["很久以前问过 J25-00001"],
            recent_messages=[Message(role=MsgRole.USER, content="J26-00001 呢")],
        ).to_prompt_text()
        caution_at = text.index("跨会话召回的")
        assert text.index("[会话摘要]") < caution_at, "会话摘要必须在警告之前，不受它管辖"
        assert caution_at < text.index("[相关历史（跨会话召回）]")
