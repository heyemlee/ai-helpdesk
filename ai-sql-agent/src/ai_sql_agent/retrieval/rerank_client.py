from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ai_sql_agent.core import ModelTransport, get_settings
from ai_sql_agent.core.errors import ModelResponseError


@dataclass
class RerankDocument:
    """
    待重排序的候选文档。
    """

    doc_index: int
    """候选文档在 documents 列表中的下标。"""

    text: str
    """候选文档文本。"""


@dataclass
class RerankResult:
    """
    重排序结果。
    """

    doc_index: int
    """原始候选文档下标。"""

    rerank_score: float
    """Rerank 模型输出的相关性分数。"""

    text: str
    """候选文档文本。"""


@dataclass
class AliyunRerankConfig:
    """
    阿里云百炼 Rerank 配置。

    qwen3-rerank 使用业务空间专属 endpoint。
    """

    api_key: str = ""
    """Rerank API Key。为空时取 Settings（AI_SQL_RERANK_API_KEY / DASHSCOPE_API_KEY）。"""

    workspace_id: str = ""
    """阿里云百炼业务空间 ID。为空时读取环境变量 DASHSCOPE_WORKSPACE_ID。"""

    rerank_url: str = ""
    """
    Rerank HTTP 接口地址。

    如果不传，则根据 workspace_id 自动拼接：
    https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks
    """

    model: str = ""
    """Rerank 模型名称，为空时取 Settings（默认 qwen3-rerank）。"""

    timeout: Optional[float] = None
    """HTTP 请求超时时间（秒），None 时取 Settings。"""

    instruct: str = (
        "Given a database schema field retrieval query, "
        "rank candidate schema fields by relevance to the query."
    )
    """Rerank 任务说明。"""


class AliyunRerankClient:
    """
    阿里云百炼 Rerank Client。

    该 Client 负责对 RRF 融合后的候选字段进行二次排序。
    如果没有配置 API Key 或 Workspace ID，则使用本地 Mock 排序，方便先跑通 Demo。
    """

    COMPONENT = "rerank"

    def __init__(self, config: Optional[AliyunRerankConfig] = None):
        self.config = config or AliyunRerankConfig()
        settings = get_settings()

        if not self.config.api_key:
            self.config.api_key = settings.resolved_rerank_api_key()
        if not self.config.workspace_id:
            self.config.workspace_id = settings.dashscope_workspace_id
        if not self.config.model:
            self.config.model = settings.rerank_model
        if not self.config.rerank_url:
            self.config.rerank_url = settings.resolved_rerank_url() or (
                f"https://{self.config.workspace_id}.cn-beijing.maas.aliyuncs.com"
                f"/compatible-api/v1/reranks"
                if self.config.workspace_id
                else ""
            )

        self.transport = ModelTransport(
            self.COMPONENT,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )

    def rerank(
        self,
        query: str,
        documents: List[RerankDocument],
        top_n: int,
    ) -> List[RerankResult]:
        """
        对候选文档进行重排序。

        Args:
            query: 用户原始查询。
            documents: 待重排序候选文档。
            top_n: 返回的候选数量。

        Returns:
            List[RerankResult]: 重排序后的候选结果。
        """
        if not documents:
            return []

        top_n = min(top_n, len(documents))

        if not self.config.api_key or not self.config.rerank_url:
            return self._mock_rerank(
                query=query,
                documents=documents,
                top_n=top_n,
            )

        return self._call_aliyun_rerank(
            query=query,
            documents=documents,
            top_n=top_n,
        )

    def _call_aliyun_rerank(
        self,
        query: str,
        documents: List[RerankDocument],
        top_n: int,
    ) -> List[RerankResult]:
        """
        调用阿里云百炼 qwen3-rerank。
        """
        result = self.transport.post_json(
            self.config.rerank_url,
            {
                "model": self.config.model,
                "query": query,
                "documents": [doc.text for doc in documents],
                "top_n": top_n,
                "instruct": self.config.instruct,
            },
            model=self.config.model,
        )

        try:
            result_items = result["results"]
        except (KeyError, TypeError) as exc:
            raise ModelResponseError(
                f"解析 Rerank 返回失败，原始返回: {str(result)[:500]}",
                component=self.COMPONENT,
                model=self.config.model,
            ) from exc

        output: List[RerankResult] = []

        for item in result_items:
            local_index = int(item["index"])
            source_doc = documents[local_index]

            output.append(
                RerankResult(
                    doc_index=source_doc.doc_index,
                    rerank_score=float(item["relevance_score"]),
                    text=source_doc.text,
                )
            )

        output.sort(key=lambda item: item.rerank_score, reverse=True)

        return output

    def _mock_rerank(
        self,
        query: str,
        documents: List[RerankDocument],
        top_n: int,
    ) -> List[RerankResult]:
        """
        本地 Mock Rerank。

        仅用于无 API Key 或无 Workspace ID 时跑通流程。
        按 Query 中文二元/三元组与英文 Token 在候选文本中的覆盖度打分。
        """
        query_terms = self._extract_terms(query)

        scored_results: List[RerankResult] = []

        for doc in documents:
            score = 0.0

            for term in query_terms:
                if term in doc.text:
                    # 更长的词命中权重更高
                    score += len(term)

            scored_results.append(
                RerankResult(
                    doc_index=doc.doc_index,
                    rerank_score=score,
                    text=doc.text,
                )
            )

        scored_results.sort(key=lambda item: item.rerank_score, reverse=True)

        return scored_results[:top_n]

    @staticmethod
    def _extract_terms(query: str) -> List[str]:
        """
        从 Query 中提取打分词：英文 Token + 中文二元/三元组。
        """
        import re

        terms = re.findall(r"[a-zA-Z0-9_]+", query.lower())

        for span in re.findall(r"[一-鿿]+", query):
            for i in range(len(span) - 1):
                terms.append(span[i:i + 2])
            for i in range(len(span) - 2):
                terms.append(span[i:i + 3])

        return list(dict.fromkeys(terms))