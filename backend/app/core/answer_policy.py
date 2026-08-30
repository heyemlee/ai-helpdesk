"""
亮点：回答策略守卫（Answer Policy Guard）

核心问题：Skills/SOP 只是写在 prompt 里的"软约束"，模型完全可能违反。
最典型的违规是**无依据的价格承诺和时效承诺**——这两类话术一旦说错，
就是真实的客诉和赔付风险，光靠 prompt 约束在生产上不可接受。

本模块把 SOP 里的禁止事项变成**可执行的硬约束**：

  1. 检测：逐句扫描 Agent 回复，找出价格表述和时效表述。
  2. 判承诺：只有同时出现「金额/时效」+「承诺动词」才算承诺；
     已经做了保守表述（通常/预计/以…为准）的句子不算承诺。
  3. 判依据：承诺里的数字必须能在本轮 grounding sources
     （RAG 检索结果 + 注入的 Skill SOP 正文）中找到，否则判定为"无依据"。
  4. 拦截：把无依据的承诺句替换为保守表述，并记录结构化违规项。

设计取舍：
  - 纯规则实现，零 LLM 调用——策略守卫在每次回复后同步执行，不能引入延迟。
  - 宁可漏判不可错判：只有"数字 + 承诺动词 + 无对冲词 + 无出处"四条同时成立才拦截。
"""
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.core.text_lang import detect_language

# ── 检测规则 ──────────────────────────────────────────────────────────────────

# 价格表述：带货币符号的金额，或带货币单位的数字
_PRICE_RE = re.compile(
    r"[¥￥$]\s*\d[\d,]*(?:\.\d{1,2})?"
    r"|\d[\d,]*(?:\.\d{1,2})?\s*(?:元|块钱|块|美元|美金|usd|cny|rmb|dollars?)",
    re.I,
)

# 日期里的数字。中文数字写的（二〇二六年十月二十八日）和阿拉伯数字一样认：
# 只认阿拉伯数字时，「十月二十八日」只截到「十月」，不带数字，_is_grounded
# 恒判无出处，出处里明明有的日期照样被换掉。
_CN_NUMERALS = "〇零一二三四五六七八九十"
_CN_YEAR = "[〇零一二三四五六七八九]{4}"
_CN_MONTH = "十[一二]?|[一二三四五六七八九]"
_CN_DAY = "三十一?|二十[一二三四五六七八九]?|十[一二三四五六七八九]?|[一二三四五六七八九]"
# 只写日的时候，日子要在 1–31 之内：「65 号提案」不是日期
_DAY_DIGITS = r"[12]\d|3[01]|0?[1-9]"
_DAY_NUM = rf"{_DAY_DIGITS}|{_CN_DAY}"
# 英文月份只认完整的月份名和标准缩写。之前写的是「前三个字母 + 任意字母」，
# 「The marble 2 cm top will be installed.」里的「marble 2」也被当成了日期。
_MONTH_NAME = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
# 日在前（28 May）时，小写的 may 多半是情态动词：「Items 2 and 3 may ship separately.」
_MONTH_NAME_DAY_FIRST = _MONTH_NAME.replace("|may|", "|(?-i:May)|")

# 只写了日的日期（28 号送到、3 日发货）后面能接什么。「号」「日」前面只有一个数字
# 时很多不是日期：「3 号柜」「5 号上柜」是编号，「7 日内」「3 日后」是时长。所以
# 回答里只认后面接着标点、时间词或动作的，接别的（多半是名词）一律不算——认漏了，
# 这句和以前一样不核；认错了，一句正常的回复会被换成兜底话术。
_DAY_FOLLOWER = (
    r"[^\w\s、]|$"     # 标点或句末。顿号不算：「3 号、5 号柜」是在列编号
    r"|当[天日]|那天|[上下中]午|早上|晚上|凌晨|傍晚|左右|开始|截止|至|周[一二三四五六日天]|星期|礼拜"
    r"|就|才|再|会(?!议)|能|可以?|将|要|需|必须|务必|一定|保证|准时|如期|统一|给|由"
    r"|送[到达货过]|发[货出]|出[厂货]|安[装排]|装[好车]|完[工成]|交[货付]|做[好完]|排[产车]|复尺"
    r"|补[发件货]|签收|退[款货]|收[货到]|付款|处理|修[好复]|返工|寄出|取货|提货|派送|配送|到[货场账达]"
    r"|运到|抵达|进场|动工|施工|验收|确认|通知|联系|回复|上门|下单|开[工料具]|生效|恢复|解决|测量|量尺"
)
# 「号」后面还可以先接前、后、起（28 号前送到、28 号以后、28 号起）。「日」不行：
# 「3 日前」「3 日后」「3 日起」更常见的意思是三天前、三天后、三天起。
_HAO_FOLLOWER = rf"[之以][前后]|[前后起]?(?:{_DAY_FOLLOWER})"
# 英文只写日的（the 28th）同理：「the 3rd drawer box」「the 2nd floor」「the 28th Street
# store」里是序数，不是日期。只认后面接着标点、钟点、时段或「this month」这类的；逗号后面
# 又是一个序数的（the 1st, 2nd and 3rd boxes）是在列东西，连字符、斜杠后面紧跟着词或数字的
# （the 3rd-floor units、the 2nd/3rd shelf）也是。
_EN_DAY_FOLLOWER = (
    r"[^\w\s,/\-]|$|-(?![a-z\d])|,(?!\s*(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b)"
    r"|at\s+(?:\d|noon|the\s+latest)|between\s+\d|in\s+the\s+(?:morning|afternoon|evening)"
    r"|(?:morning|afternoon|evening|night)\b|(?:of\s+)?(?:this|next|the|each|every)\s+month\b"
)

# 时效表述：带时间单位的数字区间，或"马上/立刻"这类绝对时效词
_LEAD_TIME_RE = re.compile(
    # 具体日期必须排在最前面：「10 月 28 日」开头的「10 月」也符合后面的
    # 「数字 + 月」那一支（时长），分支按顺序试，排在后面就只截到「10 月」。
    # 拿半个日期去出处里找，出处写的是 2026-10-28，有出处的出厂日期就被当成
    # 无依据承诺换掉了。年份一并匹配进来，_is_grounded 要拿它和出处的年份比。
    rf"(?:(?:\d{{4}}|{_CN_YEAR})\s*年\s*)?(?:\d{{1,2}}|{_CN_MONTH})\s*月\s*(?:\d{{1,2}}|{_CN_DAY})\s*[号日]"
    # 2026-11-03 / 2026/11/03 / 2026.11.03。前面粘着字母、数字或连接符的是编号
    # （SO-2026-11-03），年份只认 20xx，月日不在范围里的不算（不认的就不该进来，
    # 进来了会被当成普通时效按字面核，反而误拦）。
    r"|(?<![a-z\d/.\-])20\d\d(?P<iso_sep>[-/.])(?:1[0-2]|0?[1-9])(?P=iso_sep)(?:[12]\d|3[01]|0?[1-9])"
    r"(?!\d|[-/.]\d)"
    # 10/28/2026 / 10-28-2026：美式，月在前。年份必须写全——只写 10/28 的不认，
    # 这个业务里 3/4、1/2 是英寸分数。
    rf"|(?<![a-z\d/.\-])(?:1[0-2]|0?[1-9])(?P<us_sep>[-/])(?:{_DAY_DIGITS})(?P=us_sep)20\d\d"
    r"(?!\d|[-/.]\d)"
    # 只写了日的：28 号 / 3 日 / 二十八号。紧跟在区间连接符、小数点、分数线后面的
    # （3-5 日）不算，「第 3 号」不算，后面接什么见 _DAY_FOLLOWER。
    rf"|(?<![\d月第./\-~{_CN_NUMERALS}])(?<!第\s)(?:{_DAY_NUM})\s*"
    rf"(?:号(?=\s*(?:{_HAO_FOLLOWER}))|日(?=\s*(?:{_DAY_FOLLOWER})))"
    # 时长的数可以带小数（1.5 天、2.5 小时），要整个截进来：只截「5 天」的话，出处原样写着
    # 「1.5 天」也对不上——_is_grounded 不认更长的数的一截，小数也算
    r"|(?:\d+(?:\.\d+)?|[一二两三四五六七八九十几])\s*(?:[-~至到]\s*\d+(?:\.\d+)?\s*)?(?:个)?"
    r"\s*(?:工作日|自然日|天|小时|分钟|周|个月|月"
    # 「日」后面接着 内/后/前/起 时是时长（7 日内到账、3 日后发货），和「天」一样按字面核；
    # 接别的是上面那一支只写了日的日期（3 日发货），或者根本不是时效。后缀一并截进证据：
    # 只截「7 日」的话，_date_of 会把它当成 7 号去比。
    r"|日\s*(?:[以之]?[内后前]|起))"
    # 数字也可能是拼出来的："in two weeks" / "两周内"——只认 \d+ 会整条漏掉
    r"|(?:\d+(?:\.\d+)?|a\s+(?:few|couple\s+of)|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s*(?:[-~]\s*\d+(?:\.\d+)?\s*)?"
    r"(?:business\s+days?|working\s+days?|business\s+hours?|days?|hours?|minutes?"
    r"|weeks?|months?|years?)"
    r"|马上|立刻|立即|当天|次日|隔天|今天内"
    # 今天/明天/后天和英文的 today/tomorrow 一个口径：不带数字，_is_grounded 恒判无出处。
    # 守卫没有时钟，出处写着明天的日期（2026-10-17）也对不上「明天」。「装好后天花板……」
    # 「之后天然石……」里的「后天」是「后」+「天…」，后面接着这几个字的不算。
    r"|今天|今日|今晚|明天|明日|明早|明晚|[明大]?后天(?![花然窗地台线井气])"
    # 中文具名日期。补英文那一类时才发现中文侧同样漏着：「我们下周二发货」
    # 不含数字，_LEAD_TIME_RE 之前一条都匹配不上，和英文的 next Tuesday 一样
    # 是无出处的具体交付承诺。不补的话英文反而比中文严。
    r"|(?:下|本|这)?(?:周|星期|礼拜)[一二三四五六日天]"
    r"|(?:下|本|这|下个)(?:周|星期|礼拜|月)"
    r"|(?:这|下|上)?(?:周末|月底|月初|年底)"
    r"|immediately|right\s+away|same\s+day|next\s+day|by\s+(?:today|tomorrow)"
    # 具名日期也是绝对时效：中文的 当天/次日 对应英文的 next Tuesday / by Friday /
    # end of the week。这些不带数字，_is_grounded 因此恒判无出处——和中文侧
    # 处理 马上/立刻 的口径一致：具体交付日期不该在没有出处时被承诺。
    r"|(?:next|this|by|on|before)\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day\s+(?:morning|afternoon|at\b)"
    r"|(?:by|before)\s+(?:the\s+)?end\s+of\s+(?:the\s+)?(?:day|week|month)"
    r"|(?:next|this)\s+(?:week|month|quarter)"
    r"|\b(?:today|tonight|tomorrow)\b"
    # 带上可选的年份（October 28, 2026），和中文那一支同理；日子可以写成序数（October 28th）
    rf"|\b(?:{_MONTH_NAME})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s*\d{{4}}\b)?"
    # 日在前：28 October / 28th of October / 28 Oct. 2026。前面粘着小数点、连接符的
    # 是金额或编号的尾巴（$10.28）。月份后面的点只在后面还有年份时截进来，不然是句号。
    rf"|(?<![\d.,/\-$])\b(?:{_DAY_DIGITS})(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTH_NAME_DAY_FIRST})\b"
    r"(?:\.?,?\s*\d{4}\b)?"
    # 只写了日：the 28th，后面接什么见 _EN_DAY_FOLLOWER
    rf"|\bthe\s+(?:{_DAY_DIGITS})(?:st|nd|rd|th)\b(?=\s*(?:{_EN_DAY_FOLLOWER}))",
    re.I,
)

# 数字区间后面跟着这些字时，是时长、数量或尺寸，不是日期（见 _DATE_FORMS 里的 10-28）
_RANGE_UNIT_CHARS = (
    "个天日号周月年时小分秒工自星礼晚次件套张块片扇条根台组项步层级位人份箱包只把"
    "米厘毫寸尺英公斤磅克千吨度升元角美万百倍折成岁期"
)

# 日期的写法。_is_grounded 判具体日期有没有出处时按「哪一天」比，不按字面比：
# 业务系统查回来写 2026-10-28，模型转述成「2026 年 10 月 28 日」「10 月 28 号」
# 「October 28」，是同一天换了写法，不是编造。回答和出处用同一份清单解析，
# 两边认的写法因此一样。跑在 _normalize 之后的文本上（小写，去掉空白和逗号）。
_DATE_FORMS = (
    # 2026-10-28 / 2026/10/28 / 2026.10.28。去掉空白后，后面跟着的时间会直接
    # 粘上来（2026-10-28 14:00 → 2026-10-2814:00），所以「日」后面紧跟
    # 「两位小时:」也算日期结束。
    re.compile(
        r"(?<![\d.])(?P<y>\d{4})(?P<sep>[-/.])(?P<m>\d{1,2})(?P=sep)(?P<d>\d{1,2})"
        r"(?:(?!\d)|(?=(?:[01]\d|2[0-3]):))"
    ),
    # 10/28/2026 / 10-28-2026（美式，月在前）。年份必须写全，不带年份的 10/28 不认（英寸
    # 分数）。时间同样会粘上来，美式还常写一位数的钟点：10/28/2026 2:00 PM → 10/28/20262:00pm。
    re.compile(
        r"(?<![\d./\-])(?P<m>\d{1,2})(?P<sep>[/-])(?P<d>\d{1,2})(?P=sep)(?P<y>\d{4})"
        r"(?:(?!\d)|(?=(?:[01]?\d|2[0-3]):[0-5]\d))"
    ),
    # 2026年10月28日 / 10月28号 / 二〇二六年十月二十八日
    re.compile(
        rf"(?:(?<![\d{_CN_NUMERALS}])(?P<y>\d{{4}}|{_CN_YEAR})年)?"
        rf"(?<![\d{_CN_NUMERALS}])(?P<m>\d{{1,2}}|{_CN_MONTH})月(?P<d>\d{{1,2}}|{_CN_DAY})[日号]"
    ),
    # 10-28（不带年份）。区间长得和它一样——「10-28 天」「3-5 个工作日」「4-6 周」，
    # 后面跟单位的不算；前后粘着字母、数字或连接符的是编号（SO-2410-0815、A10-28）。
    # 不认 10/28：这个业务里 3/4、1/2 是英寸分数。
    re.compile(
        r"(?<![\da-z.:/\-第])(?P<m>\d{1,2})-(?P<d>\d{1,2})"
        rf"(?![\da-z%.:/\-~{_RANGE_UNIT_CHARS}])"
    ),
    # oct28 / october28th2026（原文 Oct 28 / October 28th, 2026）。去空白后单词都粘在
    # 一起，没法要求月份前面是词边界，所以只认完整的月份名和标准缩写。
    re.compile(rf"(?P<mon>{_MONTH_NAME})\.?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?P<y>\d{{4}})?(?!\d)"),
    # 28october / 28thofoctober / 28oct.2026（原文 28 October / 28th of October / 28 Oct. 2026）。
    # 月份后面的词也粘了上来，所以缩写和 may 后面不能再是字母：「5 marble tops」「3 decks」
    # 「2 separate boxes」「3 may ship」都不是日期。完整的月份名不会是别的词的开头，不用管。
    re.compile(
        r"(?<![\d./\-:])(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:of)?"
        r"(?P<mon>january|february|march|april|june|july|august|september|october|november|december"
        r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)(?![a-z]))"
        r"\.?(?P<y>\d{4})?(?:(?!\d)|(?=(?:[01]?\d|2[0-3]):[0-5]\d))"
    ),
    # the28th（原文 the 28th，只写了日）。回答里认哪些由 _LEAD_TIME_RE 把关，出处这边和
    # 下面的「28 号」一样放得宽。
    re.compile(rf"the(?P<d>{_DAY_DIGITS})(?:st|nd|rd|th)"),
    # 28号 / 3日 / 二十八号（不带月份）。回答里认哪些由 _LEAD_TIME_RE 把关（见
    # _DAY_FOLLOWER）；这里同时要解析出处，只排掉「N 日内 / 日后 / 日前 / 日起」这类
    # 时长写法，其余都认——出处里多认出一个日期，最坏是多放行一句，不会误拦。
    re.compile(rf"(?<![\d月第./\-~{_CN_NUMERALS}])(?P<d>{_DAY_NUM})(?:号|日(?![以之]?[内后前]|起))"),
)
_MONTHS = {
    name: i for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}

_CN_DIGIT = {c: i for i, c in enumerate("〇一二三四五六七八九")} | {"零": 0}

_Ymd = tuple[int | None, int | None, int]   # (年，没写就是 None；月，只写了日就是 None；日)


def _num(text: str) -> int:
    """日期里的一个数：28，或者中文数字写的 二〇二六、十、十二、二十八、三十一。"""
    if text.isdecimal():
        return int(text)
    if "十" not in text:   # 年份逐位写：二〇二六
        return int("".join(str(_CN_DIGIT[c]) for c in text))
    tens, _, ones = text.partition("十")
    return _CN_DIGIT.get(tens, 1) * 10 + _CN_DIGIT.get(ones, 0)


def _as_date(m: re.Match[str]) -> _Ymd | None:
    """把 _DATE_FORMS 的一次匹配换成 (年, 月, 日)；不像日期的丢掉。"""
    g = m.groupdict()
    if g.get("mon"):
        month = _MONTHS[g["mon"][:3]]
    else:
        month = _num(g["m"]) if g.get("m") else None
    day = _num(g["d"])
    year = _num(g["y"]) if g.get("y") else None
    if not 1 <= day <= 31 or (month is not None and not 1 <= month <= 12):
        return None
    # 年份只认 2000–2099：9999-12-31 这类「永不过期」的占位值不是日期
    if year is not None and not 2000 <= year < 2100:
        return None
    return year, month, day


def _date_of(norm: str) -> _Ymd | None:
    """norm（已 _normalize）整段就是一个日期时返回它，否则 None。"""
    for form in _DATE_FORMS:
        m = form.fullmatch(norm)
        if m:
            return _as_date(m)
    return None


def _dates_in(norm: str) -> Iterator[_Ymd]:
    """norm（已 _normalize）里认得出的所有日期。"""
    for form in _DATE_FORMS:
        for m in form.finditer(norm):
            date = _as_date(m)
            if date is not None:
                yield date


# 出处去掉空白和逗号以后，挨着的两个数会粘成一个更长的数：「¥500，3 个工作日内发出」去成
# 「¥5003个工作日内发出」，「¥500」「3 个工作日」都成了「5003」的一截，按字面找出处时被
# 数字边界挡掉。所以两个数之间原来有的分隔留成这个记号；千位分隔符（12,800）照旧去掉。
# 记号不能是空白字符（\x1f 在 Python 里也算空白），不然接下来去空白时会被一起去掉。
_NUMBER_GAP = "\x00"
_NUMBER_GAP_RE = re.compile(r"(?<=[\d.])[\s,，]+(?=\d)")
_THOUSANDS_SEP_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


# 承诺动词：出现金额/时效还不够，必须落在"会发生什么"的语境里才算承诺
#
# 英文那一半是后补的：在此之前 _COMMIT_RE 一个英文词都没有，而拦截要求
# 「数字 + 承诺动词 + 无对冲 + 无出处」四条同时成立——缺了承诺动词这一条，
# 英文回复里的价格/时效承诺**一条也拦不下来**（实测 "I guarantee the
# replacement door will be delivered next Tuesday." 原样放行）。
# _PRICE_RE / _LEAD_TIME_RE / _HEDGE_RE 早就各自补过英文，唯独最关键的这条没有。
_COMMIT_RE = re.compile(
    r"到账|退回|退给|退款|返还|返给|发货|送达|签收|处理完|处理好|解决|完成"
    r"|开好|开具|生效|恢复|收到|补偿|赔付|减免"
    # 橱柜业务的交付承诺：工厂排产、送货到场、补件返工、上门服务这几条线上，
    # "什么时候能做好/送到/装好"和"多少钱"一样是说错就要赔的话术。
    r"|做好|做完|完工|出货|发出|补发|送到|送货|到货|到场|上门|安排好"
    r"|排产|开料|复尺|出图|装好|安装完|返工好|修好"
    r"|费用|价格|收费|手续费|服务费|运费|邮费|差价|需支付|支付|付款"
    r"|只需|只要|仅需|报价|加价|金额为|金额是|一共|总共"
    # ── 英文 ──
    # will/we'll + 动词：英文承诺最常见的形状
    r"|(?:will|we'?ll|you'?ll|i'?ll)\s+(?:be\s+)?(?:get\s+|have\s+)?"
    r"(?:refund\w*|ship\w*|deliver\w*|dispatch\w*|send|sent|arrive\w*|issue\w*"
    r"|complete\w*|finish\w*|install\w*|replace\w*|repair\w*|fix\w*|resolve\w*"
    r"|schedul\w*|process\w*|credit\w*|charge\w*|cost|take|receive\w*|be)"
    # 名词形态的费用/交付承诺
    r"|(?:refund|reimburse|credit|invoice|quote|bill|charge)(?:s|d|ed|ing)?"
    r"|(?:ship|deliver|dispatch|install|complete|replace|repair|resolve|schedule)"
    r"(?:s|d|ed|ing|ment|ry)?"
    r"|(?:cost|price|fee|total|surcharge|upcharge|freight|shipping|labor)s?"
    r"|no\s+charge|free\s+of\s+charge|at\s+no\s+cost"
    # "Production takes 6 weeks." —— 耗时陈述同样是时效承诺，
    # 主语是流程而不是"我们"，前面那组 will/we'll 覆盖不到。
    r"|\b(?:takes?|taking)\b"
    r"|\b(?:production|fabrication|remake|turnaround|lead\s*time|rework)\b",
    re.I,
)

# 绝对化措辞：出现这些词时，即使句子里有对冲词也仍按承诺处理
_ABSOLUTE_RE = re.compile(
    r"一定|肯定|必定|保证|承诺|绝对|无条件|包退|包赔|必然|100%"
    r"|guarantee\w*|definitely|certainly|absolutely|for\s+sure|no\s+doubt"
    r"|i\s+promise|we\s+promise|rest\s+assured|without\s+fail",
    re.I,
)

# 对冲措辞：SOP 要求的保守表达，命中即认为 Agent 已经守住了边界
_HEDGE_RE = re.compile(
    r"通常|一般|预计|大约|大概|可能|不确定|视.{0,8}而定|以.{0,14}为准|具体以"
    r"|需(?:要)?核(?:验|实)|审核后|核实后|请以|仅供参考"
    r"|typically|usually|approximately|approx\.?|estimate[ds]?|subject\s+to"
    r"|roughly|around|about|likely|should\s+be|depend[s]?\s+on|depending|varies"
    r"|tentative|preliminary|non-?binding|for\s+reference|pending|to\s+be\s+confirmed"
    r"|once\s+(?:confirmed|verified)|after\s+(?:review|verification|confirmation)"
    r"|ballpark|indicative",
    re.I,
)

# 句子切分：保留原有标点，替换后仍能拼回自然的段落
logger = logging.getLogger(__name__)

# U+FFFD REPLACEMENT CHARACTER，就是客户看到的那个「�」
_REPLACEMENT_CHAR = "\ufffd"

# 句末标点。英文句号必须区分「句末」和「小数点 / 缩写点」，见 _is_sentence_period。
# 流式与非流式共用（split_sentences），不再两边各写一份——之前 enforce 用
# `(?<=[。！？!?\n])`、PolicyStreamGuard.feed 用 `[。！？!?\n]`，两份都漏了 `.`：
# 一整段英文回复因此被当成**一个句子**，逐句守卫退化成整段守卫。
_SENT_END_RE       = re.compile(r"[。！？!?\n]|\.(?=\s)")
_SENT_END_FINAL_RE = re.compile(r"[。！？!?\n]|\.(?=\s|$)")

# 句号前是这些词时不是句末。漏了它们会把 "approx. $12,800" 切成两句，
# 对冲词留在前半句、金额落在后半句，两边都判不出违规。
_ABBREVIATIONS = frozenset({
    "approx", "est", "etc", "e.g", "i.e", "no", "vs", "mr", "ms", "mrs", "dr",
    "inc", "ltd", "co", "dept", "min", "max", "qty", "ref", "fig", "ext", "apt",
    # 月份缩写。漏了它们，"We will ship on Oct. 28." 在 Oct. 处切开，日期落进
    # 没有承诺动词的后半句，编出来的日期两半都判不出来
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
_WORD_BEFORE_DOT_RE = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")

def _is_sentence_period(text: str, idx: int) -> bool:
    """
    text[idx] 是 '.'。判断它是句末还是缩写点。

    小数点不需要单独判：_SENT_END_RE 要求句号后面跟空白，而 "3.5" 和
    "$12,800.00" 的句号后面是数字，本来就匹配不上。反过来，句末金额
    （"The total is $980. We will ship tomorrow."）必须切得开，所以这里
    不能因为句号前是数字就一律否掉。
    """
    head = re.search(_WORD_BEFORE_DOT_RE, text[: idx + 1])
    return not (head and head.group(1).rstrip(".").lower() in _ABBREVIATIONS)


def split_sentences(text: str, final: bool = True) -> tuple[list[str], str]:
    """
    切句，返回（完整句列表，结尾未完成的半句）。原标点与空白全部保留，
    拼回去等于原文。

    final=False 用于流式：此时结尾的 '.' 后面还没有空白，无法区分
    "3." 是句末还是小数点没写完，留在缓冲区等下一片；flush() 时用 final=True。
    """
    pattern = _SENT_END_FINAL_RE if final else _SENT_END_RE
    out: list[str] = []
    start = 0
    for m in pattern.finditer(text):
        if m.group(0) == "." and not _is_sentence_period(text, m.start()):
            continue
        out.append(text[start : m.end()])
        start = m.end()
    return out, text[start:]

# 安全话术按回复语言选。只有中文版时，拦下一句英文承诺会往英文回复里
# 插一段中文——客户读到的是一段语言突然跳变的话，比不拦更糟。
_SAFE_REPLACEMENT = {
    "zh": {
        "price_commitment": (
            "具体金额需要以正式报价单和合同条款为准，我不能在未核验前给出确定金额。"
        ),
        "lead_time_commitment": (
            "具体时间需要以工厂排产和物流排班的实际情况为准，我不能在未确认前承诺具体日期。"
        ),
    },
    "en": {
        "price_commitment": (
            "The exact amount has to come from the formal quotation and the signed "
            "contract, so I can't commit to a figure before it has been verified."
        ),
        "lead_time_commitment": (
            "The exact timing depends on factory scheduling and delivery routing, so "
            "I can't commit to a date before it has been confirmed."
        ),
    },
}


def _safe_replacement(rule: str, sentence: str) -> str:
    """
    按被替换句子的语种取安全话术。判定与编排器的脚手架共用 detect_language，
    免得两处对"这句是中文还是英文"给出不同答案。
    """
    return _SAFE_REPLACEMENT[detect_language(sentence)][rule]

_RULE_LABEL = {
    "price_commitment": "无依据的价格承诺",
    "lead_time_commitment": "无依据的时效承诺",
}


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyViolation:
    """一条被拦截的违规表述。"""
    rule:     str    # price_commitment / lead_time_commitment
    evidence: str    # 触发违规的原始片段，例如 "3个工作日"
    sentence: str    # 违规所在的完整句子
    reason:   str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "label": _RULE_LABEL.get(self.rule, self.rule),
            "evidence": self.evidence,
            "sentence": self.sentence,
            "reason": self.reason,
        }


@dataclass
class PolicyResult:
    """策略执行结果。text 是可以安全返回给用户的最终文本。"""
    text:       str
    violations: list[PolicyViolation] = field(default_factory=list)
    checked:    bool = True

    @property
    def blocked(self) -> bool:
        return bool(self.violations)

    @property
    def blocked_count(self) -> int:
        return len(self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "blocked_count": self.blocked_count,
            "violations": [v.to_dict() for v in self.violations],
        }


# ── 策略守卫 ──────────────────────────────────────────────────────────────────

class AnswerPolicy:
    """
    对 Agent 回复做出站校验。

    用法：
        policy = AnswerPolicy()
        result = policy.enforce(answer, sources=[rag_context, skill_prompt])
        return result.text        # 已经替换掉无依据承诺的安全文本
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        # 运行期统计，供 /monitor 展示策略拦截情况
        self.checked_total = 0
        self.blocked_total = 0
        self.blocked_by_rule: dict[str, int] = {}
        # 上游网关送来的乱码统计（见 _drop_mojibake）
        self.mojibake_dropped_chars = 0
        self.mojibake_events = 0

    def enforce(self, answer: str, sources: Iterable[str] | None = None) -> PolicyResult:
        """扫描 answer，拦截其中无依据的价格/时效承诺。"""
        # 先做数据卫生再谈策略：关掉策略开关也不该把乱码发给客户
        answer = self._drop_mojibake(answer or "")
        if not self.enabled or not answer:
            return PolicyResult(text=answer, checked=False)

        self.checked_total += 1
        normalized_sources = [self._normalize(s, keep_number_gaps=True) for s in (sources or []) if s]

        violations: list[PolicyViolation] = []
        rebuilt: list[str] = []

        sentences, tail_chunk = split_sentences(answer, final=True)
        for chunk in [*sentences, tail_chunk]:
            sentence = chunk.strip()
            if not sentence:
                rebuilt.append(chunk)
                continue

            hit = self._check_sentence(sentence, normalized_sources)
            if hit is None:
                rebuilt.append(chunk)
                continue

            violations.append(hit)
            # 句首和句尾的空白都要保留。只保尾部时，中文看不出来（句子之间没有空格），
            # 英文会把替换句和前一句黏成 "...before it's verified.The invoice..."。
            head = chunk[: len(chunk) - len(chunk.lstrip())]
            tail = chunk[len(chunk.rstrip()):]
            rebuilt.append(head + _safe_replacement(hit.rule, sentence) + tail)

        if violations:
            self.blocked_total += 1
            for v in violations:
                self.blocked_by_rule[v.rule] = self.blocked_by_rule.get(v.rule, 0) + 1

        return PolicyResult(text="".join(rebuilt), violations=violations)

    def stats(self) -> dict[str, Any]:
        """供 Monitor / API 读取的策略拦截统计。"""
        rate = self.blocked_total / self.checked_total if self.checked_total else 0.0
        return {
            "enabled": self.enabled,
            "checked_total": self.checked_total,
            "blocked_total": self.blocked_total,
            "block_rate": round(rate, 4),
            "blocked_by_rule": dict(self.blocked_by_rule),
            "mojibake_dropped_chars": self.mojibake_dropped_chars,
            "mojibake_events": self.mojibake_events,
        }

    # ── 内部实现 ──────────────────────────────────────────────────────────────

    def _drop_mojibake(self, text: str) -> str:
        """
        丢掉 U+FFFD（�）——客户看到的那串「���」。

        为什么只能丢、不能修：本进程内每一层都是字符安全的，已逐一排除——
          · 前端 TextDecoder 带 {stream:true}，跨网络块正确续接；
          · api/main.py 的 _sse() 返回整串 str，Starlette 整块编码，不会切在字符中间；
          · 本类全程 str 拼接与切片，Python 的字符串切片按码点走；
          · anthropic SDK 的 _aiter_chunks 累积到完整 SSE 帧（\n\n）才产出，
            splitlines() 只在 \r/\n 处切（绝不落在多字节序列内部），
            再 raw_line.decode("utf-8") —— strict 模式，真被截断会抛
            UnicodeDecodeError，而不是悄悄产生 �。
        所以字符到达这里时，U+FFFD 已经是上游第三方网关
        （ANTHROPIC_BASE_URL，当前指向 claude-relay）替换好才下发的，
        原始字节在进入本进程之前就没了，任何代码都恢复不出来。

        能做的只有别把它送到客户眼前：少一个字读起来像错别字，
        「���」读起来像系统坏了。计数进 stats() 交给 /monitor，
        让网关这个毛病可被量化，而不是静默发生。
        """
        if _REPLACEMENT_CHAR not in text:
            return text
        dropped = text.count(_REPLACEMENT_CHAR)
        self.mojibake_dropped_chars += dropped
        self.mojibake_events += 1
        logger.warning(
            "出站文本含 %d 个 U+FFFD，已剔除；来源疑为上游网关重编码，"
            "累计 %d 次 / %d 字",
            dropped, self.mojibake_events, self.mojibake_dropped_chars,
        )
        return text.replace(_REPLACEMENT_CHAR, "")

    def _check_sentence(
        self,
        sentence: str,
        normalized_sources: list[str],
    ) -> PolicyViolation | None:
        """判断单句是否构成"无依据承诺"。四条同时成立才拦截。"""
        price = _PRICE_RE.search(sentence)
        lead_times = list(_LEAD_TIME_RE.finditer(sentence))
        if not price and not lead_times:
            return None

        # 2. 必须落在承诺语境里
        if not _COMMIT_RE.search(sentence):
            return None

        # 3. 已经按 SOP 做了保守表述，且没有绝对化措辞 → 放行
        if _HEDGE_RE.search(sentence) and not _ABSOLUTE_RE.search(sentence):
            return None

        # 4. 数字必须能在 grounding sources 中找到出处。
        #    时效表述照旧只核句子里的第一处，但具体日期不占这个名额，有几个核几个。
        #    日期对得上出处之后，要是还只核第一处，「10 月 28 日出厂，11 月 3 日送到」
        #    「10 月 28 日出厂，3-5 个工作日送到」后半句编出来的送货时间，就跟着
        #    有出处的出厂日期一起放行了。
        dates = [m for m in lead_times if _date_of(self._normalize(m.group(0)))]
        others = [m for m in lead_times if m not in dates]
        checks = [("price_commitment", price)] + [
            ("lead_time_commitment", m) for m in sorted(dates + others[:1], key=lambda m: m.start())
        ]
        for rule, match in checks:
            if match is None:
                continue
            evidence = match.group(0).strip()
            if self._is_grounded(evidence, normalized_sources):
                continue
            return PolicyViolation(
                rule=rule,
                evidence=evidence,
                sentence=sentence,
                reason=(
                    f"{_RULE_LABEL[rule]}：'{evidence}' 未出现在本轮知识库检索结果或 Skill SOP 中，"
                    "且句子使用了承诺性表述。"
                ),
            )
        return None

    def _is_grounded(self, evidence: str, normalized_sources: list[str]) -> bool:
        """
        判断一处金额/时效是否有出处。

        绝对时效词（马上、立刻、明天）永远没有出处——它们本身就是禁止表述。
        带数字的表述要求"数字和单位在出处里连在一起"出现，只允许量词"个"、
        区间连接符和「日内 / 日以内」这几处写法差异。数字还得整个对上：出处里
        更长的数（17 天、1280 元、$2,800、1.5 天）的一截不算。

        这里刻意不做"数字在出处里出现过就算有依据"的宽松匹配：知识库写
        "配送 3-5 个工作日"时，模型说"退款 3 个工作日到账"必须判为无依据——
        数字被跨条目张冠李戴，正是这类承诺出事的主要方式。

        具体日期（10 月 28 日、Oct 28）例外，按「哪一天」比而不是按字面比：出处写
        2026-10-28、回答写 2026 年 10 月 28 日，是同一天换了写法。口径并不放宽——
        月和日必须出自出处里的同一个日期（「10 月份……28 号」拼不出 10 月 28 日，
        「10-28 天」是区间不是日期），两边都写了年份时年份也要对上。只写了日的
        （28 号）只比日：出处里有哪个日期是 28 号就算，出处原样写着「28 号」也算。
        """
        if not normalized_sources:
            return False

        norm = self._normalize(evidence)
        date = _date_of(norm)
        if date is None and not re.search(r"\d", norm):
            return False  # 马上/立刻/immediately 这类绝对时效，无论如何都没有出处

        if date is not None:
            # 日期照原来的去法解析，不留数之间的记号：「October 28, 2026」的年份要和日子连上
            sources = [s.replace(_NUMBER_GAP, "") for s in normalized_sources]
            year, month, day = date
            if any(
                d == day and (month is None or m == month) and (year is None or y is None or y == year)
                for source in sources
                for y, m, d in _dates_in(source)
            ):
                return True
            if month is not None:
                return False
            # 只写了日的，出处原样写着也算：「3 日发货」也可能是「三天发货」的意思，
            # 那就和时长一个口径照字面找。但不能是更长的数字的尾巴或「第 N 号」——
            # 「加州 65 号提案」里没有 5 号。
            literal = re.compile(rf"(?<![\d第{_CN_NUMERALS}]){re.escape(norm)}")
            return any(literal.search(source) for source in sources)

        # 量词"个"可有可无，区间连接符（- ~ 至 到）任意写法都算同一处出处。
        # 这里必须一次性替换：逐个 replace 会把前一轮生成的字符类再改坏。
        # 证据和出处一样去：两个数之间原来的分隔留着记号
        norm = self._normalize(evidence, keep_number_gaps=True)
        pattern = re.escape(norm).replace("个", "个?")
        pattern = re.sub(r"\\?[-~]|至|到", "[-~至到]", pattern)
        # 「7 日内」「7 日以内」「7 日之内」是同一处
        pattern = re.sub(r"日[以之]?([内后前])$", r"日[以之]?\1", pattern)
        # 数字两头不能连着出处里更长的数：「17 天」里没有「7 天」，「1280 元」里没有「280 元」，
        # 「$2,800」里没有「$280」。小数也算更长的数：「1.5 天」里没有「5 天」，「$280.50」里
        # 没有「$280」；只是小数部分全是 0 的不算——$280.00 就是 $280，$280.50 就是 $280.5。
        if re.match(r"\d", norm):
            pattern = r"(?<![\d.])" + pattern
        if re.search(r"\d$", norm):
            pattern += r"(?!0*[1-9])" if re.search(r"\.\d+$", norm) else r"(?!\d|\.0*[1-9])"
        try:
            regex = re.compile(pattern)
        except re.error:
            return any(norm in source for source in normalized_sources)

        return any(regex.search(source) for source in normalized_sources)

    @staticmethod
    def _normalize(text: Any, keep_number_gaps: bool = False) -> str:
        """
        统一大小写、去掉空白和千位分隔符，便于做出处比对。

        keep_number_gaps：两个数之间去掉的空白、逗号留成 _NUMBER_GAP，按字面核
        数字边界时用；按日期比之前要把它去掉。
        """
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
        if keep_number_gaps:
            text = _NUMBER_GAP_RE.sub(_NUMBER_GAP, _THOUSANDS_SEP_RE.sub("", text))
        return re.sub(r"[\s,，]", "", text).lower()


class PolicyStreamGuard:
    """
    流式输出下的策略守卫。

    流式的根本矛盾：字已经发给用户了就收不回来，而"这句话是不是无依据承诺"
    必须看完整句才能判断。所以这里按句缓冲——攒到句末标点才过一次策略校验，
    再把（可能已被替换的）整句吐出去。

    代价是首字延迟从"第一个 token"变成"第一句话"，换来的是流式输出和
    非流式输出**受同一套策略约束**，不会出现"流式能说、非流式不能说"的口径不一致。
    """

    def __init__(self, policy: AnswerPolicy, sources: Iterable[str] | None = None):
        self._policy = policy
        self._sources = list(sources or [])
        self._buffer = ""
        self.violations: list[PolicyViolation] = []

    def feed(self, chunk: str) -> str:
        """喂入一段增量文本，返回当前可以安全发出的部分（可能为空）。"""
        if not chunk:
            return ""
        self._buffer += chunk

        # 切句口径和非流式完全一致（同一个 split_sentences），
        # 这样"流式能说、非流式不能说"的分叉在结构上就不可能出现。
        # final=False：结尾的 '.' 可能是还没写完的小数，留到 flush 再判。
        sentences, self._buffer = split_sentences(self._buffer, final=False)
        return "".join(self._check(s) for s in sentences)

    def flush(self) -> str:
        """流结束时把缓冲区里剩下的半句过一遍策略再发出。"""
        if not self._buffer:
            return ""
        tail, self._buffer = self._buffer, ""
        return self._check(tail)

    def add_sources(self, texts: Iterable[str]) -> None:
        """
        回答中途拿到了新的出处（工具循环里查回来的数据）。之后的句子按新出处判；
        已经发出去的句子不重判——它们说出口的时候，这份出处还不存在。
        """
        self._sources.extend(t for t in texts if t)

    def discard_pending(self) -> str:
        """
        丢掉缓冲区里还没凑成整句的部分，返回丢掉的内容（给调用方记日志）。

        工具循环里模型先写了半句再去调工具时用：那半句不会再接着写完，留在缓冲区里
        会和下一步的回答黏成一句。
        """
        pending, self._buffer = self._buffer, ""
        return pending

    def _check(self, sentence: str) -> str:
        result = self._policy.enforce(sentence, self._sources)
        self.violations.extend(result.violations)
        return result.text
