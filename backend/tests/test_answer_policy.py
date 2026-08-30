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
    ("明天给你送到。",
     "We will deliver it tomorrow."),
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


# ── 回答里以前认不出来的日期写法 ──────────────────────────────────────────────
#
# 上一组修的是「认出来了，但核错了」。还有几种写法回答里根本认不出是日期：
# 「2026-11-03 送到」「October 28th」「28 号送到」编出来也原样放行；「十月二十八日」
# 只截到「十月」，不带数字，出处里明明有也照拦。

# (回答, 有这一天的出处, 没有这一天的出处, 被拦时的证据)
_NEW_SPELLINGS = [
    # ISO 写法
    ("您的柜体 2026-11-03 送到。", "计划 2026-11-03 送货", ORDER_DATA, "2026-11-03"),
    ("您的柜体 2026/11/03 发货。", "预计 11 月 3 日发货", ORDER_DATA, "2026/11/03"),
    # 英文序数
    ("Your cabinets will ship on October 28th.", ORDER_DATA, "计划 2026-11-03 送货", "October 28th"),
    ("Your cabinets will ship on Oct 3rd.", "计划 2026-10-03 出厂", ORDER_DATA, "Oct 3rd"),
    ("Your cabinets will ship on Oct. 3rd.", "计划 2026-10-03 出厂", ORDER_DATA, "Oct. 3rd"),
    # 只写了日
    ("我们 28 号给您送到。", ORDER_DATA, "计划 2026-11-03 送货", "28 号"),
    ("我们 28 号前给您送到。", ORDER_DATA, "计划 2026-11-03 送货", "28 号"),
    ("3 日发货。", "计划 2026-11-03 发货", ORDER_DATA, "3 日"),
    ("二十八号送到。", ORDER_DATA, "计划 2026-11-03 送货", "二十八号"),
    # 中文数字
    ("您的柜体十月二十八日发货。", ORDER_DATA, "计划 2026-11-03 送货", "十月二十八日"),
    ("二〇二六年十一月三日送到。", "计划 2026-11-03 送货", ORDER_DATA, "二〇二六年十一月三日"),
]


@pytest.mark.parametrize("answer,grounding,other,evidence", _NEW_SPELLINGS)
def test_new_spelling_is_grounded_by_the_same_day(policy, answer, grounding, other, evidence):
    assert not policy.enforce(answer, sources=[grounding]).blocked


@pytest.mark.parametrize("answer,grounding,other,evidence", _NEW_SPELLINGS)
def test_new_spelling_is_blocked_without_that_day(policy, answer, grounding, other, evidence):
    for sources in ([], [other]):
        result = policy.enforce(answer, sources=sources)
        assert result.blocked, sources
        assert result.violations[0].evidence == evidence


@pytest.mark.parametrize("answer,grounding,other,evidence", _NEW_SPELLINGS)
def test_stream_and_batch_agree_on_the_new_spellings(answer, grounding, other, evidence):
    """流式按 3 个字一片喂进去：「Oct.」和「3rd」分在两片里，也不能被切成两句。"""
    from app.core.answer_policy import PolicyStreamGuard

    for sources in ([grounding], [other]):
        batch = AnswerPolicy().enforce(answer, sources=sources).text
        guard = PolicyStreamGuard(AnswerPolicy(), sources=sources)
        streamed = "".join(guard.feed(answer[i:i + 3]) for i in range(0, len(answer), 3)) + guard.flush()
        assert streamed == batch


@pytest.mark.parametrize("answer", [
    "2027-10-28 发货。",
    "Your cabinets will ship on October 28th, 2027.",
    "二〇二七年十月二十八日发货。",
])
def test_years_must_agree_in_the_new_spellings_too(policy, answer):
    assert policy.enforce(answer, sources=[ORDER_DATA]).blocked


def test_a_day_only_date_grounded_by_a_recurring_day(policy):
    """出处只写了日（每月 28 号）时，能给只写了日的回答当出处，给写了月份的不行。"""
    source = "每月 28 号统一发货。"
    assert not policy.enforce("28 号统一发货。", sources=[source]).blocked
    assert policy.enforce("10 月 28 日发货。", sources=[source]).blocked


@pytest.mark.parametrize("answer", [
    # 订单号
    "订单 SO-2410-0815 已发货。",
    "订单 SO-2024-8891 已发货。",
    "订单 SO-2026-11-03 已发货。",
    # 英寸分数
    "3/4 英寸的侧板已经补发。",
    # 编号
    "3 号柜已经发货。",
    "3 号上柜、5 号下柜已经发货。",
    "3 号前门板会补发。",
    "第 3 号已经发货。",
    "第十一号柜已经发货。",
    "加州 65 号提案要求的标签已经贴好。",
    # 英文：marble 2 以前被「前三个字母 + 任意字母」当成了 March 2
    "The marble 2 cm top will be installed.",
    "We will deliver the 3rd drawer box.",
])
def test_things_that_only_look_like_the_new_spellings_are_not_dates(policy, answer):
    """这些都不是日期。认成日期的话，没有出处时一律判无依据，正常的回复就被换掉了。"""
    assert not policy.enforce(answer, sources=[]).blocked


@pytest.mark.parametrize("answer,source", [
    # 区间
    ("我们 3 号给您送到。", "配送 3-5 个工作日"),
    ("我们 5 号给您送到。", "配送 3-5 个工作日"),
    ("6 号发货。", "标准交期 4-6 周"),
    # 英寸分数
    ("4 号送到。", "门板厚 3/4 英寸"),
    # 订单号、编号，以及更长的数字的尾巴
    ("15 号发货。", "订单 SO-2410-0815 已排产"),
    ("28 号送到。", "第 28 号柜已排产"),
    ("5 号送到。", "外包装贴有加州 65 号提案要求的警示标签"),
    # 价格
    ("28 号送到。", "运费 28 元"),
    ("Your cabinets will ship on Oct 28th.", "The refund of $10.28 has been issued."),
    # 时长
    ("7 号送到。", "退款 7 日内到账"),
])
def test_look_alikes_in_the_sources_do_not_ground_the_new_spellings(policy, answer, source):
    assert policy.enforce(answer, sources=[source]).blocked


def test_ranges_and_prices_are_still_checked_their_own_way(policy):
    """区间、金额照旧按字面核，不会被新认的日期写法截走。"""
    assert not policy.enforce("配送 3-5 个工作日送到。", sources=["配送 3-5 个工作日"]).blocked
    assert not policy.enforce("尾款 2026.11 元已到账。", sources=["尾款 2026.11 元"]).blocked
    result = policy.enforce("尾款 2026.11 元已到账。", sources=[])
    assert [v.rule for v in result.violations] == ["price_commitment"]


def test_month_abbreviations_do_not_end_a_sentence():
    """「Oct.」后面切开的话，日期落进没有承诺动词的后半句，编出来的日期两半都判不出来。"""
    from app.core.answer_policy import split_sentences

    sentences, tail = split_sentences("We will ship on Oct. 28. Thanks.", final=True)
    assert sentences == ["We will ship on Oct. 28.", " Thanks."]


# ── 守卫还看不见、或者看错了的几种时效写法 ────────────────────────────────────
#
# 上一组之后还剩四处：中文的「明天送到」根本不当时效（英文 tomorrow 早就核了）；「7 日内
# 到账」「3 日后发货」不核，时长那一支的单位里没有「日」；美式 10/28/2026 两边都不认，
# 业务系统要是按这个格式返回，模型转述成「10 月 28 日」反而被拦；日在前的英文日期
# （28 October、the 28th）回答里认不出来。

@pytest.mark.parametrize("answer,evidence", [
    ("明天送到。", "明天"),
    ("后天发货。", "后天"),
    ("今天给您装好。", "今天"),
    ("大后天上门安装。", "大后天"),
    ("明后天发货。", "明后天"),
    ("今晚发出。", "今晚"),
    ("明早送货。", "明早"),
])
def test_relative_days_are_never_grounded(policy, answer, evidence):
    """
    和 马上/当天、英文 today/tomorrow 一个口径：不带数字，恒判无出处。出处里写着同一个词
    不算；写着明天那一天的日期也不算——守卫没有时钟，不知道「明天」是哪一天。
    """
    for sources in ([], [answer], ["计划 2026-10-17 送货"]):
        result = policy.enforce(answer, sources=sources)
        assert result.blocked, sources
        assert result.violations[0].evidence == evidence


@pytest.mark.parametrize("answer", [
    "预计明天发货，具体以物流通知为准。",
    "明天是周六，展厅照常开门。",
    # 「后」是前面「装好后」「之后」的，「天…」是下一个词
    "吊柜装好后天花板的缝我们会上门封好。",
    "台面送到之后天然石要先养护。",
    "安装完成后天地柜的门板会补发。",
])
def test_relative_day_look_alikes_and_hedged_sentences_pass(policy, answer):
    assert not policy.enforce(answer, sources=[]).blocked


# (回答, 有出处时的出处, 被拦时的证据)
_DAY_DURATIONS = [
    ("退款 7 日内到账。", "审核通过后 7 日内退款到账", "7 日内"),
    ("退款 7 日以内到账。", "审核通过后 7 日内退款到账", "7 日以内"),   # 以内、之内、内是同一处
    ("付款 3 日后发货。", "付款 3 日后安排发货", "3 日后"),
    ("现货 3-5 日内发货。", "现货 3~5 日内发货", "3-5 日内"),
]


@pytest.mark.parametrize("answer,grounding,evidence", _DAY_DURATIONS)
def test_a_day_duration_is_checked_literally(policy, answer, grounding, evidence):
    """「7 日内」「3 日后」以前不核（时长的单位里没有「日」），现在和「7 天内」一样按字面核。"""
    assert not policy.enforce(answer, sources=[grounding]).blocked

    result = policy.enforce(answer, sources=[])
    assert result.blocked
    assert result.violations[0].evidence == evidence


@pytest.mark.parametrize("source", [
    # 7 日内不是 7 号：出处里 7 号的日期对不上它
    "计划 2026-10-07 出厂",
    "每月 7 日统一结算",
    # 单位不一样，或者说的是另一件事
    "补件确认后 7 个工作日内从工厂发出",
    "签收 7 日后不再受理退货",
    # 按字面核：「天」写的不是「日」的出处，反过来也一样
    "7 天内退款到账",
])
def test_look_alikes_do_not_ground_a_day_duration(policy, source):
    assert policy.enforce("退款 7 日内到账。", sources=[source]).blocked


def test_a_grounded_date_does_not_shield_a_day_duration(policy):
    """「7 日内」不是日期，占的是时效核查的那个名额，前面有出处的出厂日期替它打不了掩护。"""
    result = policy.enforce("计划 10 月 28 日出厂，7 日内送到。", sources=[ORDER_DATA])

    assert result.blocked
    assert result.violations[0].evidence == "7 日内"


# (回答, 有这一天的出处, 没有这一天的出处, 被拦时的证据)
_MORE_SPELLINGS = [
    # 美式，月在前
    ("Your cabinets will ship on 10/28/2026.", ORDER_DATA, "计划 2026-11-03 送货", "10/28/2026"),
    ("您的柜体 11/03/2026 送到。", "计划 2026-11-03 送货", ORDER_DATA, "11/03/2026"),
    ("Your cabinets will ship on 10-28-2026.", ORDER_DATA, "计划 2026-11-03 送货", "10-28-2026"),
    # 日在前
    ("Your cabinets will ship on 28 October.", ORDER_DATA, "计划 2026-11-03 送货", "28 October"),
    ("Your cabinets will ship on 3rd November 2026.", "计划 2026-11-03 送货", ORDER_DATA, "3rd November 2026"),
    ("Your cabinets will ship on the 28th of October.", ORDER_DATA, "计划 2026-11-03 送货", "28th of October"),
    ("Your cabinets will ship on 28 Oct. 2026.", ORDER_DATA, "计划 2026-11-03 送货", "28 Oct. 2026"),
    ("Your cabinets will ship on 3 May.", "计划 2026-05-03 出厂", ORDER_DATA, "3 May"),
    # 只写了日
    ("Your cabinets will ship on the 28th.", ORDER_DATA, "计划 2026-11-03 送货", "the 28th"),
    ("We will install them on the 3rd at 9 AM.", "计划 2026-11-03 安装", ORDER_DATA, "the 3rd"),
]


@pytest.mark.parametrize("answer,grounding,other,evidence", _MORE_SPELLINGS)
def test_us_and_day_first_dates_are_grounded_by_the_same_day(policy, answer, grounding, other, evidence):
    assert not policy.enforce(answer, sources=[grounding]).blocked


@pytest.mark.parametrize("answer,grounding,other,evidence", _MORE_SPELLINGS)
def test_us_and_day_first_dates_are_blocked_without_that_day(policy, answer, grounding, other, evidence):
    for sources in ([], [other]):
        result = policy.enforce(answer, sources=sources)
        assert result.blocked, sources
        assert result.violations[0].evidence == evidence


@pytest.mark.parametrize("answer,grounding,other,evidence", _MORE_SPELLINGS)
def test_stream_and_batch_agree_on_us_and_day_first_dates(answer, grounding, other, evidence):
    """流式按 3 个字一片喂进去：「Oct.」「28th.」分在两片里，也得和整段判得一样。"""
    from app.core.answer_policy import PolicyStreamGuard

    for sources in ([grounding], [other]):
        batch = AnswerPolicy().enforce(answer, sources=sources).text
        guard = PolicyStreamGuard(AnswerPolicy(), sources=sources)
        streamed = "".join(guard.feed(answer[i:i + 3]) for i in range(0, len(answer), 3)) + guard.flush()
        assert streamed == batch


@pytest.mark.parametrize("answer", [
    "Your cabinets will ship on 10/28/2027.",
    "Your cabinets will ship on 28 October 2027.",
])
def test_years_must_agree_in_us_and_day_first_dates(policy, answer):
    assert policy.enforce(answer, sources=[ORDER_DATA]).blocked


@pytest.mark.parametrize("source", [
    "ship_date: 10/28/2026",
    "ship_date: 10/28/2026 2:00 PM",   # 去掉空白后钟点直接粘在年份后面
    "ship_date: 10-28-2026",
    "Ships 28 October 2026 from the CA factory.",
    "Ships 28 Oct, 2026.",
])
def test_us_and_day_first_dates_in_the_sources_ground_a_rewrite(policy, source):
    """业务系统按美式写法返回，模型转述成「10 月 28 日」，是同一天换了写法。"""
    answer = "您的柜体 10 月 28 日发货。"

    assert policy.enforce(answer, sources=[]).blocked
    assert not policy.enforce(answer, sources=[source]).blocked


def test_an_english_day_only_source_grounds_only_a_day_only_answer(policy):
    source = "The install window opens on the 28th."

    assert not policy.enforce("28 号送到。", sources=[source]).blocked
    assert policy.enforce("10 月 28 日送到。", sources=[source]).blocked


@pytest.mark.parametrize("answer,source", [
    # 不带年份的 N/N 不认：这个业务里 3/4、1/2 是英寸分数
    ("您的柜体 10 月 28 日发货。", "计划 10/28 发货"),
    ("您的柜体 1 月 2 日发货。", "1/2 英寸背板已补发"),
    # 去掉空白后，月份缩写后面粘着的是别的词
    ("Your cabinets will ship on March 5.", "We have 5 marble tops in stock."),
    ("Your cabinets will ship on December 3.", "All 3 deck boards are cut."),
    ("Your cabinets will ship on September 2.", "Your 2 separate boxes have shipped."),
    # 情态动词 may
    ("Your cabinets will ship on May 3.", "Items 2 and 3 may ship separately."),
])
def test_look_alikes_in_the_sources_do_not_ground_us_or_day_first_dates(policy, answer, source):
    assert policy.enforce(answer, sources=[source]).blocked


@pytest.mark.parametrize("answer", [
    "Items 2 and 3 may ship separately.",
    "The 5 marble tops will be installed.",
    "We will ship all 3 decks together.",
    # 序数，不是日期
    "We will deliver to the 28th Street store.",
    "We will deliver to 28th Street.",
    "We will install it on the 2nd floor.",
    "We will deliver to the 28th-floor office.",
    "We will replace the 1st, 2nd and 3rd doors.",
    "We will replace the 2nd/3rd shelf.",
    # 不带年份的 N/N
    "The 3/4 inch panel will ship separately.",
])
def test_things_that_only_look_like_us_or_day_first_dates_are_not_dates(policy, answer):
    """认成日期的话，没有出处时一律判无依据，正常的回复就被换掉了。"""
    assert not policy.enforce(answer, sources=[]).blocked


# ── 按字面核的数字要整个对上 ──────────────────────────────────────────────────
#
# 时长、金额按字面去出处里找，以前不看数字边界：出处「17 天」能给「7 天」当出处，
# 「1280 元」能给「280 元」、「$2,800」能给「$280」、「17 日内」能给「7 日内」当出处。

# (回答, 出处里同一个数, 出处里更长的数——回答里的数只是它的一截, 被拦时的证据)
_WHOLE_NUMBERS = [
    ("补件 7 天内送到。", "补件确认后 7 天内送到", "补件确认后 17 天内送到", "7 天"),
    ("退款 7 日内到账。", "审核通过后 7 日以内退款到账", "审核通过后 17 日内退款到账", "7 日内"),
    ("加一个抽屉只要 280 元。", "加一个抽屉 280 元", "加一个抽屉 1280 元", "280 元"),
    ("加一个抽屉只要 280 元。", "加一个抽屉 280 元", "加一个抽屉 1,280 元", "280 元"),
    ("The upgrade will cost $280.", "Upgrade: $280.00 per door.", "Upgrade: $2,800 per run.", "$280"),
    ("Production takes 6 weeks.", "Production takes 6 weeks.", "Production takes 16 weeks.", "6 weeks"),
    # 小数也是更长的数，只是小数部分全是 0 的不算
    ("The upgrade will cost $280.", "Upgrade: $280.00 per door.", "Upgrade: $280.50 per door.", "$280"),
    ("The upgrade will cost $280.5.", "Upgrade: $280.50 per door.", "Upgrade: $280.55 per door.", "$280.5"),
    ("加一个抽屉只要 80 元。", "加一个抽屉 80 元", "加一个抽屉 12.80 元", "80 元"),
    ("上门安装 5 小时完成。", "上门安装 5 小时完成", "上门安装 1.5 小时完成", "5 小时"),
    # 回答里的小数整个截进证据，不再只截「5 小时」
    ("上门安装 1.5 小时完成。", "上门安装 1.5 小时完成", "上门安装 11.5 小时完成", "1.5 小时"),
]


@pytest.mark.parametrize("answer,same,longer,evidence", _WHOLE_NUMBERS)
def test_the_same_number_grounds_a_literal_check(policy, answer, same, longer, evidence):
    assert not policy.enforce(answer, sources=[same]).blocked


@pytest.mark.parametrize("answer,same,longer,evidence", _WHOLE_NUMBERS)
def test_part_of_a_longer_number_does_not_ground_it(policy, answer, same, longer, evidence):
    result = policy.enforce(answer, sources=[longer])

    assert result.blocked
    assert result.violations[0].evidence == evidence


def test_range_and_measure_word_spellings_still_ground(policy):
    """数字边界不动原来那几处写法差异：区间连接符、量词「个」。"""
    assert not policy.enforce("配送 3-5 个工作日送到。", sources=["配送 3~5 个工作日"]).blocked
    assert not policy.enforce("配送 3-5 个工作日送到。", sources=["配送 3至5工作日"]).blocked


@pytest.mark.parametrize("answer,source", [
    # 出处去掉空白和逗号以后，挨着的数会粘上来：「¥500，3 个工作日」→「¥5003个工作日」
    ("加急费 ¥500，3 个工作日内发出。", "加急费 ¥500，3 个工作日内从工厂发出。"),
    ("补件 3 个工作日内发出。", "补件流程：\n1. 3 个工作日内从工厂发出。"),
    ("补件 7 天内送到。", "订单 SO-2410-0815 7 天内送到"),
    ("Delivery takes 3-5 business days.", "The total is $980. 3-5 business days for delivery."),
    ("The upgrade will cost $280.", "Upgrade: $280, 3 doors minimum."),
])
def test_a_neighbouring_number_in_the_sources_is_not_part_of_it(policy, answer, source):
    assert not policy.enforce(answer, sources=[source]).blocked


def test_a_year_after_a_comma_still_belongs_to_its_date(policy):
    """日期照原来的去法比：出处「October 28, 2026」的年份不会因为数之间的分隔和日子断开。"""
    source = "Ships October 28, 2026 from the CA factory."

    assert not policy.enforce("Your cabinets will ship on October 28, 2026.", sources=[source]).blocked
    assert policy.enforce("Your cabinets will ship on October 28, 2027.", sources=[source]).blocked
