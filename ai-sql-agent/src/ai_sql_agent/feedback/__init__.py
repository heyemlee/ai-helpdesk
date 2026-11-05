"""
用户反馈回路：让工具越用越准。

答错的进反面教材清单，用来补评测集——评测集原本全是我们自己出的模板题，
真实用户问法与它的差距正是线上准确率与评测准确率的差距。
答对的成为认证范例，注入后续同类问题的生成 Prompt，把这家公司的写法固化下来。

反馈只收「用户表态」，问题原文与实际执行的 SQL 一律以审计记录为准。
"""

from .certified import CertifiedSqlLibrary, render_certified_examples
from .objects import CertifiedQuery, FeedbackRecord, FeedbackStats, Verdict
from .store import (
    FeedbackStore,
    NullFeedbackStore,
    PostgresFeedbackStore,
    SQLiteFeedbackStore,
    create_feedback_store,
)

__all__ = [
    "CertifiedQuery",
    "CertifiedSqlLibrary",
    "FeedbackRecord",
    "FeedbackStats",
    "FeedbackStore",
    "NullFeedbackStore",
    "PostgresFeedbackStore",
    "SQLiteFeedbackStore",
    "Verdict",
    "create_feedback_store",
    "render_certified_examples",
]
