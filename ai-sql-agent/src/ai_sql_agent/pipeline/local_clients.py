from __future__ import annotations

import hashlib
import re
from typing import List

import numpy as np

from ai_sql_agent.core.run_context import RunCancelled


class LocalHashEmbeddingClient:
    """
    本地 Hash Embedding。

    用于无 API Key 时跑通端到端流程。
    该实现只用于 Demo，不代表真实语义召回效果。
    """

    backend = "local"

    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    @property
    def signature(self) -> str:
        return f"local_hash/{self.dimensions}"

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        批量生成文本向量。
        """
        vectors = np.zeros((len(texts), self.dimensions), dtype=np.float32)

        for row_index, text in enumerate(texts):
            for token in self._tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).hexdigest()
                col_index = int(digest, 16) % self.dimensions
                vectors[row_index, col_index] += 1.0

        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-8)

    def _tokenize(self, text: str) -> List[str]:
        """
        简单中英文混合切分。
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


class SimpleKeywordExtractor:
    """
    简单关键词抽取器。

    真实流程中可以替换为大模型关键词抽取。
    当前 Demo 为了离线可运行，使用规则抽取核心业务词。
    """

    _value_vocabulary: List[str] | None = None

    @classmethod
    def value_vocabulary(cls) -> List[str]:
        """
        枚举取值词表，从业务元数据的 samples / value_range 中提取。

        懒加载并缓存：词表只依赖 Schema 元数据，查询期不变。
        """
        if cls._value_vocabulary is not None:
            return cls._value_vocabulary

        from .demo_data import (
            get_erp_business_meta,
            get_mes_business_meta,
            get_pm_business_meta,
            get_wms_business_meta,
        )

        values: set[str] = set()
        for builder in (
            get_erp_business_meta,
            get_mes_business_meta,
            get_pm_business_meta,
            get_wms_business_meta,
        ):
            for table_meta in builder().values():
                for column_meta in table_meta.get("columns", {}).values():
                    raw_values = list(column_meta.get("samples", []) or [])
                    # value_range 形如「一车间 / 二车间」，同样是声明过的取值域
                    value_range = column_meta.get("value_range", "")
                    if value_range and "/" in value_range:
                        raw_values.extend(value_range.split("/"))

                    for sample in raw_values:
                        text = str(sample).strip()
                        # 只收中文枚举值：数字、日期、编码不适合做关键词
                        if 2 <= len(text) <= 6 and all(
                            "\u4e00" <= ch <= "\u9fff" for ch in text
                        ):
                            values.add(text)

        # 长值优先，避免「一车间」被「车间」抢先匹配。等长的按字面排：只按长度排，
        # 「一车间」「二车间」谁在前就看集合的遍历顺序，而它随 PYTHONHASHSEED 变——
        # 同一个问题抽出的关键词顺序会因进程而异，检索结果跟着变。
        cls._value_vocabulary = sorted(values, key=lambda value: (-len(value), value))
        return cls._value_vocabulary

    @staticmethod
    def phrase_rules() -> List[str]:
        """业务短语表：字段别名与常用指标说法。"""
        return [
            # 项目与工期
            "实际成本", "预算金额", "预算", "超预算", "合同额", "合同金额",
            "项目名称", "项目", "变更总金额", "变更金额", "变更单", "变更",
            "里程碑", "延期",
            "供应商名称", "客户名称", "工单号", "采购单号", "订单号", "批次号",
            "验收结果", "验收", "业务板块", "板块",
            # 订单与采购
            "采购金额", "采购额", "采购成本", "采购单", "采购",
            "订单金额", "签单金额", "订单", "客户",
            "供应商", "准时率", "准时交付率", "品类",
            "已付金额", "付款金额", "付款状态", "付款",
            "工程号", "项目号",
            # 生产与工时
            "当前工序", "工序", "生产进度", "排产日期", "排产",
            "完工日期", "完工", "车间", "工单", "工时", "产量", "产能",
            "良率", "合格率",
            # 库存与物料
            "安全库存", "在库数量", "库存数量", "库存",
            "物料名称", "材料名称", "物料", "材料",
            "批次", "单位成本", "成本单价",
            "入库数量", "入库总量", "入库量", "入库",
            "物料编码", "项目编号", "项目总数",
        ]

    def extract(self, query: str) -> List[str]:
        """
        从用户 Query 中抽取关键词。

        两路合并：
        1. **短语表命中**——业务指标、字段别名这类固定说法，精确可控；
        2. **取值词兜底**——短语表覆盖不到的枚举取值（如「板材类供应商」里的「板材」），
           从元数据声明过的取值里匹配出来，见 `value_vocabulary()`。

        没有第 2 路时，用户以字段**取值**提问会完全召不回——取值不在字段名也不在
        别名里，只出现在样例值中，短语表不可能穷举。
        """
        keywords: List[str] = []

        for phrase in self.phrase_rules():
            if phrase in query and phrase not in keywords:
                keywords.append(phrase)

        # 取值词兜底：用户经常以字段的**取值**提问。
        #
        # 「板材类供应商」「一车间的工单」里，「板材」「一车间」是 category /
        # workshop 的取值，既不是字段名也不是别名，短语表穷举不了。
        # 这里不做盲目 n-gram 切分——那会切出「询良」「率低」这类无意义碎片，
        # 既污染召回又让每个关键词都要跑一遍双路检索，实测单次查询从
        # 几十毫秒涨到 7 秒。改为只匹配**元数据里声明过的枚举取值**，
        # 精确、无噪声、代价只是一次子串扫描。
        for value in self.value_vocabulary():
            if value in query and value not in keywords:
                keywords.append(value)

        if not keywords:
            keywords.append(query)

        return keywords


def build_schema_glossary(business_metas: dict, max_chars: int = 6000) -> str:
    """
    把业务元数据压成一份紧凑的术语表，供模型抽词时做口语 → Schema 术语的映射。

    为什么需要它：抽词器原本要求「保留用户原始表达，不做语义改写」。
    这条规则对标准业务说法是对的，但对口语说法是致命的——「料快没了」
    原样保留后，BM25 匹配不上 safety_stock，向量侧走的是本地哈希嵌入
    （没有语义能力），两条路都召不回来。实测留出集召回率因此只有 66.7%。

    做法上刻意选了「喂业务元数据」而不是「往短语表里加口语词」：
    前者对任何没见过的说法都成立，后者只对补进去的那几条成立，
    而且会把留出集变成第二个训练集。
    """
    lines = []
    for database, tables in (business_metas or {}).items():
        for table_name, table_meta in (tables or {}).items():
            columns = []
            for column_name, column_meta in (table_meta.get("columns") or {}).items():
                description = (column_meta.get("description") or "").strip().rstrip("。")
                label = description[:18] or column_name
                # 别名里已经被描述包含的就不再重复，术语表越紧凑模型越容易读
                aliases = [
                    alias
                    for alias in (column_meta.get("aliases") or [])
                    if alias and alias not in label
                ][:2]
                if aliases:
                    label = f"{label}/{'/'.join(aliases)}"
                columns.append(f"{column_name}({label})")
            table_desc = (table_meta.get("description") or "").strip().rstrip("。")[:20]
            lines.append(
                f"{database}.{table_name}（{table_desc}）: " + "，".join(columns)
            )

    glossary = "\n".join(lines)
    return glossary[:max_chars]


class HybridKeywordExtractor:
    """
    关键词抽取：有模型凭证时用模型，否则用规则；模型失败自动回退规则。

    为什么要有两条路：规则抽取器依赖预置短语表，对**没见过的口语说法**
    命中率很低——实测「看下花超了的工程有哪些」这类问法，短语表一个都
    匹配不到，退化成把整句当关键词，检索基本失效。模型抽取没有这个限制。

    但规则路不能删：离线开发、模型不可用降级时都要靠它，而且它对
    标准业务说法足够准，还不花钱。
    """

    def __init__(self, model_extractor=None, rule_extractor=None, glossary: str = ""):
        self.rule_extractor = rule_extractor or SimpleKeywordExtractor()
        self._model_extractor = model_extractor
        self._model_failed = False
        self.glossary = glossary
        """业务术语表。给模型做口语 → Schema 术语映射用；离线规则路用不到。"""

    @property
    def model_extractor(self):
        """懒加载模型抽取器：没有凭证时保持 None，不抛异常。"""
        if self._model_extractor is None and not self._model_failed:
            try:
                from ai_sql_agent.core import get_settings

                if not get_settings().has_model_credentials:
                    self._model_failed = True
                    return None
                from ai_sql_agent.retrieval.keyword_extractor_client import (
                    AliyunKeywordExtractor,
                )

                self._model_extractor = AliyunKeywordExtractor()
            except Exception:
                self._model_failed = True
        return self._model_extractor

    def reset_model(self) -> None:
        """
        重新允许模型抽词。

        模型调用失败一次之后，这里会一直用规则抽词直到进程重启（见 extract）。
        管理员确认模型侧恢复、复位熔断器时调它，免得检索停在降级状态。
        只清「失败过」这个标记：没有凭证时下一次照样不会去建模型抽词器。
        """
        self._model_failed = False

    def extract(self, query: str) -> List[str]:
        extractor = self.model_extractor
        if extractor is not None:
            try:
                keywords = extractor.extract(query, glossary=self.glossary)
                if keywords:
                    # 模型结果与规则结果合并：模型负责理解口语，
                    # 规则补上元数据里声明过的枚举取值（模型未必会提取到）。
                    merged = list(keywords)
                    for extra in self.rule_extractor.extract(query):
                        if extra not in merged and extra != query:
                            merged.append(extra)
                    return merged
            except RunCancelled:
                # 这次请求被取消了，不是模型坏了：不能因此在本进程里永久关掉模型抽词
                raise
            except Exception:
                # 模型不可用时静默回退，不能因为抽词失败让整条查询挂掉
                self._model_failed = True
                self._model_extractor = None

        return self.rule_extractor.extract(query)
