from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MemoryKind(str, Enum):
    STRUCTURED = "structured"
    UNSTRUCTURED = "unstructured"


@dataclass(frozen=True)
class MemoryMessage:
    id: int
    session_id: str
    user_id: str
    role: MessageRole
    content: str
    message_type: str = "text"
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    token_count: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class ConversationSummary:
    id: int
    session_id: str
    user_id: str
    version: int
    content: str
    through_message_id: int
    created_at: str = ""


@dataclass(frozen=True)
class ShortTermContext:
    session_id: str
    user_id: str
    messages: List[MemoryMessage] = field(default_factory=list)
    summary: Optional[ConversationSummary] = None
    summary_pending: bool = False

    def to_prompt_context(self) -> str:
        parts: List[str] = []
        if self.summary and self.summary.content:
            parts.extend(["[较早对话摘要]", self.summary.content])
        if self.messages:
            parts.append("[近期对话]")
            for message in self.messages:
                role = {
                    MessageRole.USER: "用户",
                    MessageRole.ASSISTANT: "助手",
                    MessageRole.SYSTEM: "系统",
                }[message.role]
                parts.append(f"{role}：{message.content}")
        return "\n".join(parts)


@dataclass(frozen=True)
class LongTermMemory:
    id: str
    user_id: str
    kind: MemoryKind
    summary: str
    content: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Any = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MemoryHit:
    memory: LongTermMemory
    score: float
    vector_score: float
    rerank_score: Optional[float] = None


@dataclass(frozen=True)
class ConversationMemoryContext:
    short_term: ShortTermContext
    long_term_hits: List[MemoryHit] = field(default_factory=list)
    long_term_enabled: bool = False

    def to_prompt_context(self) -> str:
        parts: List[str] = []
        short_text = self.short_term.to_prompt_context()
        if short_text:
            parts.append(short_text)
        if self.long_term_enabled and self.long_term_hits:
            parts.append("[个人知识库召回]")
            for index, hit in enumerate(self.long_term_hits, start=1):
                parts.append(f"{index}. {hit.memory.summary}")
        return "\n".join(parts)
