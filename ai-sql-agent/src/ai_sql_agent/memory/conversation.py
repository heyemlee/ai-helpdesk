from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ai_sql_agent.core import get_logger, get_settings, log_event

from .long_term import (
    LocalMemoryEmbeddingClient,
    LongTermMemoryConfig,
    LongTermMemoryService,
    MemoryReranker,
)
from .objects import (
    ConversationMemoryContext,
    LongTermMemory,
    MemoryHit,
    MemoryKind,
    MemoryMessage,
    MessageRole,
)
from .short_term import ShortTermMemoryConfig, ShortTermMemoryService
from .storage import SQLiteMemoryStore
from .summarizer import (
    ExtractiveMemorySummarizer,
    MemorySummarizer,
    ModelMemorySummarizer,
)
from .reranker import AliyunMemoryReranker
from .vector_store import PgVectorLongTermMemoryConfig, PgVectorLongTermVectorIndex

_logger = get_logger("memory")

DEFAULT_MEMORY_DB_PATH = "runtime_data/memory.db"


@dataclass
class MemoryServiceConfig:
    db_path: str | Path = DEFAULT_MEMORY_DB_PATH
    embedding_dimensions: int = 1024
    use_model_summarizer_when_available: bool = True
    use_reranker_when_available: bool = True
    pgvector_dsn: str = ""
    """长期记忆向量索引连接串；留空则取 Settings.pgvector_dsn。"""

    vector_table_name: str = "ai_sql_long_term_memory"

    vector_backend: str = "auto"
    """
    向量索引后端选择：

    - "auto"：配了 DSN 就用 pgvector，连不上则告警并回退 SQLite 全量打分；
    - "pgvector"：强制使用，连不上直接失败（生产环境用，避免静默降级）；
    - "none"：始终走 SQLite，用于离线开发与单测。
    """
    short_term: ShortTermMemoryConfig = field(default_factory=ShortTermMemoryConfig)
    long_term: LongTermMemoryConfig = field(default_factory=LongTermMemoryConfig)


class ConversationMemoryService:
    """供 API/路由层调用的统一长短期记忆入口。"""

    def __init__(
        self,
        config: Optional[MemoryServiceConfig] = None,
        *,
        store: Optional[SQLiteMemoryStore] = None,
        summarizer: Optional[MemorySummarizer] = None,
        embedding_client: Any = None,
        reranker: Optional[MemoryReranker] = None,
    ):
        self.config = config or MemoryServiceConfig()
        self.store = store or SQLiteMemoryStore(self.config.db_path)
        self._owns_summarizer = summarizer is None
        self.summarizer = summarizer or self._build_default_summarizer()
        resolved_embedding = embedding_client or LocalMemoryEmbeddingClient(
            dimensions=self.config.embedding_dimensions
        )
        self.short_term = ShortTermMemoryService(
            store=self.store,
            summarizer=self.summarizer,
            config=self.config.short_term,
        )
        resolved_reranker = reranker or self._build_default_reranker()
        vector_index = self._build_vector_index()
        self.long_term = LongTermMemoryService(
            store=self.store,
            embedding_client=resolved_embedding,
            summarizer=self.summarizer,
            reranker=resolved_reranker,
            vector_index=vector_index,
            config=self.config.long_term,
        )

    def begin_user_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        enable_long_term: bool = False,
        long_term_top_k: Optional[int] = None,
        memory_kinds: Optional[Sequence[MemoryKind]] = None,
        metadata_filters: Optional[Dict[str, Any]] = None,
    ) -> ConversationMemoryContext:
        self.short_term.add_message(
            user_id=user_id,
            session_id=session_id,
            role=MessageRole.USER,
            content=query,
        )
        return self.get_context(
            user_id=user_id,
            session_id=session_id,
            query=query,
            enable_long_term=enable_long_term,
            long_term_top_k=long_term_top_k,
            memory_kinds=memory_kinds,
            metadata_filters=metadata_filters,
        )

    def get_context(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str = "",
        enable_long_term: bool = False,
        long_term_top_k: Optional[int] = None,
        memory_kinds: Optional[Sequence[MemoryKind]] = None,
        metadata_filters: Optional[Dict[str, Any]] = None,
    ) -> ConversationMemoryContext:
        short_context = self.short_term.get_context(user_id=user_id, session_id=session_id)
        hits: list[MemoryHit] = []
        if enable_long_term and query.strip():
            hits = self.long_term.recall(
                user_id=user_id,
                query=query,
                top_k=long_term_top_k,
                kinds=memory_kinds,
                metadata_filters=metadata_filters,
            )
        return ConversationMemoryContext(
            short_term=short_context,
            long_term_hits=hits,
            long_term_enabled=enable_long_term,
        )

    def record_assistant_message(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        message_type: str = "text",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryMessage:
        return self.short_term.add_message(
            user_id=user_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=content,
            message_type=message_type,
            payload=payload,
            metadata=metadata,
        )

    def save_structured_result(
        self,
        *,
        user_id: str,
        query: str,
        result: Any,
        database: str = "",
        tables: Optional[list[str]] = None,
        columns: Optional[list[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        cot: str = "",
        sql: str | list[str] = "",
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> LongTermMemory:
        """仅在用户明确点击/请求保存时调用。"""
        return self.long_term.save_structured(
            user_id=user_id,
            query=query,
            result=result,
            database=database,
            tables=tables,
            columns=columns,
            filters=filters,
            cot=cot,
            sql=sql,
            extra_metadata=extra_metadata,
        )

    def save_unstructured_content(
        self,
        *,
        user_id: str,
        content: Any,
        title: str = "",
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> LongTermMemory:
        """仅在用户明确点击/请求保存时调用。"""
        return self.long_term.save_unstructured(
            user_id=user_id,
            content=content,
            title=title,
            extra_metadata=extra_metadata,
        )

    def save_pipeline_result(self, *, user_id: str, result: Any) -> LongTermMemory:
        result_dict = result.to_dict()
        sql_list = [log.sql for log in result.step_logs]
        where_conditions = []
        for sql in sql_list:
            match = re.search(r"\bWHERE\b(.*?)(?:;|$)", sql, flags=re.IGNORECASE | re.DOTALL)
            if match:
                where_conditions.append(" ".join(match.group(1).split()))
        tables = re.findall(r"表名：([^\n]+)", result.schema_context)
        columns = re.findall(r"字段名：([^\n]+)", result.schema_context)
        database = result.step_logs[0].database if result.step_logs else ""
        return self.save_structured_result(
            user_id=user_id,
            query=result.query,
            result=result_dict,
            database=database,
            tables=list(dict.fromkeys(tables)),
            columns=list(dict.fromkeys(columns)),
            filters={"sql_where": where_conditions},
            cot=result.cot_output,
            sql=sql_list,
            extra_metadata={"source": "text2sql_pipeline"},
        )

    def delete_long_term_memory(self, *, user_id: str, memory_id: str) -> bool:
        return self.long_term.delete(user_id=user_id, memory_id=memory_id)

    def close(self, wait: bool = True) -> None:
        self.short_term.close(wait=wait)

    def refresh_summarizer(self) -> None:
        """
        模型连接换过之后调：摘要用模型还是用抽取，按现在有没有凭证重新选一次。

        这个选择以前只在构造时做一次——启动时没有凭证，之后在页面上配了 Key，
        摘要也会一直停在抽取版。调用方自己传进来的摘要器不动。
        """
        if not self._owns_summarizer:
            return
        self.summarizer = self._build_default_summarizer()
        self.short_term.summarizer = self.summarizer
        self.long_term.summarizer = self.summarizer

    def _build_default_summarizer(self) -> MemorySummarizer:
        if (
            self.config.use_model_summarizer_when_available
            and get_settings().has_model_credentials
        ):
            from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig

            return ModelMemorySummarizer(
                ThinkingModelClient(
                    ThinkingModelConfig(
                        temperature=0.0,
                        use_mock_when_no_api_key=False,
                    )
                )
            )
        return ExtractiveMemorySummarizer()

    def _build_default_reranker(self) -> Optional[MemoryReranker]:
        if not self.config.use_reranker_when_available:
            return None
        settings = get_settings()
        if not settings.has_model_credentials or not settings.dashscope_workspace_id:
            return None
        from ai_sql_agent.retrieval.rerank_client import AliyunRerankClient, AliyunRerankConfig

        return AliyunMemoryReranker(
            AliyunRerankClient(
                AliyunRerankConfig(
                    instruct=(
                        "Given a user question, rank personal memory summaries "
                        "by relevance and factual usefulness."
                    )
                )
            )
        )

    def _build_vector_index(self):
        """
        长期记忆向量索引。

        配置了 pgvector 就用 pgvector（与 Schema 检索同一套向量栈）；
        没配置或连不上就回退到 SQLite + numpy 全量打分，保证离线可用。
        """
        if self.config.vector_backend == "none":
            return None

        dsn = self.config.pgvector_dsn or get_settings().pgvector_dsn
        if not dsn:
            if self.config.vector_backend == "pgvector":
                raise ValueError(
                    "vector_backend=pgvector 但未配置 AI_SQL_PGVECTOR_DSN。"
                )
            return None

        try:
            index = PgVectorLongTermVectorIndex(
                PgVectorLongTermMemoryConfig(
                    dsn=dsn,
                    table_name=self.config.vector_table_name,
                    embedding_dimensions=self.config.embedding_dimensions,
                )
            )
        except Exception as exc:
            if self.config.vector_backend == "pgvector":
                # 显式要求 pgvector 时不做静默降级
                raise
            log_event(
                _logger,
                logging.WARNING,
                "memory.vector_index.fallback",
                backend="sqlite",
                reason=str(exc),
            )
            return None

        log_event(
            _logger,
            logging.INFO,
            "memory.vector_index.selected",
            backend="pgvector",
            table=self.config.vector_table_name,
        )
        return index


def create_memory_service() -> ConversationMemoryService:
    """
    按配置创建对话记忆服务。库文件的位置取 `Settings.memory_db_path`
    （环境变量 `AI_SQL_MEMORY_DB_PATH`），相对路径按进程的工作目录解析。

    这个配置项早就在 Settings 里，但一直没有人读它：服务启动时直接
    `ConversationMemoryService()`，记忆库于是永远落在 `./runtime_data/memory.db`。
    同一个工作目录下起两个实例（比如一台机器上的正式服务和练习服务），
    两边的对话历史就写进了同一个文件——上一个实例里问过的问题，会被下一个实例
    当成「刚才的查询结果」拿来回答。

    默认值与原来写死的路径相同，所以没有设置这个变量的部署读到的还是原来那份库，
    里面的记忆原样保留；这里不迁移、不清理任何已有的文件。留空同样落回默认路径——
    SQLite 把空路径当成「用完即弃的临时库」，那样对话记忆会悄悄丢掉。
    """
    configured = str(get_settings().memory_db_path or "").strip()
    path = configured or DEFAULT_MEMORY_DB_PATH
    log_event(_logger, logging.INFO, "memory.store", backend="sqlite", path=path)
    return ConversationMemoryService(MemoryServiceConfig(db_path=path))
