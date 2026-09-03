"""答案策略守卫：拦截无依据的价格承诺和时效承诺。"""
import pytest

from core.answer_policy import AnswerPolicy

KB = (
    "[知识库检索结果]\n"
    "1. 标题: 交期说明\n   内容: 标准交期 4-6 周，以工厂排产确认为准。\n"
    "2. 标题: 补件流程\n   内容: 补件确认后 7 个工作日内从工厂发出。\n"
    "3. 标题: 五金升级\n   内容: 阻尼铰链升级单价 199 元。\n"
)


@pytest.fixture
def policy() -> AnswerPolicy:
    return AnswerPolicy()


# ── 应当拦截 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,rule", [
    ("补件会在 3 个工作日内送到。", "lead_time_commitment"),
    ("我保证 24 小时内一定安排好。", "lead_time_commitment"),
    ("这单的运费是 25 元，马上就能退给你。", "price_commitment"),
    ("加一个抽屉只要 88 元。", "price_commitment"),
])
def test_blocks_unsourced_commitments(policy, answer, rule):
    result = policy.enforce(answer, sources=[KB])

    assert result.blocked
    assert result.violations[0].rule == rule
    assert answer not in result.text  # 原句必须被替换掉


def test_blocks_when_no_sources_at_all(policy):
    """没有任何检索结果时，一切具体数字都是无依据的。"""
    result = policy.enforce("补件会在 3 个工作日内送到。", sources=[])
    assert result.blocked


def test_number_borrowed_from_a_different_topic_is_still_unsourced(policy):
    """
    知识库写的是"补件 7 个工作日发出"，模型说成"4 个工作日就能送到"，
    数字被跨条目张冠李戴——这正是这类承诺出事的主要方式，必须拦下。
    """
    result = policy.enforce("补件 4 个工作日就能送到。", sources=[KB])
    assert result.blocked


def test_absolute_wording_overrides_hedging(policy):
    """加了"通常"但同时说"保证"，仍然是承诺。"""
    result = policy.enforce("通常我保证 2 小时内一定安排好。", sources=[KB])
    assert result.blocked


# ── 应当放行 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    "补件确认后 7 个工作日内从工厂发出。",
    "标准交期 4-6 周。",
    "标准交期 4~6 周。",
    "阻尼铰链升级单价是 199 元。",
])
def test_allows_commitments_backed_by_sources(policy, answer):
    result = policy.enforce(answer, sources=[KB])
    assert not result.blocked
    assert result.text == answer


@pytest.mark.parametrize("answer", [
    "补件通常在一周左右发出，具体以工厂排产实际情况为准。",
    "预计 3 个工作日左右，具体需要核实订单状态后确认。",
])
def test_allows_hedged_statements(policy, answer):
    """SOP 要求的保守表述本来就不是承诺。"""
    result = policy.enforce(answer, sources=[KB])
    assert not result.blocked


@pytest.mark.parametrize("answer", [
    "您的问题我们会尽快处理，请先提供订单号。",
    "请提供订单号和支付时间，我帮您核实。",
    "订单号 12345 已收到，我来帮您查询。",
])
def test_does_not_flag_ordinary_replies(policy, answer):
    result = policy.enforce(answer, sources=[KB])
    assert not result.blocked


# ── 替换行为 ──────────────────────────────────────────────────────────────────

def test_only_the_offending_sentence_is_replaced(policy):
    answer = "好的，我来帮您处理。退款会在 3 个工作日内到账。请保留支付凭证。"

    result = policy.enforce(answer, sources=[KB])

    assert "好的，我来帮您处理。" in result.text
    assert "请保留支付凭证。" in result.text
    assert "3 个工作日内到账" not in result.text


def test_disabled_policy_passes_through():
    policy = AnswerPolicy(enabled=False)
    answer = "退款会在 3 个工作日内到账。"

    result = policy.enforce(answer, sources=[])

    assert result.text == answer
    assert not result.blocked
    assert result.checked is False


def test_stats_track_block_rate(policy):
    policy.enforce("退款会在 3 个工作日内到账。", sources=[])
    policy.enforce("请提供订单号。", sources=[])

    stats = policy.stats()
    assert stats["checked_total"] == 2
    assert stats["blocked_total"] == 1
    assert stats["block_rate"] == 0.5
    assert stats["blocked_by_rule"]["lead_time_commitment"] == 1


# ── 上游网关乱码 ──────────────────────────────────────────────────────────────

def test_drops_replacement_chars_from_answer(policy):
    """U+FFFD 是上游网关重编码留下的，本进程恢复不了，只能不发给客户。"""
    result = policy.enforce("好的，我帮你查一下这个���订单的进度。", sources=[])

    assert "�" not in result.text
    assert result.text == "好的，我帮你查一下这个订单的进度。"


def test_mojibake_is_dropped_even_when_policy_disabled():
    """数据卫生不受策略开关管：关掉策略也不该把乱码发出去。"""
    policy = AnswerPolicy(enabled=False)

    result = policy.enforce("已受理���。", sources=[])

    assert result.text == "已受理。"
    assert result.checked is False


def test_mojibake_counted_for_monitor(policy):
    policy.enforce("第一句有三个���。", sources=[])
    policy.enforce("第二句只有一个�。", sources=[])
    policy.enforce("这句是干净的。", sources=[])

    stats = policy.stats()
    # 只统计出现过乱码的那几次调用，干净的那次不计入
    assert stats["mojibake_events"] == 2
    assert stats["mojibake_dropped_chars"] == 4


def test_clean_answer_is_untouched(policy):
    answer = "请提供订单号，我这边帮你查。"
    assert policy.enforce(answer, sources=[]).text == answer
    assert policy.stats()["mojibake_events"] == 0


def test_stream_guard_also_drops_mojibake(policy):
    """流式和非流式共用 enforce，所以流式链路必须同样干净。"""
    from core.answer_policy import PolicyStreamGuard

    guard = PolicyStreamGuard(policy, sources=[])
    out = guard.feed("好的���，") + guard.feed("我这就帮你查。") + guard.flush()

    assert "�" not in out
    assert out == "好的，我这就帮你查。"


# ── 双语守卫 ──────────────────────────────────────────────────────────────────
#
# 这一组全部是"回复语言跟客户走"之后才成立的用例。在此之前英文回复里的
# 价格/时效承诺一条也拦不下来：_COMMIT_RE 没有任何英文词，四条件里"承诺动词"
# 这一条永远凑不齐；而 _SENT_SPLIT_RE 不认 '.'，整段英文还会被当成一个句子。

@pytest.mark.parametrize("zh,en", [
    ("退款会在 3 个工作日内到账。",
     "The refund will arrive within 3 business days."),
    ("这套柜子的价格是 12800 元。",
     "The total for this cabinet run is $12,800."),
    ("我们下周二发货。",
     "We will ship it next Tuesday."),
    ("月底前给你装好。",
     "We will have it installed by the end of the month."),
])
def test_same_commitment_is_blocked_in_both_languages(policy, zh, en):
    assert policy.enforce(zh, sources=[]).blocked, f"中文漏拦: {zh}"
    assert policy.enforce(en, sources=[]).blocked, f"英文漏拦: {en}"


@pytest.mark.parametrize("zh,en", [
    ("通常 3-5 个工作日，具体以排产为准。",
     "Typically 3-5 business days, subject to factory scheduling."),
    ("展厅周二到周四都开门。",
     "The showroom is open Tuesday through Thursday."),
])
def test_hedged_or_non_commitment_passes_in_both_languages(policy, zh, en):
    assert not policy.enforce(zh, sources=[]).blocked, f"中文误拦: {zh}"
    assert not policy.enforce(en, sources=[]).blocked, f"英文误拦: {en}"


def test_english_reply_gets_english_replacement(policy):
    """
    安全话术要跟回复语言走。只有中文版时，拦下一句英文承诺会往英文回复里
    插一段中文——客户读到的是语言突然跳变的一段话，比不拦更糟。
    """
    result = policy.enforce("We will ship it next Tuesday.", sources=[])

    assert result.blocked
    assert "can't commit to a date" in result.text
    assert "具体时间" not in result.text


def test_replacement_keeps_the_space_between_sentences(policy):
    """
    句首空白必须保留。之前只保留句尾空白——中文看不出来（句子之间没有空格），
    英文会把替换句和前一句黏成 "...verified.The invoice..."。
    """
    text = "Thanks for waiting. The total is $980. Let me know."
    out = policy.enforce(text, sources=[]).text

    assert "verified. Let me know." in out
    assert ".The" not in out and ".Let" not in out


def test_english_paragraph_is_checked_sentence_by_sentence(policy):
    """
    '.' 不算句末时整段英文只有一个句子，一处违规就会把整段换成安全话术，
    干净的句子跟着一起被吞掉。
    """
    text = "Thanks for waiting. The total is $980. Happy to help further."
    out = policy.enforce(text, sources=[]).text

    assert out.startswith("Thanks for waiting.")
    assert out.endswith("Happy to help further.")
    assert "$980" not in out


def test_decimals_and_abbreviations_do_not_split_sentences():
    """3.5 / $12,800.00 / approx. 里的句号不是句末——切错会把承诺动词和金额拆散。"""
    from core.answer_policy import split_sentences

    text = "It will take 3.5 business days. Cost is approx. $12,800.00 total. Thanks."
    sentences, tail = split_sentences(text, final=True)

    assert "".join(sentences) + tail == text          # 拼回去必须等于原文
    assert sentences[0] == "It will take 3.5 business days."
    assert "approx. $12,800.00 total." in sentences[1]


def test_stream_and_batch_agree_on_english(policy):
    """流式逐句、非流式整段，两条路对同一段英文必须给出同一个结果。"""
    from core.answer_policy import PolicyStreamGuard

    text = "Thanks for waiting. The total is $980. We will ship it next Tuesday."
    batch = policy.enforce(text, sources=[]).text

    # 按固定长度切片喂进去，原文一个字符都不丢——网络分片本来就不按词边界来
    guard = PolicyStreamGuard(AnswerPolicy(enabled=True), sources=[])
    chunks = [text[i:i + 7] for i in range(0, len(text), 7)]
    streamed = "".join(guard.feed(c) for c in chunks) + guard.flush()

    assert streamed == batch
    assert len(guard.violations) == 2


def test_a_trailing_period_is_not_flushed_early():
    """
    流式里结尾的 '.' 后面还没有空白，可能是没写完的小数（"3." → "3.5"）。
    必须留在缓冲区等下一片，不能当句末抢先发出去。
    """
    from core.answer_policy import PolicyStreamGuard

    # 刻意选一句不构成承诺的（毫米不是时效），这里测的是缓冲边界不是拦截
    guard = PolicyStreamGuard(AnswerPolicy(enabled=True), sources=[])
    assert guard.feed("The gap measures 3.") == ""
    # 句末的空格留在缓冲区当下一句的句首空白——和非流式那条路同一个口径
    assert guard.feed("5 mm across. ") == "The gap measures 3.5 mm across."
    assert guard.flush() == " "
