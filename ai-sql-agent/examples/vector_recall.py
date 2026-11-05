from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

from ai_sql_agent.core import ModelTransport

from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader


# =============================================================================
# 1. 向量召回文档对象
# =============================================================================


@dataclass
class VectorFieldDocument:
    """
    字段级向量检索文档。

    该对象只用于当前向量召回 Demo，不依赖 document_builder.py。
    这样可以避免 objects.py 和 document_builder.py 还在逐步改造时影响 Demo 运行。
    """

    doc_id: str
    """字段文档唯一标识。"""

    column: object
    """字段 Schema 对象。"""

    vector_text: str
    """向量索引文本。"""


# =============================================================================
# 2. 关键词抽取
# =============================================================================


@dataclass
class KeywordExtractorConfig:
    """
    关键词抽取模型配置。

    当 api_key 为空时，使用本地规则抽取，便于无 API Key 环境下跑通 Demo。
    当 api_key 不为空时，调用阿里云百炼 OpenAI 兼容 Chat Completions 接口。
    """

    api_key: str = ""
    """阿里云百炼 API Key。为空时读取环境变量 DASHSCOPE_API_KEY。"""

    chat_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    """阿里云百炼 OpenAI 兼容 Chat Completions 接口地址。"""

    model: str = "qwen-plus"
    """用于关键词抽取的大模型名称。"""

    timeout: int = 60
    """HTTP 请求超时时间，单位为秒。"""


class KeywordExtractor:
    """
    基于大模型的核心检索词抽取器。

    该抽取器用于从用户 Query 中提取字段、指标、表级概念和关键业务语义信息。
    向量召回阶段不直接对完整 Query 做 Embedding，而是对抽取后的关键词做 Embedding。
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
6. 若存在业务限定词，如准时率、供应商名称等，禁止拆分

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

    def __init__(self, config: KeywordExtractorConfig | None = None):
        self.config = config or KeywordExtractorConfig()

        if not self.config.api_key:
            self.config.api_key = os.getenv("DASHSCOPE_API_KEY", "")

    def extract(self, query: str) -> List[str]:
        """
        从用户 Query 中抽取核心检索词。

        Args:
            query: 用户自然语言查询。

        Returns:
            List[str]: 核心检索词列表。
        """
        if not self.config.api_key:
            return self._extract_by_rule(query)

        content = self._call_llm(query)
        return self._parse_keywords(content)

    def _call_llm(self, query: str) -> str:
        """
        调用阿里云百炼大模型完成关键词抽取。

        Args:
            query: 用户自然语言查询。

        Returns:
            str: 大模型返回的原始文本。
        """
        result = ModelTransport(
            "keyword_extractor",
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        ).post_json(
            self.config.chat_url,
            {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": f"用户Query：{query}"},
                ],
                "temperature": 0,
            },
            model=self.config.model,
        )

        try:
            return result["choices"][0]["message"]["content"].strip()

        except Exception as exc:
            raise RuntimeError(f"解析关键词抽取模型返回失败，原始返回: {result}") from exc

    def _parse_keywords(self, text: str) -> List[str]:
        """
        从模型输出中解析关键词。

        Args:
            text: 模型输出文本。

        Returns:
            List[str]: 去重后的关键词列表。
        """
        match = re.search(r"解析结果[:：]\s*(.+)", text)

        if match:
            keyword_text = match.group(1).strip()
        else:
            keyword_text = text.strip()

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

    def _extract_by_rule(self, query: str) -> List[
            "准时率",
            "订单金额",
            "定金",
            "供应商名称",
            "供应商",
            "品类",
        ]:
        """
        本地规则抽取。

        仅用于无 API Key 环境下跑通 Demo。生产环境建议使用大模型抽取。
        """
        known_phrases = [
            "订单总金额",
            "准时率",
            "订单金额",
            "定金",
            "供应商名称",
            "供应商",
            "品类",
            "城市",
            "销售额",
            "用户数量",
            "用户年龄",
        ]

        keywords: List[str] = []
        seen = set()

        for phrase in known_phrases:
            if phrase in query and phrase not in seen:
                keywords.append(phrase)
                seen.add(phrase)

        if not keywords:
            normalized = re.sub(r"[？?。.\s]+", "", query)
            if normalized:
                keywords.append(normalized)

        return keywords


# =============================================================================
# 3. Embedding Client
# =============================================================================


@dataclass
class AliyunEmbeddingConfig:
    """
    阿里云百炼 Embedding 配置。

    当 api_key 为空时，使用本地 Hash Embedding，便于无 API Key 环境下跑通 Demo。
    当 api_key 不为空时，调用阿里云百炼 OpenAI 兼容 Embedding 接口。
    """

    api_key: str = ""
    """阿里云百炼 API Key。为空时读取环境变量 DASHSCOPE_API_KEY。"""

    embedding_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    """阿里云百炼 OpenAI 兼容 Embedding 接口地址。"""

    model: str = "text-embedding-v4"
    """Embedding 模型名称。"""

    dimensions: int = 1024
    """Embedding 向量维度。"""

    timeout: int = 60
    """HTTP 请求超时时间，单位为秒。"""


class AliyunEmbeddingClient:
    """
    阿里云百炼 Embedding Client。

    该 Client 用于将字段索引文本和抽取后的核心检索词转换为向量。
    """

    def __init__(self, config: AliyunEmbeddingConfig | None = None):
        self.config = config or AliyunEmbeddingConfig()

        if not self.config.api_key:
            self.config.api_key = os.getenv("DASHSCOPE_API_KEY", "")

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        批量生成文本向量。

        Args:
            texts: 待向量化的文本列表。

        Returns:
            np.ndarray: 归一化后的向量矩阵。
        """
        if not texts:
            return np.empty((0, self.config.dimensions), dtype=np.float32)

        if not self.config.api_key:
            return self._local_hash_embedding(texts)

        return self._call_aliyun_embedding(texts)

    def _call_aliyun_embedding(self, texts: List[str]) -> np.ndarray:
        """
        调用阿里云百炼 Embedding API。

        Args:
            texts: 待向量化的文本列表。

        Returns:
            np.ndarray: 归一化后的向量矩阵。
        """
        result = ModelTransport(
            "embedding",
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        ).post_json(
            self.config.embedding_url,
            {
                "model": self.config.model,
                "input": texts,
                "dimensions": self.config.dimensions,
                "encoding_format": "float",
            },
            model=self.config.model,
        )

        try:
            data_items = sorted(result["data"], key=lambda item: item.get("index", 0))
            embeddings = [item["embedding"] for item in data_items]

        except Exception as exc:
            raise RuntimeError(f"解析 Embedding API 返回结果失败，原始返回: {result}") from exc

        vectors = np.array(embeddings, dtype=np.float32)
        return self._normalize(vectors)

    def _local_hash_embedding(self, texts: List[str]) -> np.ndarray:
        """
        本地 Hash Embedding。

        仅用于无 API Key 环境下验证流程，不代表真实语义向量效果。
        """
        vectors = np.zeros((len(texts), self.config.dimensions), dtype=np.float32)

        for row_index, text in enumerate(texts):
            for token in self._tokenize_for_hash(text):
                digest = hashlib.md5(token.encode("utf-8")).hexdigest()
                col_index = int(digest, 16) % self.config.dimensions
                vectors[row_index, col_index] += 1.0

        return self._normalize(vectors)

    def _normalize(self, vectors: np.ndarray) -> np.ndarray:
        """
        对向量进行 L2 归一化。

        Args:
            vectors: 原始向量矩阵。

        Returns:
            np.ndarray: 归一化后的向量矩阵。
        """
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-8)

    def _tokenize_for_hash(self, text: str) -> List[str]:
        """
        本地 Hash Embedding 使用的简单分词方法。
        """
        text = text.lower()
        tokens: List[str] = []

        tokens.extend(re.findall(r"[a-zA-Z0-9_]+", text))

        chinese_spans = re.findall(r"[\u4e00-\u9fff]+", text)
        for span in chinese_spans:
            tokens.append(span)
            tokens.extend(list(span))

            for i in range(len(span) - 1):
                tokens.append(span[i:i + 2])

            for i in range(len(span) - 2):
                tokens.append(span[i:i + 3])

        return [token for token in tokens if token.strip()]


# =============================================================================
# 4. 内存向量索引
# =============================================================================


class VectorIndex:
    """
    内存向量索引。

    当前 Demo 使用 numpy 矩阵保存字段向量，并通过余弦相似度完成向量召回。
    生产环境走 pgvector（见 pgvector_index.py）。
    """

    def __init__(self, embedding_client: AliyunEmbeddingClient):
        self.embedding_client = embedding_client
        self.documents: List[str] = []
        self.embeddings: np.ndarray | None = None

    def build(self, documents: List[str]) -> None:
        """
        构建向量索引。

        Args:
            documents: 字段级向量索引文本列表。
        """
        self.documents = documents
        self.embeddings = self.embedding_client.embed_texts(documents)

    def search(self, query_text: str, top_k: int = 5) -> List[Tuple[int, float]]:
        """
        使用单个检索词进行向量召回。

        Args:
            query_text: 单个核心检索词。
            top_k: 返回候选数量。

        Returns:
            List[Tuple[int, float]]: 文档下标与相似度得分。
        """
        if self.embeddings is None:
            raise RuntimeError("VectorIndex 尚未构建，请先调用 build()。")

        query_vector = self.embedding_client.embed_texts([query_text])[0]
        scores = self.embeddings @ query_vector

        top_indices = np.argsort(-scores)[:top_k]

        return [
            (int(index), float(scores[index]))
            for index in top_indices
        ]


# =============================================================================
# 5. Demo 数据库与业务元数据
# =============================================================================


def create_demo_database() -> Path:
    """
    创建 Demo 数据库：ERP 订单与采购域的两张核心表。
    - supplier: 供应商（含准时交付率）
    - purchase_order: 采购单（po_amt 为 ERP 缩写命名，字面无业务语义）
    """
    db_path = Path(tempfile.gettempdir()) / "text2sql_vector_recall_demo.db"

    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        '''
        CREATE TABLE supplier (
            supplier_id   TEXT PRIMARY KEY,
            supplier_name TEXT NOT NULL,
            category      TEXT NOT NULL,
            on_time_rate  REAL NOT NULL
        );

        CREATE TABLE purchase_order (
            po_no       TEXT PRIMARY KEY,
            job_no      TEXT NOT NULL,
            supplier_id TEXT NOT NULL,
            po_amt      REAL NOT NULL,
            po_date     TEXT NOT NULL,
            po_status   TEXT NOT NULL,
            FOREIGN KEY (supplier_id) REFERENCES supplier(supplier_id)
        );
        '''
    )
    conn.executemany(
        "INSERT INTO supplier VALUES (?, ?, ?, ?)",
        [
            ("SUP-01", "华鹏板材", "板材", 0.95),
            ("SUP-02", "东顺五金", "五金", 0.88),
            ("SUP-03", "杰森石英石", "台面", 0.72),
            ("SUP-04", "立信涂料", "涂料", 0.91),
            ("SUP-05", "广辉玻璃", "玻璃", 0.65),
        ],
    )
    conn.executemany(
        "INSERT INTO purchase_order VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("PO-24-0101", "J24001", "SUP-01", 386000.00, "2024-01-25", "received"),
            ("PO-24-0102", "J24001", "SUP-02", 92000.00, "2024-01-26", "received"),
            ("PO-24-0103", "J24001", "SUP-03", 158000.00, "2024-02-02", "received"),
            ("PO-24-0301", "J24003", "SUP-01", 742000.00, "2024-04-01", "partial"),
            ("PO-24-0303", "J24003", "SUP-05", 88000.00, "2024-04-10", "ordered"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path

def get_business_meta() -> dict:
    """
    Demo 业务元数据。

    三类文本：keyword_text 关键词召回 / vector_text 向量召回 / rerank_text 精排。
    注意 po_amt 这类 ERP 缩写命名：字面无业务语义，向量检索对它表现差，
    必须靠 keyword_text 补业务口语别名让 BM25 那一路命中。
    """
    return {
        "supplier": {
            "description": "供应商表，记录供应商名称、供货品类与准时交付率。",
            "aliases": ["供应商表", "供方表"],
            "columns": {
                "supplier_id": {
                    "description": "供应商编号。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "用于表关联。",
                    "keyword_text": "supplier_id 供应商ID 供应商编号 关联字段",
                    "vector_text": "字段名：supplier_id。所属表：supplier。该字段是供应商唯一编号，用于关联采购单表。",
                    "rerank_text": "字段：supplier.supplier_id。含义：供应商编号。角色：join_key。",
                },
                "supplier_name": {
                    "description": "供应商名称。",
                    "aliases": ["供应商", "供应商名称", "供方名称", "厂家"],
                    "semantic_role": "output_dimension",
                    "samples": ["华鹏板材", "东顺五金", "杰森石英石"],
                    "keyword_text": "supplier_name 供应商 供应商名称 供方 厂家",
                    "vector_text": "字段名：supplier_name。所属表：supplier。该字段表示供应商名称，常作为查询结果输出。",
                    "rerank_text": "字段：supplier.supplier_name。含义：供应商名称。用途：询问是哪家供应商时作为输出字段。",
                },
                "category": {
                    "description": "供货品类。",
                    "aliases": ["品类", "供货品类", "材料类别"],
                    "semantic_role": "dimension",
                    "value_range": "板材 / 五金 / 台面 / 涂料 / 玻璃",
                    "samples": ["板材", "五金", "台面", "涂料", "玻璃"],
                    "business_usage": "按品类筛选供应商。",
                    "keyword_text": "category 品类 供货品类 材料类别 板材 五金 台面 涂料 玻璃",
                    "vector_text": "字段名：category。所属表：supplier。该字段表示供应商的供货品类。",
                    "rerank_text": "字段：supplier.category。含义：供货品类。取值：板材、五金、台面、涂料、玻璃。",
                },
                "on_time_rate": {
                    "description": "准时交付率，0-1 之间。",
                    "aliases": ["准时率", "准时交付率", "及时率", "交付准时率"],
                    "semantic_role": "metric_filter",
                    "value_range": "0-1",
                    "data_distribution": "多数供应商在 0.85 以上，长尾供应商可能低于 0.7。",
                    "business_usage": "评估供应商交付表现，筛选准时率低的供应商。",
                    "samples": ["0.65", "0.72", "0.88", "0.95"],
                    "keyword_text": "on_time_rate 准时率 准时交付率 及时率 交付准时率 低于 供应商表现",
                    "vector_text": "字段名：on_time_rate。所属表：supplier。该字段表示供应商的准时交付率，用于筛选交付表现差的供应商。",
                    "rerank_text": "字段：supplier.on_time_rate。含义：准时交付率（0-1）。别名：准时率、及时率。用途：筛选准时率低于阈值的供应商。",
                    "index_texts": {
                        "vector_text": "字段名：on_time_rate。所属表：supplier。该字段表示供应商的准时交付率。"
                    },
                },
            },
        },
        "purchase_order": {
            "description": "采购单表，记录向供应商下达的采购单与含税采购金额。",
            "aliases": ["采购单表", "采购订单表", "PO表"],
            "columns": {
                "po_no": {
                    "description": "采购单号。",
                    "aliases": ["采购单号", "PO号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识采购单。",
                },
                "job_no": {
                    "description": "工程号，ERP 内部编码。",
                    "aliases": ["工程号", "项目号"],
                    "semantic_role": "join_key",
                    "business_usage": "按工程归集采购。",
                    "keyword_text": "job_no 工程号 项目号 ERP编码",
                    "vector_text": "字段名：job_no。所属表：purchase_order。该字段是采购单归属的工程编号。",
                },
                "supplier_id": {
                    "description": "供应商编号。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联供应商表。",
                },
                "po_amt": {
                    "description": "采购金额，含税。ERP 缩写命名，字面无业务语义。",
                    "aliases": ["采购金额", "采购额", "进货金额", "采购成本"],
                    "semantic_role": "metric_filter",
                    "value_range": "> 0",
                    "business_usage": "统计采购支出、按工程归集材料成本。",
                    "samples": ["88000.00", "158000.00", "742000.00"],
                    "keyword_text": "po_amt 采购金额 采购额 进货金额 采购成本 含税 采购单表 PO表",
                    "vector_text": "字段名：po_amt。所属表：purchase_order。该字段表示含税的采购金额，用于统计采购支出。",
                    "rerank_text": "字段：purchase_order.po_amt。含义：含税采购金额（ERP 缩写码）。别名：采购金额、采购额、采购成本。",
                    "index_texts": {
                        "vector_text": "字段名：po_amt。所属表：purchase_order。该字段表示含税的采购金额。"
                    },
                },
                "po_date": {
                    "description": "采购下单日期。",
                    "aliases": ["采购日期", "下单日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间统计采购。",
                },
                "po_status": {
                    "description": "采购单状态。",
                    "aliases": ["采购状态", "到货状态"],
                    "semantic_role": "filter",
                    "value_range": "ordered / partial / received",
                    "business_usage": "筛选未到货或已到货的采购单。",
                },
            },
        },
    }

def enrich_columns_with_business_meta(columns: list, business_meta: dict) -> None:
    """
    将业务元数据补充回 ColumnSchema。

    该函数用于兼容当前 Demo：即使 sqlite_loader.py 还没有支持 value_range、
    business_usage、index_texts 等字段，也可以在 Demo 中把这些信息补充到字段对象上。
    """
    for column in columns:
        table_meta = business_meta.get(column.table_name, {})
        column_meta = table_meta.get("columns", {}).get(column.column_name, {})

        if not column_meta:
            continue

        column.description = column_meta.get("description", getattr(column, "description", ""))
        column.aliases = column_meta.get("aliases", getattr(column, "aliases", []))
        column.semantic_role = column_meta.get("semantic_role", getattr(column, "semantic_role", ""))
        column.value_range = column_meta.get("value_range", getattr(column, "value_range", ""))
        column.data_distribution = column_meta.get("data_distribution", getattr(column, "data_distribution", ""))
        column.business_usage = column_meta.get("business_usage", getattr(column, "business_usage", ""))

        if "samples" in column_meta:
            column.samples = [str(item) for item in column_meta["samples"]]

        index_texts = column_meta.get("index_texts", {})
        column.vector_text = index_texts.get("vector_text", "")


def build_vector_documents(columns: list) -> List[VectorFieldDocument]:
    """
    构建字段级向量检索文档。

    Args:
        columns: 字段 Schema 列表。

    Returns:
        List[VectorFieldDocument]: 字段级向量检索文档列表。
    """
    documents: List[VectorFieldDocument] = []

    for column in columns:
        doc_id = getattr(column, "full_name", None)
        if not doc_id:
            doc_id = f"{column.database}.{column.table_name}.{column.column_name}"

        vector_text = getattr(column, "vector_text", "")

        if not vector_text:
            vector_text = "\n".join(
                part for part in [
                    f"字段名：{column.column_name}",
                    f"所属表：{column.table_name}",
                    f"所属表业务含义：{getattr(column, 'table_description', '')}",
                    f"字段含义：{getattr(column, 'description', '')}",
                    f"字段别名：{', '.join(getattr(column, 'aliases', []))}" if getattr(column, "aliases", []) else "",
                    f"业务用途：{getattr(column, 'business_usage', '')}" if getattr(column, "business_usage", "") else "",
                    f"字段角色：{getattr(column, 'semantic_role', '')}" if getattr(column, "semantic_role", "") else "",
                ]
                if part
            )

        documents.append(
            VectorFieldDocument(
                doc_id=doc_id,
                column=column,
                vector_text=vector_text,
            )
        )

    return documents


# =============================================================================
# 6. 多关键词向量召回
# =============================================================================


def search_by_extracted_keywords(
    vector_index: VectorIndex,
    documents: List[VectorFieldDocument],
    keywords: List[str],
    per_keyword_top_k: int = 5,
    final_top_k: int = 5,
) -> List[Tuple[int, float, List[str]]]:
    """
    使用抽取后的核心关键词进行多路向量召回。

    每个关键词会单独进行向量检索。多个关键词的召回结果会按照字段文档去重，
    并保留每个字段的最高相似度得分。

    Args:
        vector_index: 已构建完成的向量索引。
        documents: 字段级检索文档列表。
        keywords: 核心检索词列表。
        per_keyword_top_k: 每个关键词单独召回的候选数量。
        final_top_k: 最终返回的字段数量。

    Returns:
        List[Tuple[int, float, List[str]]]: 文档下标、最终得分、命中的关键词列表。
    """
    merged: Dict[int, Dict[str, object]] = {}

    for keyword in keywords:
        keyword_hits = vector_index.search(keyword, top_k=per_keyword_top_k)

        for doc_index, score in keyword_hits:
            if doc_index not in merged:
                merged[doc_index] = {
                    "score": score,
                    "matched_keywords": [keyword],
                }
            else:
                merged[doc_index]["score"] = max(
                    float(merged[doc_index]["score"]),
                    score,
                )

                matched_keywords = merged[doc_index]["matched_keywords"]
                assert isinstance(matched_keywords, list)

                if keyword not in matched_keywords:
                    matched_keywords.append(keyword)

    results = [
        (
            doc_index,
            float(payload["score"]),
            payload["matched_keywords"],
        )
        for doc_index, payload in merged.items()
    ]

    results.sort(key=lambda item: item[1], reverse=True)

    return results[:final_top_k]


# =============================================================================
# 7. 主流程
# =============================================================================


def main() -> None:
    db_path = create_demo_database()
    business_meta = get_business_meta()

    print("=" * 80)
    print("向量召回 Demo")
    print("=" * 80)
    print(f"Demo database: {db_path}")

    loader = SQLiteSchemaLoader(
        db_path=db_path,
        database_name="demo_erp",
        business_meta=business_meta,
        sample_size=5,
    )

    tables, columns, relations = loader.load()
    enrich_columns_with_business_meta(columns, business_meta)

    documents = build_vector_documents(columns)

    print("\n[1] Vector documents:")
    for index, doc in enumerate(documents):
        print(f"{index}. {doc.doc_id}")
        print(f"   {doc.vector_text}")

    embedding_client = AliyunEmbeddingClient(
        AliyunEmbeddingConfig(
            api_key="",
            embedding_url="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            model="text-embedding-v4",
            dimensions=1024,
        )
    )

    vector_index = VectorIndex(embedding_client)

    vector_texts = [
        doc.vector_text
        for doc in documents
    ]

    print("\n[2] Building vector index...")
    vector_index.build(vector_texts)

    if embedding_client.config.api_key:
        print("Vector index built by Aliyun Embedding API.")
    else:
        print("Vector index built by local mock embedding because DASHSCOPE_API_KEY is not set.")

    query = "查询准时率低于0.9的供应商名称"

    print("\n[3] Query:")
    print(query)

    keyword_extractor = KeywordExtractor(
        KeywordExtractorConfig(
            api_key="",
            chat_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            model="qwen-plus",
        )
    )

    keywords = keyword_extractor.extract(query)

    print("\n[4] Extracted keywords:")
    print("，".join(keywords))

    hits = search_by_extracted_keywords(
        vector_index=vector_index,
        documents=documents,
        keywords=keywords,
        per_keyword_top_k=5,
        final_top_k=5,
    )

    print("\n[5] Vector recall results by extracted keywords:")
    for rank, (doc_index, score, matched_keywords) in enumerate(hits, start=1):
        doc = documents[doc_index]
        col = doc.column

        print(
            f"{rank}. {col.table_name}.{col.column_name} "
            f"score={score:.6f} "
            f"matched_keywords={matched_keywords}"
        )
        print(f"   字段含义：{getattr(col, 'description', '')}")
        print(f"   向量文本：{doc.vector_text}")


if __name__ == "__main__":
    main()
