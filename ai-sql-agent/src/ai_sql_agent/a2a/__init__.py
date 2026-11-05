"""
A2A 服务端：把 ai-sql-agent 暴露成一个 Agent（Agent Card + JSON-RPC `/a2a`）。

契约在独立仓库 a2a-contract 的 `a2a-streaming-contract.md`（v1.13，tag v1.13 /
commit be35cfb）。本包实现 `message/send` 与 `message/stream`；`tasks/get`、
`tasks/cancel`、`tasks/resubscribe` 和 push notifications 暂不提供，返回 -32004
（契约 §7）。理由是暂不需要，不是做不到：v1.13 起 pipeline 有取消检查点，调用方
断开后在下一个检查点停下（§5.4，`AI_SQL_CANCEL_ON_DISCONNECT` 默认开）；要主动
取消走服务端自己的 `POST /runs/{run_id}/cancel`，它不在契约面上。

流式进度的唯一来源是 `ai_sql_agent.core.trace.stage()` 的事件汇（契约 §8.2）：
pipeline 侧零改动，所有阶段本来就都穿过那一个上下文管理器。
"""
from .server import router, build_agent_card

__all__ = ["router", "build_agent_card"]
