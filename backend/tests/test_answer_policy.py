"""答案策略守卫：拦截无依据的价格承诺和时效承诺。"""
import pytest

from app.core.answer_policy import AnswerPolicy

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
    from app.core.answer_policy import PolicyStreamGuard

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
    from app.core.answer_policy import split_sentences

    text = "It will take 3.5 business days. Cost is approx. $12,800.00 total. Thanks."
    sentences, tail = split_sentences(text, final=True)

    assert "".join(sentences) + tail == text          # 拼回去必须等于原文
    assert sentences[0] == "It will take 3.5 business days."
    assert "approx. $12,800.00 total." in sentences[1]


def test_stream_and_batch_agree_on_english(policy):
    """流式逐句、非流式整段，两条路对同一段英文必须给出同一个结果。"""
    from app.core.answer_policy import PolicyStreamGuard

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
    from app.core.answer_policy import PolicyStreamGuard

    # 刻意选一句不构成承诺的（毫米不是时效），这里测的是缓冲边界不是拦截
    guard = PolicyStreamGuard(AnswerPolicy(enabled=True), sources=[])
    assert guard.feed("The gap measures 3.") == ""
    # 句末的空格留在缓冲区当下一句的句首空白——和非流式那条路同一个口径
    assert guard.feed("5 mm across. ") == "The gap measures 3.5 mm across."
    assert guard.flush() == " "


# ── 承诺表述覆盖率语料 ────────────────────────────────────────────────────────
#
# 这两套正则本质是枚举，枚举一定有洞。中文那套是长期真实流量磨出来的，英文
# 这套是一次性列的——所以把语料固定成用例：以后加规则不许把已覆盖的弄丢，
# 发现新漏法就往语料里加一条。语料里的句子都是这个业务真会说出口的话。

_UNGROUNDED_COMMITMENTS = [
    # 价格
    "The replacement door will be $340.",
    "That upgrade adds $1,200 to your total.",
    "Shipping comes to $85.",
    "The labor charge is 600 dollars.",
    "The total comes out to $12,800 including freight.",
    "这套柜子的价格是 12800 元。",
    # 时效：带数字
    "Your cabinets will ship in 10 business days.",
    "The refund posts within 5 days.",
    "Expect the credit on your card in 3-5 business days.",
    "退款会在 3 个工作日内到账。",
    # 时效：拼出来的数字
    "The remake will be done in two weeks.",
    "两周内给你发货。",
    # 时效：耗时陈述（主语是流程，不是"我们"）
    "Production takes 6 weeks.",
    # 时效：具名日期 / 绝对时点
    "We'll have the replacement to you by Friday.",
    "Installation is scheduled for March 14.",
    "We can install it this Thursday.",
    "Your order completes next week.",
    "I'll get the invoice issued today.",
    "We'll deliver same day.",
    "我们下周二发货。",
    "下周把发票开好。",
    "月底前给你装好。",
]

_ACCEPTABLE = [
    # 有对冲，符合 SOP 的保守表述
    "Turnaround is typically 4-6 weeks, subject to factory scheduling.",
    "Roughly 3 weeks, but that's an estimate pending scheduling.",
    "通常两到三周，具体以排产为准。",
    "返工大概要三天。",
    # 根本不是承诺
    "The showroom is open Monday through Friday.",
    "展厅周二到周四都开门。",
    "I can't quote a price without the formal quotation.",
    "Pricing depends on the slab, edge profile and cutouts.",
    "Once QC confirms responsibility, we'll schedule the remake.",
    "How much would you like to budget for the countertop?",
]


@pytest.mark.parametrize("sentence", _UNGROUNDED_COMMITMENTS)
def test_ungrounded_commitments_are_blocked(policy, sentence):
    assert policy.enforce(sentence, sources=[]).blocked, f"漏拦: {sentence}"


@pytest.mark.parametrize("sentence", _ACCEPTABLE)
def test_acceptable_sentences_are_not_blocked(policy, sentence):
    assert not policy.enforce(sentence, sources=[]).blocked, f"误拦: {sentence}"


def test_both_languages_are_covered_by_the_corpus():
    """
    语料本身要中英都有，否则某一边的规则退化时测试全绿。
    英文那套是一次性列的，最容易悄悄退化的就是它。
    """
    import re

    for name, corpus in (("承诺", _UNGROUNDED_COMMITMENTS), ("放行", _ACCEPTABLE)):
        zh = [s for s in corpus if re.search(r"[一-鿿]", s)]
        en = [s for s in corpus if not re.search(r"[一-鿿]", s)]
        assert len(zh) >= 3, f"{name}语料缺中文样本"
        assert len(en) >= 5, f"{name}语料缺英文样本"


# ── 改了写法的日期 ────────────────────────────────────────────────────────────
#
# 用真实模型探测时发现：业务系统查回来写 2026-10-28，模型转述成
# 「2026 年 10 月 28 日」，守卫却只截到「10 月」去出处里按字面找，有出处的出厂
# 日期被整句换成兜底话术，客户看不到出厂日期。规则预取和工具循环两条路径都这样。

ORDER_DATA = "SO-2410-0815 柜体已完成，目前在做门板喷涂；计划 2026-10-28 出厂，具体送货日期出厂后排车再确认。"
REWRITTEN = "系统计划于 2026 年 10 月 28 日出厂；具体送货日期需出厂后安排车辆，再与您确认。"


def test_rewritten_factory_date_is_grounded(policy):
    result = policy.enforce(REWRITTEN, sources=[ORDER_DATA])

    assert not result.blocked, result.violations
    assert result.text == REWRITTEN


def test_rewritten_factory_date_is_grounded_when_streamed():
    from app.core.answer_policy import PolicyStreamGuard

    guard = PolicyStreamGuard(AnswerPolicy(), sources=[ORDER_DATA])
    chunks = [REWRITTEN[i:i + 4] for i in range(0, len(REWRITTEN), 4)]
    streamed = "".join(guard.feed(c) for c in chunks) + guard.flush()

    assert streamed == REWRITTEN
    assert guard.violations == []


@pytest.mark.parametrize("said", ["2026 年 10 月 28 日", "10 月 28 日", "10月28号"])
@pytest.mark.parametrize("source", [
    "计划 2026-10-28 发货",
    "计划 2026/10/28 发货",
    "计划 10-28 发货",
    "ship_date: 2026-10-28 14:00:00",   # 去掉空白后时间直接粘在日期后面
    "预计 2026 年 10 月 28 日发货",
    "预计 10月28号 发货",
])
def test_same_day_in_another_spelling_is_grounded(policy, said, source):
    answer = f"您的柜体 {said}发货。"

    assert policy.enforce(answer, sources=[]).blocked, "没有出处时这句本来就该拦"
    assert not policy.enforce(answer, sources=[source]).blocked


def test_a_date_missing_from_the_sources_is_still_blocked(policy):
    """证据要是整个日期，不能再截成「11 月」。"""
    result = policy.enforce("您的柜体 11 月 3 日送到。", sources=[ORDER_DATA])

    assert result.blocked
    assert result.violations[0].evidence == "11 月 3 日"


def test_every_date_in_a_sentence_is_checked(policy):
    """有出处的出厂日期，不能替同一句里编出来的送货日期打掩护。"""
    result = policy.enforce("计划 10 月 28 日出厂，11 月 3 日送到。", sources=[ORDER_DATA])

    assert result.blocked
    assert result.violations[0].evidence == "11 月 3 日"


def test_a_grounded_date_does_not_shield_the_rest_of_the_sentence(policy):
    """时效只核第一处，但日期不占这个名额：日期后面的送货时长照样要有出处。"""
    result = policy.enforce("计划 10 月 28 日出厂，3-5 个工作日送到。", sources=[ORDER_DATA])

    assert result.blocked
    assert result.violations[0].evidence == "3-5 个工作日"


def test_years_must_agree_when_both_sides_give_one(policy):
    result = policy.enforce("2027 年 10 月 28 日发货。", sources=[ORDER_DATA])
    assert result.blocked
    assert result.violations[0].evidence == "2027 年 10 月 28 日"

    # 只有一边写了年份时不比年份
    assert not policy.enforce("2026 年 10 月 28 日发货。", sources=["计划 10-28 发货"]).blocked


@pytest.mark.parametrize("answer,source", [
    # 区间不是日期
    ("10 月 28 日发货。", "交期 10-28 天"),
    ("3 月 5 日送到。", "配送 3-5 个工作日"),
    ("4 月 6 日发货。", "标准交期 4-6 周"),
    # 英寸分数不是日期
    ("3 月 4 日送到。", "门板厚 3/4 英寸"),
    # 编号和占位值不是日期
    ("10 月 28 日发货。", "柜号 A10-28 已排产"),
    ("12 月 31 日发货。", "有效期至 9999-12-31"),
    # 月和日出自出处里的不同地方，拼不出一个有出处的日期（张冠李戴）
    ("10 月 28 日发货。", "10 月份开始排产，每月 28 号统一发货。"),
    # 同月不同日。以前只截到「9 月」按字面比，出处里有「9 月」就放行了
    ("9 月 20 日送到。", "订单 SO-2024-8891 已排产，预计 9 月 15 日出厂。"),
])
def test_things_that_only_look_like_the_date_do_not_ground_it(policy, answer, source):
    assert policy.enforce(answer, sources=[source]).blocked


@pytest.mark.parametrize("answer", ["10 个月内完成。", "3-5 个工作日送到。"])
def test_a_date_in_the_sources_does_not_ground_a_duration(policy, answer):
    """时长的判法不变：出处里的 2026-10-28 不能给「10 个月」「3-5 个工作日」当出处。"""
    assert policy.enforce(answer, sources=[ORDER_DATA]).blocked


def test_english_dates_follow_the_same_rule(policy):
    """英文回复会把 2026-10-28 说成 October 28，是同一个毛病，按同一个口径判。"""
    assert not policy.enforce("Your cabinets will ship on October 28.", sources=[ORDER_DATA]).blocked
    assert not policy.enforce("Your cabinets will ship on Oct. 28, 2026.", sources=[ORDER_DATA]).blocked
    assert policy.enforce("Your cabinets will ship on October 28, 2027.", sources=[ORDER_DATA]).blocked
    assert policy.enforce("Your cabinets will ship on November 3.", sources=[ORDER_DATA]).blocked
