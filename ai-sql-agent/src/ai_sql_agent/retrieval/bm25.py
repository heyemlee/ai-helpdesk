from __future__ import annotations

import math
from collections import Counter
from typing import Dict, List, Tuple

from .tokenizer import tokenize


class BM25Index:
    """
    轻量 BM25 实现。
    用于字段名称、字段别名、业务词、样例值的关键词召回。
    """

    def __init__(self, docs: List[str], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1 = k1
        self.b = b

        self.tokenized_docs = [tokenize(doc) for doc in docs]
        self.doc_lengths = [len(doc) for doc in self.tokenized_docs]
        self.avg_doc_length = (
            sum(self.doc_lengths) / len(self.doc_lengths)
            if self.doc_lengths else 0.0
        )

        self.term_freqs = [Counter(doc) for doc in self.tokenized_docs]
        self.doc_freqs: Dict[str, int] = Counter()

        for doc in self.tokenized_docs:
            for term in set(doc):
                self.doc_freqs[term] += 1

        self.num_docs = len(docs)

    def search(self, query: str, top_k: int = 10) -> List[Tuple[int, float]]:
        query_terms = tokenize(query)

        results: List[Tuple[int, float]] = []

        for doc_idx, term_freq in enumerate(self.term_freqs):
            score = 0.0
            doc_len = self.doc_lengths[doc_idx] or 1

            for term in query_terms:
                if term not in term_freq:
                    continue

                freq = term_freq[term]
                doc_freq = self.doc_freqs.get(term, 0)

                idf = math.log(
                    1 + (self.num_docs - doc_freq + 0.5) / (doc_freq + 0.5)
                )

                numerator = freq * (self.k1 + 1)
                denominator = freq + self.k1 * (
                    1 - self.b + self.b * doc_len / max(self.avg_doc_length, 1e-8)
                )

                score += idf * numerator / max(denominator, 1e-8)

            if score > 0:
                results.append((doc_idx, score))

        results.sort(key=lambda item: item[1], reverse=True)
        return results[:top_k]
