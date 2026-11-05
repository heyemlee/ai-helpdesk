from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class RRFFusionConfig:
    """RRF 融合配置，用于控制候选融合和截断策略。"""

    rrf_k: int = 60  # RRF 平滑参数，越大排名差异越平滑
    truncate_multiplier: int = 6  # 根据关键词数量扩展融合候选数
    min_fused_top_k: int = 10  # 融合候选数量下限
    max_fused_top_k: int = 50  # 融合候选数量上限
    final_top_k: int = 8  # 最终输出候选数量

    route_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "keyword": 1.0,
            "vector": 1.0,
        }
    )  # 不同召回通道的融合权重


@dataclass
class RouteRecallResult:
    """单个召回通道在某个检索词下的召回结果。"""

    route_name: str  # 召回通道名称，如 keyword、vector
    query_term: str  # 当前召回使用的检索词
    ranked_doc_indices: List[int]  # 按相关性排序的文档下标


@dataclass
class RRFFusionHit:
    """RRF 融合后的候选字段结果。"""

    doc_index: int  # 字段文档下标
    score: float  # RRF 融合得分
    matched_terms: List[str]  # 命中的检索词
    sources: List[str]  # 命中的召回通道
    best_rank_by_source: Dict[str, int]  # 各召回通道中的最好排名


class RRFFusionClient:
    """RRF 融合客户端，用于融合关键词召回和向量召回结果。"""

    def __init__(self, config: Optional[RRFFusionConfig] = None):
        self.config = config or RRFFusionConfig()

    def fuse(
        self,
        route_results: List[RouteRecallResult],
        keyword_count: int,
        final_top_k: Optional[int] = None,
    ) -> List[RRFFusionHit]:
        """对多个召回通道的结果进行 RRF 融合排序。"""
        fused_scores: Dict[int, float] = {}
        matched_terms_map: Dict[int, Set[str]] = {}
        sources_map: Dict[int, Set[str]] = {}
        best_rank_by_source_map: Dict[int, Dict[str, int]] = {}

        for route_result in route_results:
            route_name = route_result.route_name
            query_term = route_result.query_term
            route_weight = self.config.route_weights.get(route_name, 1.0)

            for rank, doc_index in enumerate(route_result.ranked_doc_indices, start=1):
                score_delta = route_weight / (self.config.rrf_k + rank)

                fused_scores[doc_index] = fused_scores.get(doc_index, 0.0) + score_delta

                matched_terms_map.setdefault(doc_index, set()).add(query_term)
                sources_map.setdefault(doc_index, set()).add(route_name)

                source_rank_map = best_rank_by_source_map.setdefault(doc_index, {})

                if route_name not in source_rank_map:
                    source_rank_map[route_name] = rank
                else:
                    source_rank_map[route_name] = min(source_rank_map[route_name], rank)

        fused_top_k = self.calculate_fused_top_k(keyword_count)

        sorted_items = sorted(
            fused_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )

        truncated_items = sorted_items[:fused_top_k]

        output_top_k = final_top_k or self.config.final_top_k
        final_items = truncated_items[:output_top_k]

        return [
            RRFFusionHit(
                doc_index=doc_index,
                score=score,
                matched_terms=sorted(matched_terms_map.get(doc_index, set())),
                sources=sorted(sources_map.get(doc_index, set())),
                best_rank_by_source=best_rank_by_source_map.get(doc_index, {}),
            )
            for doc_index, score in final_items
        ]

    def calculate_fused_top_k(self, keyword_count: int) -> int:
        """根据关键词数量动态计算融合候选截断数量。"""
        dynamic_top_k = keyword_count * self.config.truncate_multiplier

        return max(
            self.config.min_fused_top_k,
            min(dynamic_top_k, self.config.max_fused_top_k),
        )