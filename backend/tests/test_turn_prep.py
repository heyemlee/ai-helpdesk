"""
准备阶段（agents/turn_prep）和两路预取（tools/turn_tools）脱离 api/main 单独可用。

/chat 走的是 api/main 按进程单例装配的那一份，test_workspace_api 和
test_sql_agent_delegation 已经覆盖。这里直接装配依赖，确认评测之类的调用方
不起 FastAPI、不碰 main 的模块级变量，也能复刻同一个准备阶段。
"""
from app.agents.turn_prep import PreparedTurn, TurnDeps, prepare_turn
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.memory.conversation_memory import MemoryContext, Message, MsgRole
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import ToolResult
from app.tools.turn_tools import Prefetch, delegate_order_query

KNOWLEDGE = [{"title": "补件流程", "content": "缺件补发在核实后安排。", "score": 0.6,
              "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}]
ORDER_DATA = {
    "answer": "SO-2024-006 的补件已于 9 月 15 日出库。", "queried_database": True,
    "execution_status": "success", "has_risk": False, "degraded": False,
    "selected_databases": ["wms_db"], "trace_id": "t-1",
    "steps": [{"database": "wms_db", "success": True}],
}


class _Tools:
    """按工具名返回：知识库给片段，业务数据给一份可作出处的结果。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, params, context=None, **_):
        self.calls.append((name, params))
        data = KNOWLEDGE if name == "knowledge_search" else dict(ORDER_DATA)
        return ToolResult(success=True, data=data, tool_name=name)

    async def search_with_rewrite(self, name, query, top_k=5, context=None):
        self.calls.append((name, {"query": query, "rewrite": True}))
        return ToolResult(success=True, data=KNOWLEDGE, tool_name=name)


class _Intent:
    intent = IntentCategory.MISSING_PART
    intent_group = "missing_part"
    urgency = UrgencyLevel.LOW
    confidence = 0.9
    entities = {"order_id": ["SO-2024-006"]}
    fast_path = True


def _memory(turns: int) -> MemoryContext:
    roles = (MsgRole.USER, MsgRole.ASSISTANT)
    return MemoryContext(
        recent_messages=[Message(role=roles[i % 2], content=f"第 {i} 句") for i in range(turns)],
        relevant_history=[], user_profile={}, summary="",
    )


def _deps(tools, *, outage: str = "", seen: list | None = None, turns: int = 0) -> TurnDeps:
    async def load_memory(user_id, conv_id, query=""):
        return _memory(turns)

    async def recognize_intent(message, history=None):
        if seen is not None:
            seen.append(history)
        return _Intent()

    return TurnDeps(
        load_memory=load_memory,
        recognize_intent=recognize_intent,
        outage_reason=lambda: outage,
        prefetch=Prefetch(tool_manager=tools, sql_agent=SQLAgentClient(base_url="http://x:8000")),
    )


async def test_prepare_turn_runs_on_injected_dependencies():
    tools, seen, reported = _Tools(), [], []
    message = "SO-2024-006 少发的门板补到哪了"
    turn = await prepare_turn(message, user_id="u1", conv_id="c1", request_id="r-1",
                              deps=_deps(tools, seen=seen, turns=7), progress_cb=reported.append)

    assert isinstance(turn, PreparedTurn)
    # 历史来自注入的记忆，只取最近 5 句；意图识别和编排请求拿到的是同一份
    assert [h["content"] for h in seen[0]] == [f"第 {i} 句" for i in range(2, 7)]
    assert turn.request.history == seen[0]
    assert turn.request.request_id == "r-1"

    # 两路都查了：规则通道定性的意图走单次直检，委派带的是可信身份
    assert ("knowledge_search", {"query": message, "top_k": 3}) in tools.calls
    [(_, params)] = [c for c in tools.calls if c[0] == "order_data_query"]
    assert (params["user_id"], params["session_id"]) == ("u1", "c1")

    # 知识和可作出处的业务数据进 grounding_sources，记忆只进上下文
    assert len(turn.request.grounding_sources) == 2
    assert "9 月 15 日" in turn.request.grounding_sources[1]
    assert "第 6 句" in turn.request.context
    assert "第 6 句" not in "".join(turn.request.grounding_sources)
    assert turn.knowledge_used is True
    assert turn.evidence["business_data"]["outcome"] == "evidence"
    assert [p["stage"] for p in reported] == ["helpdesk.understanding", "helpdesk.gathering"]


async def test_prepare_turn_skips_both_lookups_while_the_model_is_unavailable():
    tools = _Tools()
    turn = await prepare_turn("SO-2024-006 补件到哪了", user_id="u1", conv_id="c1", request_id="r",
                              deps=_deps(tools, outage="credential_rejected"))
    assert tools.calls == []
    assert turn.request.grounding_sources == []
    assert turn.evidence["knowledge"]["attempted"] is False
    assert turn.evidence["business_data"] == {
        "enabled": True, "attempted": False, "outcome": "model_unavailable",
        "reason": "credential_rejected",
    }


async def test_prefetch_without_tools_queries_nothing():
    prefetch = Prefetch(tool_manager=None)
    assert prefetch.will_search("柜门关不严怎么调", intent="installation") is False
    assert prefetch.will_query("SO-2024-006 到哪了", intent="order_status") is False
    assert prefetch.data_enabled is False

    knowledge: dict = {}
    data: dict = {}
    assert await prefetch.knowledge_context("柜门关不严怎么调", intent="installation",
                                            trace=knowledge) == ("", False)
    assert await prefetch.order_data_context("SO-2024-006 到哪了", intent="order_status",
                                             trace=data) == ("", False)
    assert knowledge["attempted"] is False
    assert data == {"enabled": False, "attempted": False, "outcome": "not_configured"}


async def test_delegation_itself_has_no_gate():
    """门控只属于规则预取：直接调执行那一半，寒暄也照样委派——要不要查由调用方决定。"""
    tools, trace = _Tools(), {}
    text, is_evidence = await delegate_order_query(tools, "你好", user_id="u1", conv_id="c1",
                                                   trace=trace)
    assert [name for name, _ in tools.calls] == ["order_data_query"]
    assert is_evidence is True and "9 月 15 日" in text
    assert trace["attempted"] is True and trace["outcome"] == "evidence"
