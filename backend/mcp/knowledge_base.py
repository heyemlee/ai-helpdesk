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
        "title": "退款政策",
        "content": (
            "退款政策说明。"
            "用户在购买后 7 天内可以申请无理由退款。"
            "退款申请提交后，系统会在 1-3 个工作日内审核。"
            "审核通过后，款项将在 5-7 个工作日内退回原支付账户。"
            "如果商品已发货，需要先完成退货流程才能退款。"
            "退货运费由用户承担，除非是商品质量问题。"
            "超过 7 天但未超过 30 天的订单，需要提供商品质量问题的证据才能退款。"
        ),
    },
    {
        "title": "订单查询",
        "content": (
            "订单查询指南。"
            "用户可以通过订单号查询订单状态。"
            "订单状态包括：待支付、已支付、已发货、运输中、已签收、已完成。"
            "如果订单显示已发货但超过 7 天未收到，可以联系客服申请查件。"
            "物流信息通常在发货后 24 小时内更新。"
            "如果订单显示异常，请提供订单号联系客服处理。"
        ),
    },
    {
        "title": "账户安全",
        "content": (
            "账户安全说明。"
            "建议用户定期修改密码，密码长度至少 8 位，包含字母和数字。"
            "如果忘记密码，可以通过绑定的手机号或邮箱重置。"
            "发现账户异常登录时，系统会自动锁定账户并发送通知。"
            "用户可以在安全设置中开启两步验证，提高账户安全性。"
            "不要将密码分享给他人，客服人员不会索要用户密码。"
        ),
    },
    {
        "title": "技术故障排查",
        "content": (
            "常见技术问题排查。"
            "应用崩溃：请尝试清除缓存后重启应用，如果问题持续请更新到最新版本。"
            "登录失败 401 错误：表示认证失败，请检查用户名密码是否正确，或尝试重置密码。"
            "页面加载慢：检查网络连接，尝试切换 WiFi 或移动数据。"
            "支付失败：确认银行卡余额充足，检查是否开启了网上支付功能。"
            "500 服务器错误：这是服务端问题，请稍后重试，如果持续出现请联系技术支持。"
        ),
    },
    {
        "title": "会员与积分",
        "content": (
            "会员积分规则。"
            "每消费 1 元累积 1 积分。"
            "积分可以在下次购物时抵扣，100 积分 = 1 元。"
            "会员等级分为：普通会员、银卡会员（累计消费 1000 元）、金卡会员（累计消费 5000 元）。"
            "银卡会员享受 95 折优惠，金卡会员享受 9 折优惠。"
            "积分有效期为 1 年，过期自动清零。"
            "生日当月消费可获得双倍积分。"
        ),
    },
    {
        "title": "配送说明",
        "content": (
            "配送服务说明。"
            "标准配送：3-5 个工作日送达，免运费（订单满 99 元）。"
            "加急配送：1-2 个工作日送达，运费 15 元。"
            "同城配送：当日达或次日达，运费 10 元。"
            "偏远地区可能需要额外 2-3 天。"
            "配送时间为每天 9:00-18:00，节假日可能延迟。"
            "如果需要修改收货地址，请在发货前联系客服。"
        ),
    },
]
