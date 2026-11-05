from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ai_sql_agent.core import get_logger, get_settings, log_event, stage
from ai_sql_agent.core.errors import ModelCallError
from ai_sql_agent.core.metrics import DEGRADED_RESPONSES
from ai_sql_agent.memory import ConversationMemoryContext, ConversationMemoryService, LongTermMemory
from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig

from .conversation_service import MemoryAwareSqlAgentService
from .data_qa import DataQAClient, ModelDataQAClient, RuleBasedDataQAClient
from .objects import PipelineResult
from .routing import (
    DynamicIntentRouter,
    ModelIntentClassifier,
    RouteDecision,
    RouteType,
)
from .text2sql_pipeline import Text2SQLPipeline

_logger = get_logger("router")


@dataclass
class RoutedSqlAgentResult:
    decision: RouteDecision
    answer: str
    memory_context: ConversationMemoryContext
    pipeline_result: Optional[PipelineResult] = None

    degraded: bool = False
    """
    对话问答这条路上模型不可用、答复改由规则版给出。查库那条路的降级记在
    `pipeline_result.degraded` 上；这条路没有 pipeline_result，所以单独记。
    不记的话，调用方拿到的是一句固定话术加 `degraded=false`——看起来就像模型认真答过。
    """

    degrade_reason: str = ""

    @property
    def queried_database(self) -> bool:
        return self.pipeline_result is not None


class DynamicSqlAgentService:
    """记忆感知的动态路由入口：按意图选择 Text2SQL 或数据问答。"""

    def __init__(
        self,
        pipeline: Text2SQLPipeline,
        memory: ConversationMemoryService,
        *,
        router: Optional[DynamicIntentRouter] = None,
        data_qa_client: Optional[DataQAClient] = None,
        use_model_when_available: bool = True,
    ):
        self.pipeline = pipeline
        self.memory = memory
        self.memory_pipeline = MemoryAwareSqlAgentService(pipeline, memory)
        model_client = self._build_model_client() if use_model_when_available else None
        self.router = router or DynamicIntentRouter(
            model_classifier=(ModelIntentClassifier(model_client) if model_client else None)
        )
        # 指标口径从 Pipeline 已经解析好的语义层取，而不是让问答类自己再读一次文件：
        # 注入自定义语义层时（测试、一个进程跑两套元数据），两边必须是同一份，
        # 否则会出现「查得对但口径解释来自另一套元数据」这种极难排查的错位。
        definitions = dict(
            getattr(getattr(pipeline, "semantic", None), "metric_definitions", {}) or {}
        )
        self.data_qa_client = data_qa_client or (
            ModelDataQAClient(model_client, definitions=definitions)
            if model_client
            else RuleBasedDataQAClient(definitions=definitions)
        )

    def run(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        enable_long_term: bool = False,
        long_term_top_k: Optional[int] = None,
        access=None,
    ) -> RoutedSqlAgentResult:
        with stage(
            "memory_context",
            _logger,
            user_id=user_id,
            session_id=session_id,
            enable_long_term=enable_long_term,
        ) as memory_stage:
            context = self.memory.begin_user_turn(
                user_id=user_id,
                session_id=session_id,
                query=query,
                enable_long_term=enable_long_term,
                long_term_top_k=long_term_top_k,
            )
            memory_stage.set(
                history_messages=len(context.short_term.messages),
                summary_pending=context.short_term.summary_pending,
                long_term_hits=len(context.long_term_hits),
            )

        with stage("routing", _logger) as routing_stage:
            decision = self.router.route(query, context)
            routing_stage.set(
                route=decision.route.value,
                confidence=decision.confidence,
                reason=decision.reason,
            )

        if decision.route == RouteType.DATABASE_QUERY:
            response = self.memory_pipeline.run_with_context(
                user_id=user_id,
                session_id=session_id,
                query=query,
                context=context,
                assistant_metadata={"route_decision": decision.to_dict()},
                access=access,
            )
            return RoutedSqlAgentResult(
                decision=decision,
                answer=response.answer,
                memory_context=context,
                pipeline_result=response.pipeline_result,
            )

        degraded = False
        with stage("data_qa", _logger) as qa_stage:
            try:
                answer = self.data_qa_client.answer(query, context)
                qa_stage.set(answer_chars=len(answer), degraded=False)
            except ModelCallError as exc:
                degraded = True
                # 模型挂了不该整个请求失败：退回规则版，答复基于记忆里的历史结果
                DEGRADED_RESPONSES.inc(reason="model_unavailable", failed_stage="data_qa")
                log_event(
                    _logger,
                    logging.WARNING,
                    "data_qa.degraded",
                    error_category=exc.category,
                    error=str(exc),
                )
                answer = RuleBasedDataQAClient().answer(query, context)
                qa_stage.set(answer_chars=len(answer), degraded=True)

        self.memory.record_assistant_message(
            user_id=user_id,
            session_id=session_id,
            content=answer,
            message_type="data_qa",
            metadata={"route_decision": decision.to_dict()},
        )
        return RoutedSqlAgentResult(
            decision=decision,
            answer=answer,
            memory_context=context,
            degraded=degraded,
            degrade_reason="model_unavailable" if degraded else "",
        )

    def save_result_to_personal_knowledge_base(
        self, *, user_id: str, result: PipelineResult
    ) -> LongTermMemory:
        return self.memory.save_pipeline_result(user_id=user_id, result=result)

    @staticmethod
    def _build_model_client():
        if not get_settings().has_model_credentials:
            return None
        return ThinkingModelClient(
            ThinkingModelConfig(
                temperature=0.0,
                use_mock_when_no_api_key=False,
            )
        )
