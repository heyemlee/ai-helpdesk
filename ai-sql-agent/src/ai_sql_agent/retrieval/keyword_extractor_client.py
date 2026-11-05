from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from ai_sql_agent.core.model_client import FollowsModelSettings
from ai_sql_agent.core.model_text import plain_text


@dataclass
class AliyunKeywordExtractorConfig:
    """
    关键词抽取模型配置。

    该配置用于调用大模型，从用户 Query 中抽取核心检索词。
    """

    api_key: str = ""
    """模型 API Key。为空时取 Settings（OPENAI_API_KEY / ANTHROPIC_API_KEY）。"""

    chat_url: str = ""
    """Chat 接口地址，为空时取 Settings（按 provider 从 BASE_URL 拼出来）。"""

    model: str = ""
    """用于关键词抽取的大模型名称，为空时取 Settings。"""

    provider: str = ""
    """openai（OpenAI 兼容）或 anthropic（Messages），为空时取 Settings。"""

    timeout: Optional[float] = None
    """HTTP 请求超时时间（秒），None 时取 Settings。"""


class AliyunKeywordExtractor(FollowsModelSettings):
    """
    基于大模型的核心检索词抽取器。

    该类负责：
    1. 构造关键词抽取 Prompt。
    2. 调用 Chat 接口（协议由 provider 决定）。
    3. 使用正则从模型输出中解析关键词。
    """

    SYSTEM_PROMPT = """你是一个字段解析助手。

任务：
从用户输入的查询中提取核心字段、业务指标、表级概念以及关键业务语义信息。

要求：
1. 提取字段、指标、表格或业务概念等核心关键词
2. 尽量保留用户原始表达，不进行语义改写
3. 不输出解释、分析过程或额外内容
4. 多个关键词之间使用全角逗号分隔
5. 若存在聚合语义，如数量、总金额、平均值等，需完整保留
6. 若存在业务限定词，如采购金额、准时交付率等，禁止拆分

输出格式：
解析结果：<关键词1>，<关键词2>

示例1：
用户Query：查询用户年龄大于30的用户数量
解析结果：用户年龄，用户数量

示例2：
用户Query：统计每个城市的订单总金额
解析结果：城市，订单总金额

示例3：
用户Query：查询准时率低于0.9的供应商名称
解析结果：准时率，供应商名称
"""

    GLOSSARY_PROMPT = """你是一个字段解析助手。

任务：
从用户输入的查询中提取核心检索词，并把口语说法映射到下面「业务术语表」里的字段。

要求：
1. 先按用户原话提取关键词，保留原始表达，不做改写；
2. 再判断这些说法在业务术语表里对应哪些字段或表，输出对应的**中文含义词**
   （不要输出英文字段名，检索索引里存的是中文描述与别名）；
3. 映射不确定就不要输出，宁可少给也不要瞎猜；映射最多 6 个；
4. 不输出解释、分析过程或额外内容；
5. 多个关键词之间使用全角逗号分隔。

输出格式（严格两行）：
解析结果：<原话关键词1>，<原话关键词2>
映射术语：<术语1>，<术语2>

# 业务术语表
{glossary}
"""

    COMPONENT = "keyword_extractor"
    URL_FIELD = "chat_url"

    MISSING_KEY = (
        "缺少模型 API Key。请在 .env 里配置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY，"
        "或在 AliyunKeywordExtractorConfig(api_key='...') 中传入。"
    )

    def __init__(self, config: AliyunKeywordExtractorConfig | None = None):
        self._bind_connection(config or AliyunKeywordExtractorConfig())
        if not self.config.api_key:
            raise ValueError(self.MISSING_KEY)

    def extract(self, query: str, glossary: str = "") -> List[str]:
        """
        从用户 Query 中抽取核心检索词。

        glossary 非空时额外做一次「口语 → Schema 术语」映射，两组词合并返回。
        原始表达仍然保留：映射是**增量**，不是替换——标准业务说法本来就能召回，
        没必要为了口语说法把它们搞坏。

        Args:
            query: 用户自然语言查询。
            glossary: 业务术语表（由 Schema 元数据生成，见 build_schema_glossary）。

        Returns:
            List[str]: 核心检索词列表。
        """
        content = self._call_llm(query, glossary=glossary)
        return self._parse_keywords(content)

    def _call_llm(self, query: str, glossary: str = "") -> str:
        """
        调用大模型完成关键词抽取。

        Args:
            query: 用户自然语言查询。
            glossary: 业务术语表，为空时走原来的纯抽词 Prompt。

        Returns:
            str: 大模型返回的原始文本。
        """
        system_prompt = (
            self.GLOSSARY_PROMPT.format(glossary=glossary)
            if glossary.strip()
            else self.SYSTEM_PROMPT
        )
        connection = self._connection()
        if not connection.has_key:
            # 构造之后 Key 被撤掉了（页面恢复成了没有 Key 的服务端配置）：不拿空 Key 去撞
            raise ValueError(self.MISSING_KEY)
        return connection.transport.chat(
            connection.url,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"用户Query：{query}"},
            ],
            model=connection.model,
            temperature=0.0,
        )

    def _parse_keywords(self, text: str) -> List[str]:
        """
        从模型输出中解析关键词。

        Args:
            text: 模型输出文本。

        Returns:
            List[str]: 去重后的关键词列表。
        """
        # 「**解析结果**：」这类加了格式的写法，不去掉记号就按标签找不到，整段回答会被当成关键词
        text = plain_text(text)
        segments = re.findall(r"(?:解析结果|映射术语)[:：]\s*(.+)", text)
        keyword_text = "，".join(segments) if segments else text.strip()

        raw_keywords = re.split(r"[，,\n；;]+", keyword_text)

        keywords: List[str] = []
        seen = set()

        for item in raw_keywords:
            keyword = item.strip()

            if not keyword:
                continue

            if keyword not in seen:
                keywords.append(keyword)
                seen.add(keyword)

        if not keywords:
            raise ValueError(f"未能从模型输出中解析到关键词，模型输出为: {text}")

        return keywords