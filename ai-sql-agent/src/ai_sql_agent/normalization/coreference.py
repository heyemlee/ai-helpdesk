from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple


# 指代词 → 是否指向实体集合
_PRONOUNS = ("它们", "他们", "她们", "这些", "那些", "这几个", "那几个", "它", "这个", "那个")

# 从上一轮问题里抽取「被查询对象」的模式。
# 例：查询实际成本超过预算的项目名称 → 实际成本超过预算的项目
_SUBJECT_PATTERNS = (
    r"(?:查询|统计|列出|看看|找出|筛选)?\s*(.{2,40}?的(?:项目|订单|采购单|供应商|物料|材料|工单|客户|批次))",
    r"(?:查询|统计|列出|看看|找出|筛选)\s*(.{2,40}?)(?:的名称|的数量|是多少|有哪些|$)",
)


class CoreferenceResolver:
    """
    代词消解。

    多轮对话里用户会说「它们的采购金额呢」，其中「它们」指上一轮查出的实体。
    不消解就直接送进检索，关键词里只有「采购金额」，会丢掉上一轮的筛选条件。

    实现是规则式的：从最近一轮**查库类**问题里抽取被查询对象短语，替换掉指代词。
    规则覆盖不到时保持原样——宁可不改，也不要改错，改错比不改危害大得多。
    """

    def resolve(
        self,
        query: str,
        history_queries: Sequence[str],
    ) -> Tuple[str, List[str]]:
        """返回 (消解后的问题, 消解记录)。"""
        pronoun = self._find_pronoun(query)
        if pronoun is None or not history_queries:
            return query, []

        antecedent = self._extract_subject(history_queries)
        if not antecedent:
            return query, []

        resolved = query.replace(pronoun, antecedent, 1)
        return resolved, [f"{pronoun} → {antecedent}"]

    @staticmethod
    def _find_pronoun(query: str) -> Optional[str]:
        for pronoun in _PRONOUNS:
            if pronoun in query:
                return pronoun
        return None

    @staticmethod
    def _extract_subject(history_queries: Sequence[str]) -> str:
        """从最近一轮往前找，取第一个能抽出主体的问题。"""
        for previous in reversed(list(history_queries)):
            text = previous.strip()
            if not text:
                continue
            for pattern in _SUBJECT_PATTERNS:
                match = re.search(pattern, text)
                if match:
                    subject = match.group(1).strip()
                    # 去掉尾部的输出字段词，只保留限定短语
                    subject = re.sub(r"(名称|数量|金额|列表)$", "", subject).strip()
                    if len(subject) >= 2:
                        return subject
        return ""
