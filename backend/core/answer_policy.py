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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# ── 检测规则 ──────────────────────────────────────────────────────────────────

# 价格表述：带货币符号的金额，或带货币单位的数字
_PRICE_RE = re.compile(
    r"[¥￥$]\s*\d[\d,]*(?:\.\d{1,2})?"
    r"|\d[\d,]*(?:\.\d{1,2})?\s*(?:元|块钱|块|美元|美金|usd|cny|rmb|dollars?)",
    re.I,
)

# 时效表述：带时间单位的数字区间，或"马上/立刻"这类绝对时效词
_LEAD_TIME_RE = re.compile(
    r"\d+\s*(?:[-~至到]\s*\d+\s*)?(?:个)?\s*(?:工作日|自然日|天|小时|分钟|周|个月|月)"
    r"|\d+\s*(?:business\s+days?|working\s+days?|days?|hours?|minutes?|weeks?)"
    r"|马上|立刻|立即|当天|次日|隔天|今天内|immediately|right\s+away|same\s+day",
    re.I,
)

# 承诺动词：出现金额/时效还不够，必须落在"会发生什么"的语境里才算承诺
_COMMIT_RE = re.compile(
    r"到账|退回|退给|退款|返还|返给|发货|送达|签收|处理完|处理好|解决|完成"
    r"|开好|开具|生效|恢复|收到|补偿|赔付|减免"
    # 橱柜业务的交付承诺：工厂排产、送货到场、补件返工、上门服务这几条线上，
    # "什么时候能做好/送到/装好"和"多少钱"一样是说错就要赔的话术。
    r"|做好|做完|完工|出货|发出|补发|送到|送货|到货|到场|上门|安排好"
    r"|排产|开料|复尺|出图|装好|安装完|返工好|修好"
    r"|费用|价格|收费|手续费|服务费|运费|邮费|差价|需支付|支付|付款"
    r"|只需|只要|仅需|报价|加价|金额为|金额是|一共|总共",
)

# 绝对化措辞：出现这些词时，即使句子里有对冲词也仍按承诺处理
_ABSOLUTE_RE = re.compile(
    r"一定|肯定|必定|保证|承诺|绝对|无条件|包退|包赔|必然|100%|guarantee|guaranteed|definitely",
    re.I,
)

# 对冲措辞：SOP 要求的保守表达，命中即认为 Agent 已经守住了边界
_HEDGE_RE = re.compile(
    r"通常|一般|预计|大约|大概|可能|不确定|视.{0,8}而定|以.{0,14}为准|具体以"
    r"|需(?:要)?核(?:验|实)|审核后|核实后|请以|仅供参考"
    r"|typically|usually|approximately|estimated|subject\s+to",
    re.I,
)

# 句子切分：保留原有标点，替换后仍能拼回自然的段落
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?\n])")

_SAFE_REPLACEMENT = {
    "price_commitment": (
        "具体金额需要以正式报价单和合同条款为准，我不能在未核验前给出确定金额。"
    ),
    "lead_time_commitment": (
        "具体时间需要以工厂排产和物流排班的实际情况为准，我不能在未确认前承诺具体日期。"
    ),
}

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

    def enforce(self, answer: str, sources: Iterable[str] | None = None) -> PolicyResult:
        """扫描 answer，拦截其中无依据的价格/时效承诺。"""
        if not self.enabled or not answer:
            return PolicyResult(text=answer or "", checked=False)

        self.checked_total += 1
        normalized_sources = [self._normalize(s) for s in (sources or []) if s]

        violations: list[PolicyViolation] = []
        rebuilt: list[str] = []

        for chunk in _SENT_SPLIT_RE.split(answer):
            sentence = chunk.strip()
            if not sentence:
                rebuilt.append(chunk)
                continue

            hit = self._check_sentence(sentence, normalized_sources)
            if hit is None:
                rebuilt.append(chunk)
                continue

            violations.append(hit)
            # 保留原句的尾部空白/换行，避免替换后段落粘连
            tail = chunk[len(chunk.rstrip()):]
            rebuilt.append(_SAFE_REPLACEMENT[hit.rule] + tail)

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
        }

    # ── 内部实现 ──────────────────────────────────────────────────────────────

    def _check_sentence(
        self,
        sentence: str,
        normalized_sources: list[str],
    ) -> PolicyViolation | None:
        """判断单句是否构成"无依据承诺"。四条同时成立才拦截。"""
        price = _PRICE_RE.search(sentence)
        lead_time = _LEAD_TIME_RE.search(sentence)
        if not price and not lead_time:
            return None

        # 2. 必须落在承诺语境里
        if not _COMMIT_RE.search(sentence):
            return None

        # 3. 已经按 SOP 做了保守表述，且没有绝对化措辞 → 放行
        if _HEDGE_RE.search(sentence) and not _ABSOLUTE_RE.search(sentence):
            return None

        # 4. 数字必须能在 grounding sources 中找到出处
        for rule, match in (("price_commitment", price), ("lead_time_commitment", lead_time)):
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

        绝对时效词（马上、立刻）永远没有出处——它们本身就是禁止表述。
        带数字的表述要求"数字和单位在出处里连在一起"出现，只允许量词"个"
        和区间连接符的写法差异。

        这里刻意不做"数字在出处里出现过就算有依据"的宽松匹配：知识库写
        "配送 3-5 个工作日"时，模型说"退款 3 个工作日到账"必须判为无依据——
        数字被跨条目张冠李戴，正是这类承诺出事的主要方式。
        """
        if not normalized_sources:
            return False

        norm = self._normalize(evidence)
        if not re.search(r"\d", norm):
            return False  # 马上/立刻/immediately 这类绝对时效，无论如何都没有出处

        # 量词"个"可有可无，区间连接符（- ~ 至 到）任意写法都算同一处出处。
        # 这里必须一次性替换：逐个 replace 会把前一轮生成的字符类再改坏。
        pattern = re.escape(norm).replace("个", "个?")
        pattern = re.sub(r"\\?[-~]|至|到", "[-~至到]", pattern)
        try:
            regex = re.compile(pattern)
        except re.error:
            return any(norm in source for source in normalized_sources)

        return any(regex.search(source) for source in normalized_sources)

    @staticmethod
    def _normalize(text: Any) -> str:
        """统一大小写、去掉空白和千位分隔符，便于做出处比对。"""
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
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

        emitted = ""
        # 只处理已经出现句末标点的完整句，剩下的半句继续留在缓冲区
        while True:
            match = re.search(r"[。！？!?\n]", self._buffer)
            if not match:
                break
            sentence, self._buffer = self._buffer[:match.end()], self._buffer[match.end():]
            emitted += self._check(sentence)
        return emitted

    def flush(self) -> str:
        """流结束时把缓冲区里剩下的半句过一遍策略再发出。"""
        if not self._buffer:
            return ""
        tail, self._buffer = self._buffer, ""
        return self._check(tail)

    def _check(self, sentence: str) -> str:
        result = self._policy.enforce(sentence, self._sources)
        self.violations.extend(result.violations)
        return result.text
