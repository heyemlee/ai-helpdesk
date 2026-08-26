"""
model_call 事件：每次模型请求结束时发一条——成功、出错、被保护挡下都发。

在 core/llm_guard.py 的 GuardedLLMClient 那一层调：一轮里的所有对话模型请求（意图、改写、
重排、作答、摘要、画像、评审）都经过它，包括被保护挡下、根本没出网的那些。控制台的
"测试连接"直接造 SDK 客户端，不属于任何一轮，不记。

这一层看不到的：SDK 自己的重试（LLM_MAX_RETRIES）在这里只算一次调用；远端 Embedding 和
Rerank 不是对话模型，不经过这里（OTel 的 httpx 埋点里有）。

用途、补检轮次、专家实例和第几步来自调用点打的标签（recorder.model_call_tag），在请求
开始时取一次；记录器也在开始时取，所以结束在别的任务里也记在同一轮。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.events.recorder import current_call_tag, events

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def usage_of(message: Any) -> dict[str, int] | None:
    """回复里的用量；拿不到（流式中途出错、调用方没取最终消息）时 None。"""
    usage = _get(message, "usage") if message is not None else None
    if usage is None:
        return None
    return {
        "input_tokens": int(_get(usage, "input_tokens") or 0),
        "output_tokens": int(_get(usage, "output_tokens") or 0),
        "cache_read_tokens": int(_get(usage, "cache_read_input_tokens") or 0),
        "cache_write_tokens": int(_get(usage, "cache_creation_input_tokens") or 0),
    }


def output_of(message: Any) -> Any:
    """
    回复的原样输出：结构化调用是解析后的 JSON；作答是守卫之前的原始文字加 tool_use 块。
    思考块之类只记类型。
    """
    parsed = _get(message, "parsed_output")
    if parsed is not None:
        return parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
    blocks: list[dict[str, Any]] = []
    for block in _get(message, "content") or []:
        kind = _get(block, "type")
        if kind == "text":
            blocks.append({"type": "text", "text": _get(block, "text") or ""})
        elif kind == "tool_use":
            blocks.append({"type": "tool_use", "id": _get(block, "id"), "name": _get(block, "name"),
                           "input": _get(block, "input")})
        elif kind:
            blocks.append({"type": str(kind)})
    return blocks


class ModelCall:
    """一次模型请求从开始到结束。开始时取用途标签和记录器，`finish` 发 model_call。"""

    def __init__(self, provider: str, kwargs: dict[str, Any], *, stream: bool):
        self.recorder = events()
        self.tag = current_call_tag()
        self.provider = provider
        self.model = str(kwargs.get("model") or "")
        choice = kwargs.get("tool_choice")
        self.tool_choice = _get(choice, "type") if choice else None
        self.stream = stream
        self.started = time.monotonic()
        self.first_token_ms: float | None = None
        self.done = False

    def first_token(self) -> None:
        if self.first_token_ms is None:
            self.first_token_ms = round((time.monotonic() - self.started) * 1000, 1)

    def finish(self, *, status: str, sent: bool, message: Any = None,
               error: BaseException | None = None, reason: str = "") -> None:
        """status 是 ok / error / blocked；reason 是 llm_guard 判出的原因（REASON_*）。只发一次。"""
        if self.done or not self.recorder.recording:
            return
        self.done = True
        try:
            output = output_of(message) if message is not None else None
            blocks = output if isinstance(output, list) else []
            data: dict[str, Any] = {
                "purpose": self.tag.get("purpose", "unknown"),
                "provider": self.provider, "model": self.model, "stream": self.stream,
                "status": status, "sent": sent, "reason": reason,
                "error_type": type(error).__name__ if error is not None else "",
                "latency_ms": round((time.monotonic() - self.started) * 1000, 1),
                "first_token_ms": self.first_token_ms,
                "usage": usage_of(message),
                "stop_reason": str(_get(message, "stop_reason") or "") if message is not None else "",
                "tool_choice": self.tool_choice,
                "tool_uses": sum(1 for b in blocks if b.get("type") == "tool_use"),
                "text_chars": sum(len(b.get("text") or "") for b in blocks if b.get("type") == "text"),
            }
            if "round" in self.tag:
                data["round"] = self.tag["round"]
            body: dict[str, Any] = {}
            if output is not None:
                body["output"] = output
            if error is not None:
                body["error"] = str(error)[:2000]
            self.recorder.emit("model_call", data, body or None,
                               agent=self.tag.get("agent", ""), step=self.tag.get("step"))
        except Exception:
            logger.debug("model_call 没记下来", exc_info=True)


class RecordedStream:
    """
    包在 SDK 流对象外面：截住第一段文字（首字耗时）和 get_final_message（用量和输出）。
    其余属性原样透给里面那个。两个供应商的流对象都有这两个入口，循环本来就依赖它们。
    """

    def __init__(self, inner: Any, call: ModelCall):
        self._inner = inner
        self._call = call
        self.final: Any = None

    @property
    def text_stream(self):
        async def _texts():
            async for text in self._inner.text_stream:
                self._call.first_token()
                yield text
        return _texts()

    async def get_final_message(self) -> Any:
        self.final = await self._inner.get_final_message()
        return self.final

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":          # 还没初始化完就被探属性（copy 之类）：别递归
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __aiter__(self):
        return self._inner.__aiter__()
