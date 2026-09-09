"""
稀疏检索（关键词侧）的分词与 BM25 打分。

为什么需要它：纯稠密向量检索对**领域专名和精确 token 是系统性弱项**。
"铰链"、"补件"、"定金"这类词在通用嵌入模型里区分度不高，订单号
（SO-2024-0117）这种字符串更是几乎没有语义可言——用户原样打出来的
关键词，向量检索反而召不回。稀疏检索恰好相反：它只认字面命中。
两路互补，融合（见 knowledge_base.rrf_fuse）后召回率显著高于任一单路。

分词策略（不引入 jieba 等外部分词器）：
  - ASCII 字母/数字连续串 → 整段小写成一个词（"SO-2024-0117" → so / 2024 / 0117）；
  - 中日韩连续串 → **字符二元组**（"补件流程" → 补件 / 件流 / 流程）。

二元组而不是单字：单字（补/件/流/程）在中文里歧义太大，"补" 会命中
"补充说明""补差价"；二元组保留了局部词序，无需词典就能逼近词级匹配，
是中文全文检索里的成熟做法。代价是索引膨胀约一倍，在这个语料量级无所谓。

同一套 tokenize 同时用于**写入时建索引**和**检索时构造查询**，两侧必须
完全一致——分词器一旦不对称，索引里的 token 和查询的 token 对不上，
稀疏这一路就会静默地永远召回为空。所以它被收敛在这一个函数里。
"""
from __future__ import annotations

import math
from collections import Counter

# 中日韩统一表意文字（含扩展 A）+ 假名。够覆盖中文业务语料。
_CJK_RANGES = (
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0x3400, 0x4DBF),   # 扩展 A
    (0x4E00, 0x9FFF),   # 基本区
    (0xF900, 0xFAFF),   # 兼容表意文字
)

# 单条查询参与稀疏检索的 token 上限。超长问句（用户粘贴一整段投诉）
# 会产生上百个二元组，tsquery 会变得很慢且几乎命中全库，反而稀释排序。
MAX_QUERY_TOKENS = 64


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> list[str]:
    """
    文本 → token 列表（保序，可含重复；BM25 需要词频）。

    >>> tokenize("补件流程 SO-2024-0117")
    ['补件', '件流', '流程', 'so', '2024', '0117']
    """
    if not text:
        return []

    tokens: list[str] = []
    ascii_buf: list[str] = []
    cjk_buf: list[str] = []

    def flush_ascii() -> None:
        if ascii_buf:
            tokens.append("".join(ascii_buf).lower())
            ascii_buf.clear()

    def flush_cjk() -> None:
        if not cjk_buf:
            return
        if len(cjk_buf) == 1:
            tokens.append(cjk_buf[0])
        else:
            tokens.extend(cjk_buf[i] + cjk_buf[i + 1] for i in range(len(cjk_buf) - 1))
        cjk_buf.clear()

    for ch in text:
        if ch.isalnum() and ch.isascii():
            flush_cjk()
            ascii_buf.append(ch)
        elif _is_cjk(ch):
            flush_ascii()
            cjk_buf.append(ch)
        else:
            flush_ascii()
            flush_cjk()

    flush_ascii()
    flush_cjk()
    return tokens


def index_text(title: str, content: str) -> str:
    """
    片段 → 供存储层建立稀疏索引的 token 串（空格分隔）。

    标题一并进索引：标题往往是最凝练的关键词（"缺件漏发与补件流程"），
    正文里反而未必原样出现。
    """
    return " ".join(tokenize(f"{title} {content}"))


def query_tokens(query: str) -> list[str]:
    """查询 → 去重后的 token 列表，截断到 MAX_QUERY_TOKENS。"""
    return list(dict.fromkeys(tokenize(query)))[:MAX_QUERY_TOKENS]


def bm25_scores(
    q_tokens: list[str],
    docs: list[list[str]],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """
    对一组文档按 BM25 打分，返回与 docs 等长的分数列表（未命中为 0）。

    只在 SQLite 兜底后端用——它本来就要全表扫描，顺手算 BM25 的边际成本
    可以忽略。Postgres 侧走 tsvector 的 GIN 索引 + ts_rank_cd，不走这里。

    idf 用带平滑的 Robertson 公式并夹住下界：语料只有几十个片段时，
    出现在半数以上文档里的词会算出负 idf，让"命中得越多分越低"，
    在小语料上这个反常比停用词本身危害更大。
    """
    n = len(docs)
    if not q_tokens or n == 0:
        return [0.0] * n

    doc_counters = [Counter(d) for d in docs]
    doc_lens = [len(d) for d in docs]
    avg_len = sum(doc_lens) / n or 1.0

    scores = [0.0] * n
    for term in set(q_tokens):
        df = sum(1 for c in doc_counters if term in c)
        if df == 0:
            continue
        idf = max(math.log(1 + (n - df + 0.5) / (df + 0.5)), 1e-6)
        for i, counter in enumerate(doc_counters):
            tf = counter.get(term, 0)
            if not tf:
                continue
            norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_lens[i] / avg_len))
            scores[i] += idf * norm
    return scores
