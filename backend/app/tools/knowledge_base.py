"""
RAG 知识库（pgvector / SQLite 双后端）的混合检索。

检索链路：文档切片 → Embedder 生成向量 + 关键词 token → 存储后端 →
**稠密与稀疏两路并行召回 → RRF 融合 → Top-K**。本模块只负责业务逻辑：
切片、内容哈希去重、双路融合、结果格式化、默认文档种子，索引的存与查
全部面向 storage/vector_store.py 的接口，后端可替换。

为什么是混合检索而不是纯向量：单路稠密检索在这个业务里有一类稳定的
盲区——用户原样打出来的领域专名和精确串（"铰链"、"补件"、订单号）
恰恰是嵌入模型区分度最低的部分。补一路 BM25/tsvector 关键词检索，
两路互补。关键是这一路**不需要任何 LLM 调用**，所以规则快通道也能
用上（它的零网络往返承诺不受影响），这与"多角度改写"那种要花一次
生成调用才能换来的召回提升有本质区别。

为什么用 RRF 而不是加权求和分数：余弦相似度和 BM25 分数量纲完全不同
（前者 0-1 有界，后者无上界且随语料规模漂移），线性加权需要为每个语料
重新标定权重，标完还会随文档增长失效。RRF 只用**名次**，不碰原始分数，
因此无需标定——这是它成为工业界默认融合方式的原因。

RRF 只在稠密一路用的是真正的 Embedding 模型时才用。没配远端 Embedding 时，
稠密一路是本地字符 n-gram 向量，它不是一个独立的判断，融合改成"关键词在前、
向量补位"——原因和实测见 lexical_first_fuse。

文档与分片。检索的单位是分片，人读的单位是文档：导入时把标题、原文、
来源和时间记进文档表，分片用 doc_id 指回去，检索结果因此能说出"出自哪篇"。

去重策略：文档按 sha256(标题 + 正文) 判重。同一份文档重复上传（运营
误操作、定时同步任务重跑）命中同一个哈希，直接跳过——导入幂等。
**标题相同、正文不同是两篇文档**，各自保留：只看标题就合并，会把
"2025 版保修政策"和"2026 版保修政策"拼成一篇谁也没写过的东西。

文档 ID 就取自那个哈希，分片 ID 再由文档 ID + 序号 + 内容得出——全都是内容
算出来的，没有随机数。所以两个实例同时导同一篇，写的是同一批行，不会进去两份。
分片 ID 同时是 RRF 融合的合并键：两路召回同一片段时按 ID 归并。

同名文档的两个版本往往共用大段原文。分片各归各的文档，但**检索结果里同一段
文字只出现一次**（按标题 + 片段内容去重，见 search_async）——不然两个版本
各出一份，会白占掉回答上下文里的名额。

历史分片。加文档表之前写入的分片没有 doc_id，也没有原文——长文档切片时
换行被换成了句号，拼回去的不是原来那份。ensure_documents 把它们归档成
"历史"文档并照实标注：能确定是同一次导入的整篇拼回去（origin=legacy），
缺片的标成不完整（legacy_partial），分不清归属的每片单独成篇。归不出来的
东西不硬凑——来源、版本、发布日期这些当时没记下来的，现在也不补。
"""
import asyncio
import hashlib
import logging
import re
import time
from typing import Any

from app.core.embedder import EmbeddingUnavailable
from app.core.lexical import index_text, query_tokens
from app.core.telemetry import span

logger = logging.getLogger(__name__)

# 文档原文的可信程度（kb_documents.origin）
ORIGIN_ORIGINAL = "original"              # 导入时存下的原文
ORIGIN_LEGACY = "legacy"                  # 历史分片齐全，正文由分片拼回
ORIGIN_LEGACY_PARTIAL = "legacy_partial"  # 历史分片不全，正文只有找得到的那几片

# 文档是怎么进来的（kb_documents.source_type）
SOURCE_MANUAL = "manual"   # 在工作台里粘贴导入
SOURCE_UPLOAD = "upload"   # 上传文件
SOURCE_SEED = "seed"       # 空库时导入的内置示例
SOURCE_LEGACY = "legacy"   # 加文档表之前导入的，来源当时没记

# 文档的大类（kb_documents.category），按客户从咨询到售后的顺序排，工作台也按这个
# 顺序列。分类只用来整理和浏览，检索不看它：改分类不碰分片、向量和关键词索引。
# 库里存的是 key——改名只改 name / name_en，key 不能动。空串 = 未分类。
CATEGORIES: tuple[dict[str, str], ...] = (
    {"key": "products", "name": "产品与材料", "name_en": "Products & materials"},
    {"key": "design", "name": "咨询与设计", "name_en": "Consultation & design"},
    {"key": "orders", "name": "报价与订单", "name_en": "Quotes & orders"},
    {"key": "production", "name": "生产与交期", "name_en": "Production & lead times"},
    {"key": "delivery", "name": "送货与收货", "name_en": "Delivery & receiving"},
    {"key": "installation", "name": "安装与验收", "name_en": "Installation & sign-off"},
    {"key": "aftersales", "name": "售后与保修", "name_en": "After-sales & warranty"},
)
CATEGORY_KEYS = tuple(c["key"] for c in CATEGORIES)
# 按"未分类"筛选时用的保留字。它不会存进库里：未分类在库里就是空串。
UNCATEGORIZED = "uncategorized"
_UNCATEGORIZED_ALIASES = {UNCATEGORIZED, "未分类", "none"}


class UnknownCategory(ValueError):
    """导入或改分类时给了一个不认识的分类。"""

    def __init__(self, value: str, title: str = ""):
        self.value = value
        self.title = title
        names = "、".join(c["name"] for c in CATEGORIES)
        where = f"《{title}》的" if title else ""
        super().__init__(
            f"{where}分类「{value}」不认识。可用的分类：{names}，"
            f"也可以写 key（{', '.join(CATEGORY_KEYS)}）；留空是未分类。"
        )


def normalize_category(value: Any) -> str:
    """
    导入文件和请求里写的分类 → 库里存的 key。

    认 key（delivery）、中文名（送货与收货）和英文名（Delivery & receiving，不分大小写）——
    手写 JSON 的人多半写中文名。空值和"未分类"都是空串。不认识的抛 UnknownCategory，
    不悄悄当成未分类：拼错一个字，整批文档就全进了未分类，还没人知道为什么。
    """
    text = str(value or "").strip()
    if not text or text.lower() in _UNCATEGORIZED_ALIASES:
        return ""
    lowered = text.lower()
    for c in CATEGORIES:
        if lowered in (c["key"], c["name_en"].lower()) or text == c["name"]:
            return c["key"]
    raise UnknownCategory(text)


def stored_category(value: Any) -> str:
    """库里存的分类 → 接口里给的。已经撤掉的旧分类按未分类给，不让文档挂在一个看不见的类下。"""
    return value if value in CATEGORY_KEYS else ""

CHUNK_SIZE = 500

# 正文第一行多长以内才当标题用
MAX_HEADING_CHARS = 120

# 没有分片的文档记录多久之后才算空壳。导入是先登记文档再写分片，
# 中间隔着几毫秒；另一个实例恰好在这时启动，不能把正在导的那篇清掉。
EMPTY_DOC_GRACE_S = 600.0

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
# 摘要里去掉的 Markdown 记号：行首的 # 和 >，以及强调号、反引号、表格竖线。
# **下划线不动**——它在这类资料里几乎都是字段名的一部分（on_hand_qty、
# contract_amt），当成强调号剥掉会把字段名改成另一个词。
_MD_LINE_PREFIX_RE = re.compile(r"^\s{0,3}(?:#{1,6}|>+)\s*")
_MD_NOISE_RE = re.compile(r"[*`|]+")


def document_hash(title: str, content: str) -> str:
    """文档判重用的哈希。首尾空白和换行符风格不算差异。"""
    body = (content or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(f"{(title or '').strip()}\n{body}".encode()).hexdigest()


def split_heading(content: str) -> tuple[str, str]:
    """
    正文开头如果是 Markdown 标题行，拆成 (标题, 其余正文)；不是就返回 ("", 原文)。

    历史分片有一种特殊形状：超过一片长度的文档在切片时换行被换成了句号，
    标题和正文于是压在同一行里（`# 标题。第一句。第二句…`）。这时以第一个
    句号为界——那正是原来换行的位置。太长的"标题"不认：那多半是一整段
    恰好以 # 开头的正文，拿它当标题会把整篇文档显示成一个标题。
    """
    text = (content or "").lstrip()
    first, _, rest = text.partition("\n")
    match = _HEADING_RE.match(first)
    if not match:
        return "", content or ""
    heading = match.group(1).strip()
    if "。" in heading:
        heading, _, tail = heading.partition("。")
        rest = f"{tail}\n{rest}" if rest else tail
    heading = heading.strip()
    if not heading or len(heading) > MAX_HEADING_CHARS:
        return "", content or ""
    return heading, rest.lstrip("\n")


def document_heading(content: str) -> str:
    """正文第一行如果是 Markdown 标题，返回标题文字；否则返回空串。"""
    return split_heading(content)[0]


def plain_text(content: str) -> str:
    """正文去掉标题行和 Markdown 记号、空白折成单个空格。摘要和命中片段都从它取。"""
    lines = split_heading(content)[1].splitlines()
    cleaned = (_MD_NOISE_RE.sub(" ", _MD_LINE_PREFIX_RE.sub("", line)) for line in lines)
    return " ".join(" ".join(cleaned).split())


def document_excerpt(content: str, limit: int = 200) -> str:
    """
    列表里那两行摘要：正文开头，去掉标题行和 Markdown 记号。

    是**摘录**不是概括——不调模型，文档里没有的话这里一个字都不会多。
    """
    text = plain_text(content)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def match_snippet(content: str, terms: list[str], before: int = 50, after: int = 110) -> str:
    """
    正文里命中处附近的一小段，给筛选结果说明"为什么是这篇"。

    每个词取它第一次出现的位置，各截一段；挨得近的并成一段，最多给两段。
    只在正文里找——标题命中的由列表上的标题高亮去说明，这里返回空串。
    """
    text = plain_text(content)
    lowered = text.lower()
    hits = sorted({at for at in (lowered.find(t.lower()) for t in terms) if at >= 0})
    if not hits:
        return ""
    windows: list[list[int]] = []
    for at in hits:
        start, end = max(0, at - before), min(len(text), at + after)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    parts = [text[start:end].strip() for start, end in windows[:2]]
    return (("…" if windows[0][0] > 0 else "") + " … ".join(parts)
            + ("…" if windows[min(len(windows), 2) - 1][1] < len(text) else ""))


# RRF 的平滑常数。60 是 Cormack 原论文的取值，也是业界默认：它决定了
# 排名靠前的结果领先多少——k 越小头部越陡峭（第 1 名相对第 5 名权重差距
# 越大），k 越大越接近"两路都命中就加分"的均匀投票。60 在 Top-10 这个
# 量级上的表现是：单路第 1 名压不过另一路第 1 + 第 3 的组合，
# 也就是"两路都认可"胜过"一路极度认可"，这正是融合想要的行为。
RRF_K = 60


def should_use_knowledge(message: str, intent=None) -> bool:
    """
    是否该为这条消息检索知识库。

    跳过纯寒暄，业务类问题才检索，避免无关 RAG 干扰回复。
    放在这里而不是 api 层：/chat 主链路和评测 harness 都要用同一套门控，
    否则评测出来的分数和线上不是一回事。
    """
    msg = (message or "").strip().lower()
    if not msg:
        return False
    intent_value = getattr(intent, "value", intent)
    if intent_value in {"greeting", "feedback", "escalation", "human_handoff", "other"}:
        return False
    if intent_value in {
        "query", "request", "technical", "billing", "order", "complaint",
        "order_status", "delivery", "order_change", "missing_part",
        "quote_pricing", "payment_issue", "installation", "damage_defect",
    }:
        return True
    greetings = {"你好", "您好", "嗨", "hi", "hello", "hey", "早上好", "晚上好"}
    if msg in greetings:
        return False
    business_keywords = [
        "订单", "工单", "报价", "价格", "交期", "排产", "送货", "补件", "缺件",
        "改单", "尺寸", "门板", "台面", "柜体", "饰面", "五金", "铰链", "安装",
        "划痕", "破损", "定金", "尾款", "发票", "账单",
        "order", "quote", "delivery", "install", "invoice", "payment",
    ]
    return len(msg) >= 4 or any(kw in msg for kw in business_keywords)


def _row_key(row: dict[str, Any]) -> str:
    """两路结果按它归并：片段 id，没有就退回标题 + 序号。"""
    return row.get("id") or f"{row.get('title', '')}:{row.get('chunk_index', 0)}"


def rrf_fuse(
    rankings: dict[str, list[dict[str, Any]]],
    top_k: int,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """
    Reciprocal Rank Fusion：把多路召回结果按名次融合成一个列表。

    每条结果的融合分 = Σ 1/(k + 该路里的名次)，名次从 1 起算。
    两路都召回的片段会拿到两份加分，因此自然排到只被单路召回的片段前面。

    rankings: {路名: 该路的召回结果（已按各自相关性排序）}，按片段 id 归并。
    返回的每条结果带 rrf_score 和 retrieval（hybrid 表示两路都命中）。
    """
    fused: dict[str, dict[str, Any]] = {}
    for source, rows in rankings.items():
        for rank, row in enumerate(rows, start=1):
            key = _row_key(row)
            entry = fused.get(key)
            if entry is None:
                entry = {**row, "rrf_score": 0.0, "sources": []}
                fused[key] = entry
            # 同一片段两路都返回时，保留有效的余弦分（稀疏路也会带，但
            # 兜底一下：某一路给了 0 分不该覆盖另一路的真实分数）
            if not entry.get("score") and row.get("score"):
                entry["score"] = row["score"]
            entry["rrf_score"] += 1.0 / (k + rank)
            entry["sources"].append(source)

    for entry in fused.values():
        entry["retrieval"] = "hybrid" if len(set(entry["sources"])) > 1 else entry["sources"][0]
        entry.pop("sources", None)

    ordered = sorted(fused.values(), key=lambda e: e["rrf_score"], reverse=True)
    return ordered[:top_k]


def lexical_first_fuse(
    dense: list[dict[str, Any]], lexical: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    关键词一路的结果按原顺序排在前面，只被稠密一路召回的排在后面补位。

    给**本地 n-gram 向量**用的。RRF 的前提是两路各自独立：两份名单里都出现的片段，
    会排到只在一份名单里拿第一的前面，因为"两个互不相干的判断都认可"比
    "一个判断极度认可"更可信。本地 n-gram 向量不满足这个前提——它量的也是字符
    重合，和关键词一路是同一种信号，只是噪声大得多。拿它去投票，得票最多的是
    和问题只沾一点边、因而在两路里都排得上号的片段，关键词一路的第 1 名反而
    被挤出前三。

    56 个片段、108 条用例上的实测（docs/evaluation/retrieval-ca-factory.md），
    目标文档进前 3 的条数：只用关键词 80，RRF 融合 62。所以这种向量不参与排序，
    只在关键词一路什么都没召回时（问句和文档没有共同的字面）兜个底。

    返回的每条结果带 retrieval，取值和 rrf_fuse 一致。
    """
    dense_by_key = {_row_key(row): row for row in dense}
    lexical_keys = {_row_key(row) for row in lexical}

    fused: list[dict[str, Any]] = []
    for row in lexical:
        twin = dense_by_key.get(_row_key(row))
        entry = {**row, "retrieval": "hybrid" if twin else "lexical"}
        if not entry.get("score") and twin and twin.get("score"):
            entry["score"] = twin["score"]
        fused.append(entry)
    fused.extend(
        {**row, "retrieval": "dense"} for row in dense if _row_key(row) not in lexical_keys
    )
    return fused


def fuse_candidates(
    dense: list[dict[str, Any]], lexical: list[dict[str, Any]], semantic: bool = True
) -> list[dict[str, Any]]:
    """
    两路召回的候选 → 一份去重后的排序。

    semantic 说的是稠密一路的向量有没有语义（Embedder.semantic）：有就用 RRF，
    没有就用 lexical_first_fuse。KnowledgeBase.search_async 和
    scripts/eval_retrieval.py 都走这里，评测量的就是线上这条路。

    同一段文字只留排名最靠前的那一条。同名文档的不同版本会各自带着相同的段落，
    不去掉的话前 K 条里会有几条一模一样的内容。先融合全部候选再去重，调用方
    最后才截断：反过来做，被去掉的名额就补不回来了。
    """
    if semantic:
        fused = rrf_fuse({"dense": dense, "lexical": lexical}, top_k=len(dense) + len(lexical))
    else:
        fused = lexical_first_fuse(dense, lexical)

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in fused:
        key = (row["title"], row["content"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


class KnowledgeBase:
    """面向存储接口的知识库。所有公开方法均为 async。"""

    def __init__(self, store, embedder):
        self._store = store
        self._embedder = embedder

    @property
    def backend(self) -> str:
        """当前存储后端名，供 /health 排障。"""
        return self._store.backend

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    async def add_documents_async(
        self,
        documents: list[dict[str, str]],
        source_type: str = SOURCE_MANUAL,
        source_name: str = "",
        category: str = "",
    ) -> int:
        """
        批量导入文档，按内容去重，返回新增片段数。

        documents 格式: [{"title": "...", "content": "...", "category": "..."}, ...]
        （category 可省）。长文档自动切片（每片 500 字）。需要知道每篇各自落成了
        什么，用 import_documents。
        """
        results = await self.import_documents(
            documents, source_type=source_type, source_name=source_name, category=category
        )
        return sum(r["chunks"] for r in results if r["status"] == "created")

    async def import_documents(
        self,
        documents: list[dict[str, str]],
        source_type: str = SOURCE_MANUAL,
        source_name: str = "",
        category: str = "",
    ) -> list[dict[str, Any]]:
        """
        批量导入文档，逐篇返回结果 [{id, title, status, chunks, category}]。

        status:
          created    新文档，已切片入库；
          duplicate  标题和正文都与已有文档相同，没有重复写入，id 指向已有的那篇；
          restored   与一篇历史文档逐片相同——把这份原文补给它，不另立一篇；
          empty      正文为空，跳过（id 为 None）。

        分类：每篇自己写了 category 用它的，没写的用参数 category（导入框里选的那个）。
        只有新建的文档会落到这个分类里；已经在库里的那篇（duplicate / restored）分类
        不动——它可能是有人特意归过的，重新导入一遍不该把它挪走。结果里的 category
        是那篇文档现在实际所在的分类。任何一篇的分类写错了（UnknownCategory），
        整批一篇都不导：先校验，再动库。
        """
        default_category = normalize_category(category)
        wanted: list[str] = []
        for doc in documents:
            try:
                wanted.append(normalize_category(doc["category"]) if doc.get("category")
                              else default_category)
            except UnknownCategory as ex:
                raise UnknownCategory(ex.value, str(doc.get("title") or "").strip()) from None

        # 历史分片先归档：判重看的是文档表，升级后第一次导入时表里还是空的，
        # 不先归档的话整个老知识库会被原样再导一遍。
        await self.ensure_documents()

        results: list[dict[str, Any]] = []
        batch: dict[str, tuple[str, str]] = {}  # 本批内已处理的哈希 → (文档 id, 分类)
        for doc, doc_category in zip(documents, wanted):
            title = str(doc.get("title") or "").strip()
            content = str(doc.get("content") or "")
            chunks = self._chunk_text(content, chunk_size=CHUNK_SIZE)
            if not chunks:
                results.append({"id": None, "title": title, "status": "empty", "chunks": 0,
                                "category": ""})
                continue

            digest = document_hash(title, content)
            known_id, known_category = batch.get(digest, (None, ""))
            if known_id is None:
                known = await self._store.kb_doc_find_by_hash(digest)
                if known and not known["chunks"]:
                    # 记录还在、分片没了（有人直接在库里清过分片）。认它是"已有"
                    # 的话这篇就再也导不回来，所以清掉空壳、按新文档重新导入。
                    # 另一个实例此刻正好在导同一篇也没关系：文档和分片的 ID 都是
                    # 内容算出来的，两边写的是同一批行，见下面 doc_id 的说明。
                    await self._store.kb_doc_delete(known["id"])
                    known = None
                if known and known["origin"] == ORIGIN_LEGACY:
                    # 历史文档原样再导入了一遍：正文一字不差，说明库里存着的就是原文，
                    # 不必再挂着"由分片拼回"的标记。来源照旧不动——那次是从哪来的没记。
                    await self._store.kb_doc_update(
                        known["id"], {"content": content, "origin": ORIGIN_ORIGINAL}
                    )
                if known:
                    known_id, known_category = known["id"], stored_category(known["category"])
            if known_id is not None:
                results.append({"id": known_id, "title": title, "status": "duplicate", "chunks": 0,
                                "category": known_category})
                continue

            restored = await self._restore_legacy(title, content, chunks, digest)
            if restored is not None:
                batch[digest] = restored
                results.append({"id": restored[0], "title": title, "status": "restored", "chunks": 0,
                                "category": restored[1]})
                continue

            # 文档 ID 由标题和正文算出来，不是随机数。导入因此在并发下也是幂等的：
            # 两个实例同时导同一篇（空库启动时两边一起导默认文档就是这种情况），
            # 算出的文档 ID 和分片 ID 完全相同，后到的插入落空，库里只有一份。
            # 用随机 ID 的话，"先查有没有、再写"之间的那道缝会让同一篇进去两次。
            doc_id = f"doc_{digest[:20]}"
            now = time.time()
            # 向量先算完再落库：这是导入里唯一慢的一步，放在登记文档之前，
            # 文档记录和它的分片之间就只隔着两次写入。整篇的分片一批送去算；
            # 远端 Embedding 这时不可用会抛 EmbeddingUnavailable，这一篇什么都不写
            # ——没有向量的分片进了库，稠密那一路永远召不回它。
            epoch = getattr(self._embedder, "epoch", 0)
            embeddings = await self._embedder.embed_many(
                [self.embedding_text(title, chunk) for chunk in chunks]
            )
            rows = [
                {
                    "id": hashlib.md5(f"{doc_id}\n{i}\n{chunk}".encode()).hexdigest(),
                    "doc_id": doc_id,
                    "title": title,
                    "content": chunk,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "ingested_at": now,
                    "tokens": index_text(title, chunk),
                    "embedding": embeddings[i],
                }
                for i, chunk in enumerate(chunks)
            ]
            inserted = await self._store.kb_doc_add({
                "id": doc_id,
                "title": title,
                "content": content,
                "content_hash": digest,
                "origin": ORIGIN_ORIGINAL,
                "source_type": source_type,
                "source_name": source_name,
                "chunk_count": len(rows),
                "created_at": now,
                "category": doc_category,
            })
            try:
                # 文档记录是别人刚写的也照样写分片：ID 相同，已有的不会重复，
                # 缺的补上——两边谁先谁后，最后都是完整的一篇。
                await self._store.kb_add(rows)
                if getattr(self._embedder, "epoch", 0) != epoch:
                    # 算向量和写库之间向量模型换过：这些行带的是旧模型的向量，存储层
                    # 看不出来（同维度时不报错），留下就是永远召不回的分片
                    raise EmbeddingUnavailable("向量模型在导入过程中切换了，这篇没有写入")
            except Exception as ex:
                # 分片没写成：这篇文档检索不到，留着记录等于在列表里摆一篇假的
                if inserted:
                    await self._store.kb_doc_delete(doc_id)
                if getattr(self._embedder, "epoch", 0) != epoch and not isinstance(ex, EmbeddingUnavailable):
                    # 换维度时旧向量写不进新列，同一件事的另一种表现
                    raise EmbeddingUnavailable("向量模型在导入过程中切换了，这篇没有写入") from ex
                raise
            if not inserted:
                # 记录是并发导入的另一边写的，分类以它写进去的为准
                existing = await self._store.kb_doc_get(doc_id)
                doc_category = stored_category(existing["category"]) if existing else doc_category
            batch[digest] = (doc_id, doc_category)
            results.append({
                "id": doc_id, "title": title,
                # 记录不是这次写进去的 = 同一篇被并发导入抢先了一步，算重复
                "status": "created" if inserted else "duplicate",
                "chunks": len(rows) if inserted else 0,
                "category": doc_category,
            })

        created = [r for r in results if r["status"] == "created"]
        skipped = len(results) - len(created)
        logger.info(
            f"知识库导入 {len(created)} 篇文档 / {sum(r['chunks'] for r in created)} 个片段"
            + (f"，跳过 {skipped} 篇（重复或空）" if skipped else "")
        )
        return results

    async def _restore_legacy(
        self, title: str, content: str, chunks: list[str], digest: str
    ) -> tuple[str, str] | None:
        """
        这次导入的如果正是某篇历史文档，把原文补给它，返回它的 (id, 分类)。

        长文档的历史分片拼不回原文（切片时换行变成了句号），所以它的哈希和
        重新导入的这份对不上，按文档判重会漏过去，老文档就在库里出现两次。
        这里按老办法再认一遍：分片 ID 曾经是 md5(标题 + 片段)，同一份文档切出来
        的 ID 必然逐片相同。全部对上、都归在同一篇历史文档名下、片数也一致，
        才认定是同一篇——差一片都不算，那是另一个版本。
        """
        old_ids = [hashlib.md5(f"{title}\n{chunk}".encode()).hexdigest() for chunk in chunks]
        owners = await self._store.kb_chunk_owners(old_ids)
        if len(owners) != len(set(old_ids)) or len(set(owners.values())) != 1:
            return None
        doc = await self._store.kb_doc_get(next(iter(owners.values())))
        if doc is None or doc["origin"] != ORIGIN_LEGACY or doc["chunks"] != len(set(old_ids)):
            return None
        # 只补原文。来源和导入时间不动：那次导入是从哪来的当时没记，
        # 不能拿这一次的文件名去顶替。
        await self._store.kb_doc_update(doc["id"], {
            "content": content, "content_hash": digest, "origin": ORIGIN_ORIGINAL,
        })
        logger.info(f"历史文档 {doc['id']}（{title!r}）已由重新导入的原文补全")
        return doc["id"], stored_category(doc["category"])

    async def ensure_documents(self) -> int:
        """
        给还没有文档归属的历史分片补建文档记录，返回新建的文档数。幂等。

        分组依据是 (标题, 导入时间, 总片数)：同一次导入的同一篇文档，这三样
        必然相同，而导入时间精确到微秒，两次导入撞上的可能可以忽略。
        **不按标题单独分组**——同名不同文的两篇会被拼成一篇。组内：
          - 序号 0..n-1 各出现一次 → 一篇完整的历史文档，正文按序拼回；
          - 序号有缺 → 一篇不完整的历史文档，只有找得到的那几片；
          - 序号有重复 → 同一批里有同名文档，分不清哪片跟哪片，每片单独成篇。

        文档 ID 由分组内容算出来，不是随机数：两个实例同时启动会各算一遍，
        得到同一个 ID，后到的那次插入落空，不会归出两套。顺序是先给分片
        标上 doc_id 再登记文档——反过来的话，文档会有一瞬间名下没有分片，
        正好被另一个实例当成空壳清掉。
        """
        rows = await self._store.kb_chunks_unfiled()
        if not rows:
            return 0

        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in rows:
            key = (row["title"], row["ingested_at"], row["total_chunks"])
            groups.setdefault(key, []).append(row)

        created = 0
        for (title, ingested_at, total), members in groups.items():
            indexes = [m["chunk_index"] for m in members]
            if len(set(indexes)) != len(indexes):
                bundles = [(ORIGIN_LEGACY if total == 1 else ORIGIN_LEGACY_PARTIAL, [m])
                           for m in members]
            elif sorted(indexes) == list(range(total)):
                bundles = [(ORIGIN_LEGACY, members)]
            else:
                bundles = [(ORIGIN_LEGACY_PARTIAL, members)]

            for origin, chunks in bundles:
                chunks = sorted(chunks, key=lambda c: (c["chunk_index"], c["id"]))
                content = "\n\n".join(c["content"] for c in chunks)
                seed = "\x1f".join([title, repr(ingested_at), str(total), chunks[0]["id"]])
                doc_id = f"legacy_{hashlib.md5(seed.encode()).hexdigest()[:16]}"
                await self._store.kb_assign_doc([c["id"] for c in chunks], doc_id)
                if await self._store.kb_doc_add({
                    "id": doc_id,
                    "title": title,
                    "content": content,
                    "content_hash": document_hash(title, content),
                    "origin": origin,
                    "source_type": SOURCE_LEGACY,
                    "source_name": "",
                    "chunk_count": total,
                    "created_at": ingested_at,
                }):
                    created += 1

        if created:
            logger.info(f"已把 {len(rows)} 个历史分片归档成 {created} 篇文档")
        return created

    async def prune_empty_documents(self) -> int:
        """
        清掉名下没有任何分片的文档记录，返回清掉的条数。

        分片是检索的全部依据：一篇没有分片的文档答不了任何问题，留在列表里
        只会让人以为它在起作用。出现这种记录通常是有人直接在库里删了分片
        （按数据集版本整体替换知识库的脚本就是这么做的）。刚登记的不动，
        见 EMPTY_DOC_GRACE_S。
        """
        stale = await self._store.kb_docs_without_chunks(time.time() - EMPTY_DOC_GRACE_S)
        for doc_id in stale:
            await self._store.kb_doc_delete(doc_id)
        if stale:
            logger.warning(f"清掉 {len(stale)} 条没有分片的文档记录: {', '.join(stale[:5])}"
                           + ("…" if len(stale) > 5 else ""))
        return len(stale)

    # ── 文档浏览 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _present(row: dict[str, Any]) -> dict[str, Any]:
        """存储层的一行 → 接口里的文档摘要。display_title 优先用正文自己的标题行。"""
        head = row.get("head") or row.get("content") or ""
        return {
            "id": row["id"],
            "title": row["title"],
            "display_title": document_heading(head) or row["title"],
            "excerpt": document_excerpt(head),
            "origin": row["origin"],
            "source_type": row["source_type"],
            "source_name": row["source_name"],
            "chunk_count": row["chunk_count"],
            "chunks": row.get("chunks", row["chunk_count"]),
            "chars": row.get("chars", len(head)),
            "created_at": row["created_at"],
            "category": stored_category(row.get("category", "")),
        }

    @staticmethod
    def _category_facets(counts: dict[str, int]) -> tuple[list[dict[str, Any]], int]:
        """按分类的篇数 → (每个大类一项，按 CATEGORIES 的顺序、没有文档的也列出, 未分类篇数)。"""
        facets = [{**c, "documents": counts.get(c["key"], 0)} for c in CATEGORIES]
        uncategorized = sum(n for key, n in counts.items() if key not in CATEGORY_KEYS)
        return facets, uncategorized

    async def list_documents(
        self, query: str = "", limit: int = 100, offset: int = 0, category: str | None = None,
    ) -> dict[str, Any]:
        """
        文档列表。query 为空是浏览全部（新导入的在前）；非空时按空白拆成词，
        每个词都要在标题或正文里出现，并说明是标题命中还是正文命中。

        category 为空是不分类别；给了就只列这一类，UNCATEGORIZED 是未分类。
        不认识的分类不报错，按"这一类里没有文档"给——地址栏里留着一个已经撤掉的
        分类时，页面照常打开，告诉人这个分类不存在，而不是整页报错。

        `categories` 是每个大类里满足筛选词的篇数，不受 category 影响——工作台
        拿它画分类目录：选中了"送货与收货"，其它分类各有几篇匹配也照样看得见。
        """
        terms = [t for t in (query or "").split() if t][:8]
        selected = (category or "").strip()
        filters: dict[str, list[str]] = {}
        applied: str | None = None
        if selected and selected.lower() in _UNCATEGORIZED_ALIASES:
            filters["category_not_in"] = list(CATEGORY_KEYS)
            applied = UNCATEGORIZED
        elif selected:
            try:
                key = normalize_category(selected)
            except UnknownCategory:
                key = ""
            filters["category_in"] = [key] if key else []
            applied = key or selected

        rows, matched = await self._store.kb_doc_list(
            terms, limit=limit, offset=offset, with_content=bool(terms), **filters
        )
        documents = []
        for row in rows:
            item = self._present(row)
            if terms:
                title_text = f"{row['title']} {item['display_title']}".lower()
                item["match"] = {
                    "title": all(t.lower() in title_text for t in terms),
                    "snippet": match_snippet(row.get("content", ""), terms),
                }
            documents.append(item)
        facets, uncategorized = self._category_facets(
            await self._store.kb_doc_category_counts(terms)
        )
        stats = await self._store.kb_doc_stats()
        return {
            "query": " ".join(terms),
            "category": applied,
            "documents": documents,
            "matched": matched,
            "categories": facets,
            "uncategorized": uncategorized,
            "total_documents": stats["total"],
            "legacy_documents": stats["legacy"],
            "total_chunks": await self._store.kb_count(),
        }

    async def category_overview(self) -> dict[str, Any]:
        """全部大类（按 CATEGORIES 的顺序）和各自的篇数，外加未分类的篇数。"""
        facets, uncategorized = self._category_facets(await self._store.kb_doc_category_counts([]))
        return {"categories": facets, "uncategorized": uncategorized}

    async def set_document_category(self, doc_id: str, category: Any) -> dict[str, Any] | None:
        """
        把一篇文档挪到另一个分类（空值 = 未分类），返回改完的文档摘要；文档不存在返回 None。

        只改文档表的分类这一列：分片、向量、关键词索引都不动，检索结果不受影响。
        分类不认识抛 UnknownCategory。
        """
        key = normalize_category(category)
        row = await self._store.kb_doc_get(doc_id)
        if row is None:
            return None
        if row.get("category", "") != key:
            await self._store.kb_doc_update(doc_id, {"category": key})
            logger.info(f"文档 {doc_id} 的分类改为 {key or '未分类'}")
        return self._present({**row, "category": key})

    async def get_document(self, doc_id: str) -> dict[str, Any] | None:
        """一篇文档的正文和它名下的分片。不存在返回 None。"""
        row = await self._store.kb_doc_get(doc_id)
        if row is None:
            return None
        passages = await self._store.kb_doc_chunks(doc_id)
        return {
            **self._present(row),
            "content": row["content"],
            # 去掉开头标题行的正文：标题已经作为 display_title 单独给了，
            # 阅读页不用自己再解析一遍 Markdown 去找它
            "body": split_heading(row["content"])[1],
            "passages": [
                {
                    "id": p["id"],
                    "index": p["chunk_index"],
                    "chars": len(p["content"]),
                    "content": p["content"],
                    "indexed": p["indexed"],
                    "ingested_at": p["ingested_at"],
                }
                for p in passages
            ],
        }

    async def document_titles(self, doc_ids: list[str]) -> dict[str, str]:
        """文档 id → 展示用标题。给检索结果和对话依据标出处用。"""
        wanted = sorted({d for d in doc_ids if d})
        briefs = await self._store.kb_doc_briefs(wanted)
        return {b["id"]: document_heading(b["head"]) or b["title"] for b in briefs}

    async def document_stats(self) -> dict[str, int]:
        stats = await self._store.kb_doc_stats()
        return {
            "total_documents": stats["total"],
            "legacy_documents": stats["legacy"],
            "total_chunks": await self._store.kb_count(),
        }

    async def search_async(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """
        混合检索：稠密向量 + 稀疏关键词并行召回，融合后取 Top-K。

        怎么融合看稠密一路用的是什么向量（见 fuse_candidates）：真正的 Embedding
        模型用 RRF；本地 n-gram 向量时关键词结果在前、向量结果只补位。

        返回 [{title, content, score, chunk, retrieval, doc_id, chunk_id}]：
          - score     余弦相似度（两路口径一致，稀疏召回的片段也带真实向量分）；
                      拿不到查询向量时是 None，见下；
          - retrieval 该片段由哪一路召回（dense / lexical / hybrid），
                      排障时能直接看出"这条是关键词捞回来的"；
          - doc_id / chunk_id  出处：哪篇文档的哪一片。回答的依据靠它追回原文。

        召回深度取 top_k 的 3 倍（下限 10）：RRF 融合的收益来自两路名次的
        交叉，如果每路只召回 top_k 条，交集之外的候选根本没机会进入融合，
        融合就退化成了"两个短列表拼接"。

        远端 Embedding 不可用时只走关键词那一路：结果的 retrieval 全是 lexical、
        score 是 None。少一路召回，但关键词这一路不依赖向量，没有理由跟着一起停。
        """
        if not (query or "").strip():
            return []

        recall_k = max(top_k * 3, 10)
        tokens = query_tokens(query)
        with span(
            "rag.hybrid_search",
            backend=self._store.backend,
            top_k=top_k,
            lexical_terms=len(tokens),
        ):
            try:
                # 查询和文档可以不是同一种编法（Embedder.embed_query）；
                # 没有这个方法的 Embedder 替身就按文档的编法来
                embed_query = getattr(self._embedder, "embed_query", self._embedder.embed)
                embedding = await embed_query(query)
            except EmbeddingUnavailable:
                embedding = None
            if embedding is None:
                dense = []
                lexical = await self._store.kb_search_lexical(tokens, None, recall_k)
            else:
                dense, lexical = await asyncio.gather(
                    self._store.kb_search(embedding, recall_k),
                    self._store.kb_search_lexical(tokens, embedding, recall_k),
                    return_exceptions=True,
                )
                failed = next(
                    (r for r in (dense, lexical) if isinstance(r, BaseException)), None
                )
                if failed is not None:
                    # 最常见的是向量维度对不上：另一个实例刚把库里的向量换成了新模型的，
                    # 这个实例还没跟上（core/model_runtime.py 几秒内会跟上）。关键词那一路
                    # 顺带算的余弦分也用到了查询向量，所以它也会跟着失败——不带向量再查一次。
                    # 不是向量的问题时，这一次照样会失败，错误原样抛出去。
                    logger.warning(f"带向量的检索失败，这次只走关键词: {type(failed).__name__}: {failed}")
                    dense = []
                    lexical = await self._store.kb_search_lexical(tokens, None, recall_k)
            # 没有 semantic 属性的 Embedder（测试里的替身）按"有语义"处理
            semantic = getattr(self._embedder, "semantic", True)
            fused = fuse_candidates(dense, lexical, semantic)[:top_k]

        return [
            {
                "title": r["title"],
                "content": r["content"],
                "score": round(float(r["score"]), 4) if r.get("score") is not None else None,
                "chunk": r.get("chunk_index", 0),
                "retrieval": r["retrieval"],
                "doc_id": r.get("doc_id", ""),
                "chunk_id": r.get("id", ""),
            }
            for r in fused
        ]

    async def doc_count_async(self) -> int:
        """片段总数。名字是历史遗留的——文档数见 document_stats。"""
        return await self._store.kb_count()

    async def ensure_seeded(self) -> None:
        """
        知识库为空时导入默认客服文档（原 Chroma 版在构造函数里做，现改为显式调用）。

        远端 Embedding 这时恰好不可用就先空着：服务照常起来，下次启动再导。
        为了几篇示例文档让整个服务起不来不值得。
        """
        if await self.doc_count_async() == 0:
            try:
                count = await self.add_documents_async(_DEFAULT_DOCS, source_type=SOURCE_SEED)
            except EmbeddingUnavailable as ex:
                logger.warning(f"知识库是空的，但 Embedding 不可用，默认文档没有导入: {ex}")
                return
            logger.info(f"已导入默认知识库: {count} 个片段")

    async def backfill_lexical(self, batch: int = 500) -> int:
        """
        为升级前写入的片段补建关键词索引，返回回填条数。

        混合检索是给已经在跑的库加的一路索引，老片段的 tokens 是空的。
        不回填的话它们永远只能被稠密这一路召回——症状是"新导入的文档
        关键词搜得到，老文档搜不到"，很难往索引上想。启动时跑一次即可。
        """
        total = 0
        while True:
            rows = await self._store.kb_rows_missing_tokens(batch)
            if not rows:
                break
            await self._store.kb_set_tokens(
                [(r["id"], index_text(r["title"], r["content"])) for r in rows]
            )
            total += len(rows)
            if len(rows) < batch:
                break
        if total:
            logger.info(f"已为 {total} 个历史片段回填关键词索引")
        return total

    # ── 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: dict[str, Any], context: Any) -> list[dict]:
        """作为工具 knowledge_search 的 handler 注册。"""
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    async def cache_version(self) -> str:
        """
        knowledge_search 结果缓存的版本，作为工具的 cache_version 注册。

        两样东西变了，之前缓存的检索结果都不能再用：
          - 库里的分片：导入、换向量这类写入会换掉存储层的版本号。号记在库里，
            员工实例导入了文档，客户实例缓存着的结果也跟着作废；
          - 这个实例算查询向量用的模型：embedder.epoch 换模型、换连接时都会变。
            另一个实例换完向量、这个实例还没跟上的那几秒，检索只走关键词——
            跟上之后不能再拿那几秒缓存下来的结果顶着用。
        """
        kb_revision = getattr(self._store, "kb_revision", None)
        revision = await kb_revision() if kb_revision is not None else ""
        return f"{revision}/{getattr(self._embedder, 'epoch', 0)}"

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def embedding_text(title: str, chunk: str) -> str:
        """
        一个分片拿去算向量的文本：标题 + 正文。

        导入和重建向量（scripts/reembed.py）必须用同一个：重建时换一种拼法，
        算出来的就不是"这批分片在新模型下的向量"，而是另一批文本的向量。
        """
        return f"{title}\n{chunk}"

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 500) -> list[str]:
        """将长文本按 chunk_size 切片，按句号/换行保持语义完整。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)
        return chunks


_DEFAULT_DOCS = [
    {
        "title": "定制流程与节点",
        "category": "design",
        "content": (
            "定制橱柜从下单到完工共七个节点。"
            "量尺：需要房屋已交付、墙面地面基本完工、水电点位已定。"
            "设计出图：客户确认平面图和立面图，确认后图纸即为生产依据。"
            "报价：按确认图纸出正式报价单，报价单是唯一的价格依据，口头报价不作数。"
            "签约收定金：定金到账后进入排产队列。"
            "排产开料：一旦开料，柜体尺寸和饰面不可更改，改动按变更单处理并产生材料损失。"
            "生产完工与送货：送货需提前预约，现场须具备卸货条件。"
            "安装验收：安装完成后现场清点并签署验收单。"
        ),
    },
    {
        "title": "交期说明",
        "category": "production",
        "content": (
            "交期说明。"
            "在图纸确认且定金到账后，标准线的标准交期为 4 周，进阶线为 5 周，定制线为 6 周，"
            "均以工厂排产确认为准。"
            "以下情形会延长交期：特殊饰面或特殊颜色需要单独调色、需要外购件、"
            "异形柜和到顶柜需要额外工序、旺季排产队列较长、客户在排产后提出变更。"
            "交期从图纸确认日起算，不从下单日起算。"
            "任何具体的完工日期都必须由工厂排产确认后给出，销售和客服不得自行承诺日期。"
        ),
    },
    {
        "title": "报价构成与变更加价",
        "category": "orders",
        "content": (
            "橱柜报价由六部分构成，报价单上分项列出："
            "柜体、门板饰面、台面（含水槽和灶台开孔）、五金、送货费、安装费。"
            "报价不含拆旧、水电改造、瓦工泥工和垃圾清运。"
            "变更加价按订单状态区分：未排产的变更只收材料差价；"
            "已排产未开料的变更需工厂确认能否撤回排产，收材料差价和排产调整费；"
            "已开料的变更按已投入材料损失加重新加工费计算；已完工的变更按新增订单处理。"
            "所有变更必须走正式变更单，变更单确认前的口头金额一律无效。"
        ),
    },
    {
        "title": "缺件漏发与补件流程",
        "category": "delivery",
        "content": (
            "收货时须按装箱单当场清点。"
            "发现缺件、漏发或错发，需在验收单上注明并提供订单号、柜号、缺失部件名称、"
            "装箱单编号和现场清点照片。"
            "客服登记后进入补件流程：先查是否为分批发货尚未到货，"
            "再查工厂库存是否可直接调配，库存无货的需重新排产。"
            "五金件和标准配件通常可从库存直接补发；门板、柜体和台面属定制件，必须重新排产。"
            "补件的具体到货日期以工厂排产确认为准，登记时不承诺日期。"
            "因缺件导致安装队现场停工的，标记为高优先级并转运营直接对接工厂。"
        ),
    },
    {
        "title": "安装与常见现场问题",
        "category": "installation",
        "content": (
            "现场问题分三类，责任方不同：做错了（按错图纸或错尺寸生产）、"
            "运输损（出厂合格、途中磕碰）、装歪了（产品合格、现场安装或墙面地面不平）。"
            "柜门关不严或对不齐绝大多数是铰链未调到位，属现场可调整范围："
            "铰链有左右、上下、前后三个调节方向，一次只调一个方向、每次半圈，"
            "正常门缝为 2-3mm 且上下一致。"
            "台面与墙面之间有缝在毛坯墙面上常见，标准做法是打胶收边或加挡水条。"
            "石英石拼接缝正常宽度为 1-2mm。"
            "质检结论出来前不对责任方下定论，也不承诺更换或返工的完成日期。"
        ),
    },
    {
        "title": "保修范围与除外情形",
        "category": "aftersales",
        "content": (
            "柜体和门板质保 5 年，五金件质保 3 年，台面质保 1 年，自安装验收签字之日起算。"
            "保修范围包括：正常使用下的柜体结构开裂、门板脱胶起层、饰面非人为起泡、"
            "铰链滑轨失效、台面非外力导致的开裂。"
            "以下不在保修范围：客户自行拆改柜体或重新开孔、非我方安装队安装、"
            "长期受潮或高温导致的变形、外力磕碰划伤、正常使用磨损、"
            "木质材料的天然纹理与批次色差。"
            "保修申请需提供订单号、柜号和现场照片，由工厂质检判定后确定处理方式。"
        ),
    },
]
