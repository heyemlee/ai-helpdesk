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

去重策略：片段 ID = md5(标题 + 完整片段内容)。同一份文档重复上传
（运营误操作、定时同步任务重跑）产生完全相同的 ID，写入前先查已存在
片段——导入幂等，同一段 SOP 不会在检索结果里出现两次。同一个 ID 也是
RRF 融合的合并键：两路召回同一片段时按 ID 归并，而不是靠内容字符串比对。
"""
import asyncio
import hashlib
import logging
import time
from typing import Any

from core.lexical import index_text, query_tokens
from core.telemetry import span

logger = logging.getLogger(__name__)

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
            key = row.get("id") or f"{row.get('title', '')}:{row.get('chunk_index', 0)}"
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

    async def add_documents_async(self, documents: list[dict[str, str]]) -> int:
        """
        批量导入文档，按内容去重，返回新增片段数。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档自动切片（每片 500 字）。
        """
        candidates: list[dict[str, Any]] = []
        seen_in_batch: set[str] = set()
        now = time.time()

        for doc in documents:
            title = doc.get("title", "")
            content = doc.get("content", "")
            chunks = self._chunk_text(content, chunk_size=500)
            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}\n{chunk}".encode()).hexdigest()
                if doc_id in seen_in_batch:
                    continue
                seen_in_batch.add(doc_id)
                candidates.append({
                    "id": doc_id,
                    "title": title,
                    "content": chunk,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "ingested_at": now,
                    "tokens": index_text(title, chunk),
                })

        if not candidates:
            return 0

        existing = await self._store.kb_existing_ids([c["id"] for c in candidates])
        fresh = [c for c in candidates if c["id"] not in existing]
        if not fresh:
            logger.info(f"知识库导入: {len(candidates)} 个片段全部已存在，跳过")
            return 0

        for c in fresh:
            c["embedding"] = await self._embedder.embed(f"{c['title']}\n{c['content']}")
        await self._store.kb_add(fresh)

        skipped = len(candidates) - len(fresh)
        logger.info(
            f"知识库导入 {len(fresh)} 个新片段"
            + (f"，跳过 {skipped} 个重复片段" if skipped else "")
        )
        return len(fresh)

    async def search_async(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """
        混合检索：稠密向量 + 稀疏关键词并行召回，RRF 融合后取 Top-K。

        返回 [{title, content, score, chunk, retrieval}]：
          - score     余弦相似度（两路口径一致，稀疏召回的片段也带真实向量分）；
          - retrieval 该片段由哪一路召回（dense / lexical / hybrid），
                      排障时能直接看出"这条是关键词捞回来的"。

        召回深度取 top_k 的 3 倍（下限 10）：RRF 融合的收益来自两路名次的
        交叉，如果每路只召回 top_k 条，交集之外的候选根本没机会进入融合，
        融合就退化成了"两个短列表拼接"。
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
            embedding = await self._embedder.embed(query)
            dense, lexical = await asyncio.gather(
                self._store.kb_search(embedding, recall_k),
                self._store.kb_search_lexical(tokens, embedding, recall_k),
            )
            fused = rrf_fuse({"dense": dense, "lexical": lexical}, top_k=top_k)

        return [
            {
                "title": r["title"],
                "content": r["content"],
                "score": round(float(r.get("score", 0.0)), 4),
                "chunk": r.get("chunk_index", 0),
                "retrieval": r["retrieval"],
            }
            for r in fused
        ]

    async def doc_count_async(self) -> int:
        return await self._store.kb_count()

    async def ensure_seeded(self) -> None:
        """知识库为空时导入默认客服文档（原 Chroma 版在构造函数里做，现改为显式调用）。"""
        if await self.doc_count_async() == 0:
            count = await self.add_documents_async(_DEFAULT_DOCS)
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

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: dict[str, Any], context: Any) -> list[dict]:
        """作为 MCP 工具 knowledge_search 的 handler 注册。"""
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

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
