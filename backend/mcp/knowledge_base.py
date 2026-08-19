"""
RAG 知识库（pgvector / SQLite 双后端）。

亮点：真实的向量检索链路——文档切片 → Embedder 生成向量 → 存储后端
（生产 Postgres+pgvector 的 `<=>` 余弦检索；demo/单测 SQLite 暴力扫）。
本模块只负责业务逻辑：切片、内容哈希去重、结果格式化、默认文档种子，
向量的存与查全部面向 storage/vector_store.py 的接口，后端可替换。

去重策略：片段 ID = md5(标题 + 完整片段内容)。同一份文档重复上传
（运营误操作、定时同步任务重跑）产生完全相同的 ID，写入前先查已存在
片段——导入幂等，同一段 SOP 不会在检索结果里出现两次。
"""
import hashlib
import logging
import time
from typing import Any

from core.telemetry import span

logger = logging.getLogger(__name__)


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
        """语义检索：返回 [{title, content, score, chunk}]，score 为余弦相似度。"""
        if not (query or "").strip():
            return []
        with span("rag.vector_search", backend=self._store.backend, top_k=top_k):
            embedding = await self._embedder.embed(query)
            rows = await self._store.kb_search(embedding, top_k)
        return [
            {
                "title": r["title"],
                "content": r["content"],
                "score": round(float(r["score"]), 4),
                "chunk": r.get("chunk_index", 0),
            }
            for r in rows
        ]

    async def doc_count_async(self) -> int:
        return await self._store.kb_count()

    async def ensure_seeded(self) -> None:
        """知识库为空时导入默认客服文档（原 Chroma 版在构造函数里做，现改为显式调用）。"""
        if await self.doc_count_async() == 0:
            count = await self.add_documents_async(_DEFAULT_DOCS)
            logger.info(f"已导入默认知识库: {count} 个片段")

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
