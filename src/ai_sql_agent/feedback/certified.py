from __future__ import annotations

from typing import Callable, List, Sequence

from ai_sql_agent.auth import extract_referenced_tables
from ai_sql_agent.retrieval.tokenizer import tokenize

from .objects import CertifiedQuery

MIN_SIMILARITY = 0.34
"""
低于这个相似度就不注入。

范例的价值在于「同一类问题上次是这么写对的」。相关性不够的范例不是中性的：
模型会去模仿它的表和写法，把当前问题往那条老查询上带。宁可不给。
"""


class CertifiedSqlLibrary:
    """
    认证查询库：把用户确认过「答对了」的 SQL，作为范例注入后续生成。

    这是反馈回路里「正向」的那一半——反面进评测集，正面进 Prompt。
    价值不在于省一次模型调用，而在于把这家公司的写法固化下来：
    同一个问题第二次被问到时，模型看到的是上次被人确认过的那条 SQL，
    而不是每次都从 Schema 重新猜一遍 join 路径和口径。

    三条安全约束：

    1. 只取**当前用户自己**确认过的查询——别人的查询里有他没权限看的表名；
    2. 注入前按当前权限再过一遍表，权限收紧后老范例不能继续出现在 Prompt 里；
    3. 范例只是范例：生成的 SQL 照样过 SQL 守卫、权限校验和执行治理，
       认证不等于放行。
    """

    def __init__(
        self,
        loader: Callable[[str], Sequence[CertifiedQuery]],
        top_k: int = 2,
        min_similarity: float = MIN_SIMILARITY,
    ):
        self.loader = loader
        """user_id -> 该用户的认证查询列表。"""

        self.top_k = top_k
        self.min_similarity = min_similarity

    def lookup(
        self,
        *,
        query: str,
        database: str,
        user_id: str,
        access=None,
    ) -> List[CertifiedQuery]:
        """取与当前问题最相近的认证范例（同库、有权限、相似度达标）。"""
        if not query.strip() or not user_id or self.top_k < 1:
            return []

        try:
            candidates = list(self.loader(user_id))
        except Exception:
            # 范例是增强项，取不到就不给，不能影响主链路
            return []

        wanted = set(tokenize(query))
        if not wanted:
            return []

        scored: List[tuple[float, CertifiedQuery]] = []
        for candidate in candidates:
            if candidate.database != database:
                continue
            if candidate.query.strip() == query.strip():
                # 完全相同的问题走的是另一条路（降级重放），
                # 当范例给出去等于把答案直接抄给模型，评测上会虚高
                continue
            if not self._allowed(candidate, access):
                continue
            score = self._similarity(wanted, candidate.query)
            if score >= self.min_similarity:
                scored.append((score, candidate))

        scored.sort(key=lambda item: (-item[0], item[1].created_at))
        return [candidate for _, candidate in scored[: self.top_k]]

    @staticmethod
    def _similarity(wanted: set, other: str) -> float:
        """Jaccard 相似度。分词器与检索同一个，中文按字与 bigram 切。"""
        tokens = set(tokenize(other))
        if not tokens:
            return 0.0
        overlap = len(wanted & tokens)
        union = len(wanted | tokens)
        return overlap / union if union else 0.0

    @staticmethod
    def _allowed(candidate: CertifiedQuery, access) -> bool:
        """按当前权限过一遍范例里引用的表。"""
        if access is None:
            return True
        tables = extract_referenced_tables(candidate.sql)
        return all(
            access.allows_table(candidate.database, table) for table in tables
        )


def render_certified_examples(examples: Sequence[CertifiedQuery]) -> str:
    """
    把认证范例渲染成注入 SQL 生成 Prompt 的段落。

    刻意写清楚「这些是别的问题的答案，不是本题答案」：不加这句时，
    模型会把范例的筛选条件一起抄进来——范例越相关，抄错得越像模像样。
    """
    if not examples:
        return ""

    lines = [
        "# 已确认正确的相似查询（供参考，不是本题答案）",
        "",
        "下面是**其他问题**上被用户确认过结果正确的 SQL，写法与口径可以参考"
        "（表怎么关联、金额用哪个字段、状态怎么筛）。",
        "但它们回答的是各自的问题：**不要照抄筛选条件**，"
        "本题的条件以「当前步骤CoT」为准。",
        "",
    ]
    for index, example in enumerate(examples, start=1):
        lines.append(f"## 参考{index}：{example.query}")
        lines.append(example.sql.strip())
        lines.append("")
    return "\n".join(lines).strip()
