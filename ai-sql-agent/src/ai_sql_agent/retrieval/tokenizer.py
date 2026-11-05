from __future__ import annotations

import re
from typing import List


def tokenize(text: str) -> List[str]:
    """
    中英文混合分词。

    英文、数字和下划线按连续词切分；
    中文同时保留连续片段、单字和 bigram，用于提升召回覆盖率。
    """
    text = text.lower()
    tokens: List[str] = []

    # 英文、数字、下划线关键词
    tokens.extend(re.findall(r"[a-zA-Z0-9_]+", text))

    chinese_spans = re.findall(r"[\u4e00-\u9fff]+", text)

    for span in chinese_spans:
        tokens.append(span)

        # 中文单字召回
        tokens.extend(list(span))

        # 中文 bigram 召回，例如“销售额” -> “销售”“售额”
        for i in range(len(span) - 1):
            tokens.append(span[i:i + 2])

    return [token for token in tokens if token.strip()]