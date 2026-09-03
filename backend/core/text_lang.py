"""
回复语言判定。

独立成一个模块，而不是塞进 answer_policy 或 agent_orchestrator：
  · 编排器用它决定脚手架消息的语言；
  · 策略守卫用它决定安全话术的语种。
两处必须是同一份判定。这正是切句正则踩过的坑——enforce 和 PolicyStreamGuard
各写一份 `[。！？!?\\n]`，改一边忘另一边，两条链路的口径就悄悄分叉了。

零依赖：答案策略是纯规则、零 LLM 的同步代码，不能因为要判个语言就把
llm_utils（连带 anthropic SDK）拉进来。
"""
import re

ZH = "zh"
EN = "en"

_CJK_RE   = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 汉字信息密度大约是拉丁字母的 3 倍：20 字的中文句子 ≈ 60 字符的英文。
# 用这个比例判主体语言，而不是"有没有汉字"——
#   "Can you send a replacement for the W3012 橱柜?" 只夹了两个汉字，
#   按"有没有"判会判成中文，然后给英文客户回中文。
_ZH_DENSITY = 3


def detect_language(text: str) -> str:
    """
    判定一段文本按中文还是英文处理。

    只区分这两种：系统的 SOP、知识库和安全话术就这两套，再细分没有对应的
    下游能力，只会多出一条没人处理的分支。判不出来（空串、纯数字、纯符号）
    时返回 EN——英文脚手架对中文模型的干扰，小于中文脚手架对英文回复的干扰。
    """
    if not text:
        return EN
    cjk   = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    return ZH if cjk * _ZH_DENSITY >= latin and cjk > 0 else EN
