"""
亮点：多轮对话记忆管理

三级记忆架构，模拟人类记忆机制：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（pgvector / SQLite）—— 跨会话的历史对话，按语义相似度检索
  3. 用户画像（Postgres / SQLite）—— 从对话中提炼的长期偏好和实体

情景记忆与画像面向 storage/vector_store.py 的存储接口：生产走
Postgres + pgvector（`<=>` 余弦检索），未配置 POSTGRES_DSN 时用
SQLite 兜底。向量由 core/embedder.py 统一生成。

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - 向量由 core/embedder.py 统一生成：配置了 OpenAI 兼容端点走远端，
    未配置时退化为本地字符 n-gram 哈希向量
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import redis.asyncio as redis
from pydantic import BaseModel

from core.llm_utils import (
    DEFAULT_ANTHROPIC_MODEL,
    create_llm_client,
    extract_text_content,
    structured_create,
    structured_parse,
)
from core.telemetry import span

logger = logging.getLogger(__name__)


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   dict[str, Any] = field(default_factory=dict)


class UserProfilePayload(BaseModel):
    """
    用户画像的提炼结果，由结构化输出在服务端按 schema 约束。

    字段是写死的三条而不是一个自由 dict：strict schema 不允许
    `additionalProperties`，而且原来那版 prompt 里给的样例
    `{"entities": {"产品": [], "问题类型": []}}` 也从来没有约束力——模型换个
    key 名（"产品类型"、"products"）画像就变形，读的人无从发现。

    key 用英文还有个附带好处：画像会被 `json.dumps(..., ensure_ascii=True)`
    渲染进 prompt，中文 key 在那一步会变成一串 \\uXXXX 转义，白占 token
    还不好读。
    """

    preferences: list[str]
    products: list[str]
    issue_types: list[str]


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  list[Message]   # 工作记忆：最近对话
    relevant_history: list[str]       # 情景记忆：语义相关的历史片段
    user_profile:     dict[str, Any]  # 用户画像：偏好、常用实体
    summary:          str             # 当前会话摘要（压缩后）

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    # 跨会话召回内容的使用边界。三级记忆里，「会话摘要」和「最近对话」属于当前
    # 这轮对话，而「相关历史」和「用户画像」是按 user_id 跨会话检索来的——它们
    # 可能来自几周前、可能讲的是完全另一个工程。
    #
    # 不标注会出真问题。实测过一次：客户在新会话里问 J26-00001 的欠款，模型从
    # 相关历史里读到上一个会话的 J26-00136，认定那才是「该账户名下的订单」，
    # 于是以账户安全为由拒绝回答，反过来要客户证明 J26-00001 是自己的。对
    # builder 客户这是常态——一个客户同时有几十个 job，每问一个新工程都会撞上。
    #
    # 又不能简单关掉：同一份召回在另一个用例里帮了忙（客户报了个不存在的工程号，
    # 模型顺带提示「您之前问过 J26-00136，是不是想查这一单」）。所以边界是
    # **可以用来提示，不能用来否定客户此刻说的话**。
    _RECALL_CAUTION = (
        "以下两节是按用户身份跨会话召回的，**不属于当前这轮对话**：可能来自很久以前，"
        "也可能讲的是另一个工程/订单。用途仅限于补充背景或在客户信息有误时给出提示"
        "（例如「您之前咨询过 X，是不是想查这一单」）。"
        "**不得**据此认定客户「名下只有」其中提到的工程，"
        "**不得**用它质疑或拒绝客户此刻提出的工程号，"
        "**不得**把其中的金额、日期、状态当作本轮的事实——本轮事实只来自本轮查到的数据。"
    )

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")

        recalled = []
        if self.relevant_history:
            recalled.append("[相关历史（跨会话召回）]\n"
                            + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            recalled.append("[用户画像（跨会话累积）]\n"
                            + json.dumps(self.user_profile, ensure_ascii=True))
        if recalled:
            parts.append(self._RECALL_CAUTION)
            parts.extend(recalled)
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和用户画像存向量库（pgvector / SQLite）。
    """

    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 KEEP_RECENT 条
    KEEP_RECENT   = 5     # 压缩后保留的最近消息条数
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数

    def __init__(
        self,
        store,
        embedder,
        redis_url:    str = "redis://localhost:6379/0",
        api_key:      str = "",
        base_url:     str | None = None,
        model: str = DEFAULT_ANTHROPIC_MODEL,
    ):
        self._client = create_llm_client(api_key, base_url)
        self._model  = model

        self._redis = redis.from_url(redis_url, decode_responses=True)
        # 情景记忆 + 用户画像的存储后端（PgStore 或 LocalStore）与向量生成器
        self._store = store
        self._embedder = embedder

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """将一条消息写入工作记忆，超阈值时自动压缩。"""
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        # 追加到 Redis 列表（左推，最新在前）
        await self._redis.lpush(key, json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        }))
        await self._redis.expire(key, 86400)  # 24h TTL

        # 超过压缩阈值时触发压缩
        if await self._redis.llen(key) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        """
        从当前工作记忆中提炼用户偏好，更新用户画像。
        用 LLM 提炼偏好，然后按 user_id 覆盖写入向量库——每用户单条，
        避免多条并存时取不到最新画像。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        prompt = self._safe_text(
            f"从以下对话中提炼这位客户的长期偏好和反复出现的关键实体。\n"
            f"只提炼跨轮次仍然成立的信息，一次性的寒暄和当轮细节不要收进来。\n\n对话:\n{text}"
        )

        try:
            payload = await structured_parse(
                self._client,
                output_format=UserProfilePayload,
                model=self._model, max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )

            # 每个用户只保留一条画像，整条覆盖（存储层 ON CONFLICT/REPLACE 保证）。
            doc_text = self._safe_text(json.dumps(payload.model_dump(), ensure_ascii=False))
            await self._store.profile_set(user_id, conv_id, doc_text)
            logger.info(f"用户画像已更新: {user_id}")
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 1. 工作记忆（当前会话最近消息）
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        with span("memory.get_context"):
            return await self._get_context_inner(user_id, conv_id, query)

    async def _get_context_inner(self, user_id: str, conv_id: str, query: str) -> MemoryContext:
        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history = await self._search_episodic(user_id, query or (recent[-1].content if recent else ""))

        # 3. 用户画像
        profile = await self._get_profile(user_id)

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        工作记忆压缩：
          1. 用 LLM 对旧消息生成摘要
          2. 摘要存 Redis（覆盖旧摘要）
          3. 旧消息存入情景记忆（向量库）供跨会话检索
          4. 工作记忆只保留最近 5 条
        """
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return

        to_compress = messages[:-self.KEEP_RECENT]
        keep        = messages[-self.KEEP_RECENT:]

        # LLM 摘要
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"用 2-3 句话总结以下对话的关键信息：\n{text}")
        try:
            resp = await structured_create(
                self._client,
                model=self._model, max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(extract_text_content(resp.content)).strip()
        except Exception:
            summary = f"对话包含 {len(to_compress)} 条消息（摘要生成失败）"

        # 存摘要到 Redis
        skey = self._summary_key(user_id, conv_id)
        old_summary = await self._redis.get(skey) or ""
        new_summary = self._safe_text(f"{old_summary}\n{summary}").strip()
        await self._redis.setex(skey, 86400, new_summary)

        # 旧消息存入情景记忆
        await self._store_episodic(user_id, conv_id, text, summary)

        # 重置工作记忆为最近 KEEP_RECENT 条
        key = self._wm_key(user_id, conv_id)
        await self._redis.delete(key)
        for m in reversed(keep):
            await self._redis.lpush(key, json.dumps({
                "role": m.role.value, "content": m.content,
                "ts": m.timestamp.isoformat(), "metadata": m.metadata,
            }))
        await self._redis.expire(key, 86400)
        logger.info(f"工作记忆压缩完成: {user_id}/{conv_id}，摘要 {len(summary)} 字")

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str) -> list[Message]:
        key  = self._wm_key(user_id, conv_id)
        # 压缩阈值已经限住了列表长度，这里直接取全部
        raws = await self._redis.lrange(key, 0, -1)
        msgs = []
        for raw in reversed(raws):  # Redis lpush 最新在前，reversed 还原时序
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, query: str) -> list[str]:
        """语义检索情景记忆：向量由 Embedder 生成，检索按 user_id 过滤。"""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            embedding = await self._embedder.embed(query_text)
            docs = await self._store.episodic_search(
                self._safe_text(user_id), embedding, self.HISTORY_TOP_K
            )
            return [self._safe_text(doc) for doc in docs if isinstance(doc, str) and doc.strip()]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """将压缩后的对话片段存入情景记忆，向量由 core/embedder.py 生成。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            embedding = await self._embedder.embed(summary)
            await self._store.episodic_add(
                doc_id, user_id, conv_id, summary, text, embedding
            )
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")

    async def _get_profile(self, user_id: str) -> dict[str, Any]:
        """获取用户画像：user_id 主键直读。"""
        try:
            raw = await self._store.profile_get(self._safe_text(user_id))
            if raw:
                return json.loads(raw)
        except Exception as ex:
            logger.warning(f"读取用户画像失败: {ex}")
        return {}

    async def close(self) -> None:
        """关闭异步 Redis 连接。"""
        await self._redis.aclose()

    @staticmethod
    def _wm_key(user_id: str, conv_id: str) -> str:
        return f"wm:{user_id}:{conv_id}"

    @staticmethod
    def _summary_key(user_id: str, conv_id: str) -> str:
        return f"summary:{user_id}:{conv_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """清洗为合法 UTF-8，避免 Redis/存储层读写遇到代理字符。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """递归清洗 metadata，避免 Redis/存储层后续读写遇到非法 UTF-8。"""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value
