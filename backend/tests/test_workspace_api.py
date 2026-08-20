"""员工工作台用到的接口：知识文档、SOP、工单通知状态、对话依据。

不启动 lifespan（那会去连 Redis / Postgres）：按 test_api_auth.py 的做法重新
导入 api.main，再把要测的组件换成落在临时目录里的真实现。
"""
import importlib
import io
import json
import logging
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.core.embedder import Embedder
from app.core.skill_loader import SkillManager
from app.storage.vector_store import LocalStore
from app.tickets.notifier import TicketNotifier
from app.tickets.ticket_store import TicketStore
from app.tools.knowledge_base import KnowledgeBase
from app.tools.tool_manager import ToolResult


def _load_main(monkeypatch, api_key: str = ""):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_HELPDESK_API_KEY", api_key)
    monkeypatch.setenv("AI_HELPDESK_PROTECT_CHAT", "false")
    monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
    monkeypatch.delenv("APP_ENV", raising=False)
    sys.modules.pop("app.api.main", None)
    return importlib.import_module("app.api.main")


@pytest.fixture
def main(monkeypatch):
    return _load_main(monkeypatch)


@pytest.fixture
def kb(main, monkeypatch, tmp_path):
    store = LocalStore(db_path=str(tmp_path / "kb.db"))
    knowledge = KnowledgeBase(store=store, embedder=Embedder())
    monkeypatch.setattr(main, "_knowledge_base", knowledge)
    return knowledge


@pytest.fixture
def client(main):
    return TestClient(main.app)


# ── 鉴权：知识、SOP、工单都要员工钥匙 ─────────────────────────────────────────

STAFF_ONLY = [
    ("GET", "/knowledge/documents"),
    ("GET", "/knowledge/documents/doc_x"),
    ("PATCH", "/knowledge/documents/doc_x"),
    ("GET", "/knowledge/categories"),
    ("GET", "/sops"),
    ("POST", "/search"),
    ("POST", "/knowledge/add"),
    ("POST", "/knowledge/upload"),
    ("GET", "/tickets"),
    ("GET", "/tickets/tk_x"),
    ("POST", "/tickets/tk_x/status"),
    ("GET", "/conversations/c1"),
]


@pytest.mark.parametrize("method,path", STAFF_ONLY)
def test_staff_endpoints_reject_anonymous_callers(monkeypatch, method, path):
    """
    配了 AI_HELPDESK_API_KEY 的实例（面向客户的那个就是）上，不带钥匙读不到
    知识文档、SOP 和工单——内部的采购成本规则和处理流程不是公开 FAQ。
    """
    m = _load_main(monkeypatch, api_key="s3cret")
    client = TestClient(m.app)
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"X-API-Key": "wrong"}).status_code == 401
    # 带对钥匙就越过鉴权，落到业务层（组件没初始化 → 503，或请求体不对 → 422）
    assert client.request(method, path, headers={"X-API-Key": "s3cret"}).status_code != 401


# ── 知识文档 ──────────────────────────────────────────────────────────────────

def test_import_then_browse_then_read(client, kb):
    body = "# LTL Delivery Scope\n\nCurbside delivery does not include unloading or installation.\n"
    added = client.post("/knowledge/add", json={"documents": [
        {"title": "fulfillment", "content": body},
        {"title": "blank", "content": "   "},
    ]}).json()
    created, blank = added["documents"]
    assert created["status"] == "created" and created["chunks"] == 1
    assert blank == {"id": None, "title": "blank", "status": "empty", "chunks": 0, "category": ""}
    assert added["added_documents"] == 1 and added["added_chunks"] == 1
    assert (added["total_documents"], added["total_chunks"]) == (1, 1)

    listing = client.get("/knowledge/documents").json()
    [doc] = listing["documents"]
    assert doc["id"] == created["id"]
    assert doc["display_title"] == "LTL Delivery Scope"
    assert doc["excerpt"].startswith("Curbside delivery")
    assert (doc["source_type"], doc["origin"]) == ("manual", "original")
    assert "content" not in doc                      # 列表不带全文

    detail = client.get(f"/knowledge/documents/{created['id']}").json()
    assert detail["content"] == body
    assert detail["body"] == "Curbside delivery does not include unloading or installation.\n"
    assert [p["index"] for p in detail["passages"]] == [0]

    again = client.post("/knowledge/add", json={"documents": [
        {"title": "fulfillment", "content": body},
    ]}).json()
    assert again["documents"][0] == {
        "id": created["id"], "title": "fulfillment", "status": "duplicate", "chunks": 0,
        "category": "",
    }
    assert again["added_documents"] == 0 and again["total_documents"] == 1

    assert client.get("/knowledge/documents/doc_missing").status_code == 404
    assert client.get("/knowledge/stats").json() == {
        "total_documents": 1, "legacy_documents": 0, "total_chunks": 1,
    }


def test_filter_distinguishes_no_match_from_empty_library(client, kb):
    empty = client.get("/knowledge/documents").json()
    assert empty["documents"] == [] and empty["total_documents"] == 0

    client.post("/knowledge/add", json={"documents": [
        {"title": "warranty", "content": "Check the written limited warranty for the order."},
    ]})
    hit = client.get("/knowledge/documents", params={"q": "  WRITTEN   warranty "}).json()
    assert hit["query"] == "WRITTEN warranty" and hit["matched"] == 1
    assert hit["documents"][0]["match"]["title"] is False
    assert "written limited warranty" in hit["documents"][0]["match"]["snippet"]

    miss = client.get("/knowledge/documents", params={"q": "lifetime"}).json()
    assert miss["documents"] == [] and miss["matched"] == 0
    assert miss["total_documents"] == 1              # 库不是空的，只是没匹配上


def test_upload_keeps_the_file_name_as_source(client, kb):
    resp = client.post("/knowledge/upload", files={
        "file": ("C:\\docs\\Install Guide.md", io.BytesIO("# Hinge\n\n先查柜体水平。".encode()), "text/markdown"),
    })
    assert resp.status_code == 200
    [doc] = resp.json()["documents"]
    detail = client.get(f"/knowledge/documents/{doc['id']}").json()
    assert detail["title"] == "Install Guide"
    assert (detail["source_type"], detail["source_name"]) == ("upload", "Install Guide.md")

    batch = json.dumps([{"title": "a", "content": "甲"}, {"title": "b", "content": "乙"}])
    resp = client.post("/knowledge/upload", files={"file": ("batch.JSON", batch.encode(), "application/json")})
    assert [d["status"] for d in resp.json()["documents"]] == ["created", "created"]
    assert resp.json()["total_documents"] == 3


def test_documents_are_filed_and_moved_through_the_api(client, kb):
    added = client.post("/knowledge/add", json={"documents": [
        {"title": "fee", "content": "送货费按距离计。", "category": "delivery"},
        {"title": "hinge", "content": "铰链三个方向可调。", "category": "安装与验收"},
        {"title": "loose", "content": "还没归类。"},
    ]}).json()
    assert [d["category"] for d in added["documents"]] == ["delivery", "installation", ""]

    overview = client.get("/knowledge/categories").json()
    assert [c["key"] for c in overview["categories"]][:3] == ["products", "design", "orders"]
    counts = {c["key"]: c["documents"] for c in overview["categories"]}
    assert (counts["delivery"], counts["installation"], overview["uncategorized"]) == (1, 1, 1)
    assert next(c for c in overview["categories"] if c["key"] == "delivery")["name"] == "送货与收货"

    delivery = client.get("/knowledge/documents", params={"category": "delivery"}).json()
    assert [d["title"] for d in delivery["documents"]] == ["fee"] and delivery["category"] == "delivery"
    loose = client.get("/knowledge/documents", params={"category": "uncategorized"}).json()
    assert [d["title"] for d in loose["documents"]] == ["loose"]
    everything = client.get("/knowledge/documents", params={"category": ""}).json()
    assert everything["category"] is None and everything["matched"] == 3

    moved = client.patch(f"/knowledge/documents/{added['documents'][2]['id']}",
                         json={"category": "售后与保修"})
    assert moved.status_code == 200 and moved.json()["category"] == "aftersales"
    assert client.get("/knowledge/categories").json()["uncategorized"] == 0

    wrong = client.patch(f"/knowledge/documents/{added['documents'][2]['id']}", json={"category": "售后"})
    assert wrong.status_code == 400 and "售后与保修" in wrong.json()["detail"]
    assert client.patch("/knowledge/documents/doc_missing", json={"category": ""}).status_code == 404


def test_a_wrong_category_rejects_the_whole_batch(client, kb):
    resp = client.post("/knowledge/add", json={"documents": [
        {"title": "ok", "content": "没问题的一篇。", "category": "orders"},
        {"title": "typo", "content": "分类拼错了。", "category": "送货"},
    ]})
    assert resp.status_code == 400
    assert "《typo》" in resp.json()["detail"] and "没有导入任何文档" in resp.json()["detail"]
    assert client.get("/knowledge/documents").json()["total_documents"] == 0


def test_upload_files_documents_under_the_chosen_category(client, kb):
    resp = client.post("/knowledge/upload", data={"category": "installation"}, files={
        "file": ("hinge.md", io.BytesIO("# Hinge\n\n先查柜体水平。".encode()), "text/markdown"),
    })
    assert resp.status_code == 200 and resp.json()["documents"][0]["category"] == "installation"

    # JSON 里每篇自己写的分类优先，没写的用表单里选的
    batch = json.dumps([
        {"title": "a", "content": "甲", "category": "报价与订单"},
        {"title": "b", "content": "乙"},
    ])
    resp = client.post("/knowledge/upload", data={"category": "products"},
                       files={"file": ("batch.json", batch.encode(), "application/json")})
    assert [d["category"] for d in resp.json()["documents"]] == ["orders", "products"]

    bad = json.dumps([{"title": "c", "content": "丙", "category": "nope"}])
    resp = client.post("/knowledge/upload", files={"file": ("bad.json", bad.encode(), "application/json")})
    assert resp.status_code == 400 and "nope" in resp.json()["detail"]
    resp = client.post("/knowledge/upload", data={"category": "nope"},
                       files={"file": ("x.md", b"# X\n\nbody", "text/markdown")})
    assert resp.status_code == 400
    assert client.get("/knowledge/documents").json()["total_documents"] == 3


@pytest.mark.parametrize("payload", ['{"title": "not a list"}', '["just", "strings"]', "{broken"])
def test_upload_rejects_malformed_json(client, kb, payload):
    resp = client.post("/knowledge/upload", files={"file": ("bad.json", payload.encode(), "application/json")})
    assert resp.status_code == 400
    assert client.get("/knowledge/documents").json()["total_documents"] == 0


def test_search_results_name_their_document(client, kb, main, monkeypatch):
    [doc] = client.post("/knowledge/add", json={"documents": [
        {"title": "install", "content": "# Door Troubleshooting\n\nCheck cabinet level before adjusting hinges."},
    ]}).json()["documents"]

    class _Tools:
        async def search_with_rewrite(self, name, query, top_k=5, context=None):
            hits = await kb.search_async(query, top_k=top_k)
            return ToolResult(success=True, data=hits, tool_name=name, reranked=False, evidence_ok=True)

    monkeypatch.setattr(main, "_tool_manager", _Tools())
    result = client.post("/search", json={"query": "adjusting hinges"}).json()
    [hit] = result["results"]
    assert hit["doc_id"] == doc["id"]
    assert hit["doc_title"] == "Door Troubleshooting"     # 文档自己的标题，不是文件名
    assert hit["title"] == "install"
    assert result["evidence_ok"] is True


# ── SOP ───────────────────────────────────────────────────────────────────────

def test_sops_are_readable_in_full_without_leaking_server_paths(client, main, monkeypatch, tmp_path):
    skill_dir = tmp_path / "skills" / "order_logistics"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Orders and Freight\ndescription: Freight coordination\n"
        "keywords: order,LTL\nagents: order\n---\n\n"
        "没有确认的 carrier appointment 不报准确到货日。\n",
        encoding="utf-8",
    )
    manager = SkillManager(root_dir=str(tmp_path / "skills"))
    manager.load()
    monkeypatch.setattr(main, "_skill_manager", manager)

    payload = client.get("/sops").json()
    assert payload["count"] == 1 and payload["errors"] == 0
    [sop] = payload["sops"]
    assert sop["name"] == "Orders and Freight"
    assert sop["agents"] == ["order"] and sop["keywords"] == ["order", "LTL"]
    assert "carrier appointment" in sop["content"]
    assert sop["source"] == "order_logistics/SKILL.md"
    assert str(tmp_path) not in json.dumps(payload)
    assert sop["modified_at"] == pytest.approx(time.time(), abs=120)


# ── 对话依据 ──────────────────────────────────────────────────────────────────

class _KnowledgeTools:
    """只实现 /chat 准备阶段用到的两个检索入口。"""

    def __init__(self, items, **flags):
        self._items, self._flags = items, flags
        self.calls: list[str] = []

    async def call(self, name, params, context=None, **_):
        self.calls.append("direct")
        return ToolResult(success=True, data=self._items, tool_name=name)

    async def search_with_rewrite(self, name, query, top_k=5, context=None):
        self.calls.append("rewrite")
        return ToolResult(success=True, data=self._items, tool_name=name, **self._flags)


async def test_knowledge_trace_lists_only_passages_that_reached_the_prompt(main, kb, monkeypatch):
    [doc] = await kb.import_documents([{
        "title": "invoice", "content": "# Invoices and Change Orders\n\n更正由财务提供 revised invoice。",
    }])
    passage = (await kb.get_document(doc["id"]))["passages"][0]
    items = [
        {"title": "invoice", "content": passage["content"], "score": 0.42, "chunk": 0,
         "retrieval": "hybrid", "doc_id": doc["id"], "chunk_id": passage["id"]},
        {"title": "empty", "content": "  ", "score": 0.4, "doc_id": "doc_x", "chunk_id": "c"},
        {"title": "second", "content": "第二段。", "score": 0.3, "chunk": 1,
         "retrieval": "dense", "doc_id": "", "chunk_id": "c2"},
        {"title": "fourth", "content": "排在 top_k 之外。", "score": 0.1, "doc_id": "", "chunk_id": "c4"},
    ]
    monkeypatch.setattr(main, "_tool_manager", _KnowledgeTools(items, reranked=True, evidence_ok=True))

    trace: dict = {}
    text, used = await main._build_knowledge_context("invoice 地址错了怎么更正", intent="billing", trace=trace)
    assert used is True and "revised invoice" in text
    assert trace["attempted"] is True and trace["mode"] == "rewrite_rerank"
    assert (trace["reranked"], trace["corrected"], trace["evidence_ok"]) == (True, False, True)

    first, second = trace["sources"]            # 空内容的那条和 top_k 之外的那条都不在
    assert first == {
        "doc_id": doc["id"], "chunk_id": passage["id"], "title": "invoice",
        "doc_title": "Invoices and Change Orders", "passage": 0, "score": 0.42,
        "retrieval": "hybrid", "excerpt": "更正由财务提供 revised invoice。",   # 不带标题行
    }
    assert second["doc_title"] == "second"      # 没有文档归属的片段退回它自己的标题
    assert "排在 top_k 之外" not in text


async def test_knowledge_trace_for_fast_path_and_skipped_turns(main, monkeypatch):
    tools = _KnowledgeTools([{"title": "t", "content": "正文", "score": 0.5}])
    monkeypatch.setattr(main, "_tool_manager", tools)

    trace: dict = {}
    assert (await main._build_knowledge_context("订单到哪了", intent="order_status",
                                                fast_path=True, trace=trace))[1] is True
    assert trace["mode"] == "direct" and tools.calls == ["direct"]

    skipped: dict = {}
    assert await main._build_knowledge_context("你好", intent="greeting", trace=skipped) == ("", False)
    assert skipped["attempted"] is False and skipped["sources"] == []
    assert tools.calls == ["direct"]            # 寒暄不检索


async def test_source_excerpt_marks_where_it_was_cut(main, monkeypatch):
    """
    摘录只取段落开头 240 字。切断的地方要看得出来是切断——练习库里那一段的依据句
    （"Curbside delivery does not include …"）正好排在 240 字之后，硬切出来是
    "…Curbsi"，读的人会以为库里存的段落就是坏的。
    """
    long_passage = "# Order Status and Delivery\n\n" + "模拟业务流程说明。" * 30 + "Curbside delivery does not include unloading."
    items = [
        {"title": "fulfillment", "content": long_passage, "score": 0.4, "chunk": 0},
        {"title": "short", "content": "Curbside delivery only.", "score": 0.3, "chunk": 0},
    ]
    monkeypatch.setattr(main, "_tool_manager", _KnowledgeTools(items))
    trace: dict = {}
    await main._build_knowledge_context("curbside delivery 包含卸货吗", intent="delivery", trace=trace)

    cut, whole = (s["excerpt"] for s in trace["sources"])
    assert cut.endswith("…") and len(cut) <= 241
    assert cut.startswith("模拟业务流程说明。") and "Order Status" not in cut   # 标题行照旧不进摘录
    assert whole == "Curbside delivery only."                                  # 没切就不加省略号


async def test_degraded_placeholder_is_never_shown_as_a_source(main, monkeypatch):
    """知识库不可用时工具返回一条兜底文案。它不是知识，不能出现在"回答依据"里。"""
    fallback = [{"title": "知识库降级结果", "content": "知识库暂时不可用", "score": 0.0, "fallback": True}]
    monkeypatch.setattr(main, "_tool_manager", _KnowledgeTools(fallback))
    trace: dict = {}
    await main._build_knowledge_context("保修多久", intent="query", trace=trace)
    assert trace["sources"] == [] and trace["degraded"] is True


async def test_evidence_fields_are_shared_by_chat_and_stream(main):
    fields = main._evidence_fields({
        "knowledge": {"attempted": True, "mode": "direct", "sources": [{"doc_id": "d"}]},
        "business_data": {"enabled": True, "attempted": True, "outcome": "evidence"},
    })
    assert fields == {
        "knowledge_sources": [{"doc_id": "d"}],
        "knowledge": {"attempted": True, "mode": "direct"},
        "business_data": {"enabled": True, "attempted": True, "outcome": "evidence"},
    }
    assert main._evidence_fields({}) == {"knowledge_sources": [], "knowledge": {}, "business_data": {}}
    # 响应模型认得这些字段，缺省是空
    response = main.ChatResponse(conv_id="c", response="r", intent="other", agent_type="general",
                                 escalated=False, latency_ms=1.0, **fields)
    assert response.knowledge_sources == [{"doc_id": "d"}]
    assert main.ChatResponse(conv_id="c", response="r", intent="other", agent_type="general",
                             escalated=False, latency_ms=1.0).business_data == {}


class _DataTools:
    def __init__(self, data, success=True):
        self._data, self._success = data, success

    async def call(self, name, params, context=None, **_):
        return ToolResult(success=self._success, data=self._data, tool_name=name)


OK_DATA = {
    "answer": "WO-3001 处于 Edge banding。", "queried_database": True,
    "execution_status": "success", "has_risk": False, "degraded": False,
    "selected_databases": ["mes_db", "erp_db"], "trace_id": "t-9",
    "steps": [{"database": "mes_db", "success": True}],
}


async def test_business_data_trace_reports_what_actually_happened(main, monkeypatch):
    from app.tools.sql_agent_client import SQLAgentClient

    async def outcome(data, success=True, message="订单 SO-2024-001 到哪个工序了", intent="order_status"):
        monkeypatch.setattr(main, "_tool_manager", _DataTools(data, success))
        trace: dict = {}
        _text, is_evidence = await main._build_order_data_context(message, intent=intent, trace=trace)
        return trace, is_evidence

    # 这个实例没接业务数据：说没接，不说"没查到"
    monkeypatch.setattr(main, "_sql_agent", None)
    trace, _ = await outcome(OK_DATA)
    assert trace == {"enabled": False, "attempted": False, "outcome": "not_configured"}

    monkeypatch.setattr(main, "_sql_agent", SQLAgentClient(base_url="http://x:8000"))
    trace, _ = await outcome(OK_DATA, message="你好", intent="greeting")
    assert trace == {"enabled": True, "attempted": False, "outcome": "skipped"}

    trace, is_evidence = await outcome(dict(OK_DATA))
    assert is_evidence is True
    assert trace["outcome"] == "evidence" and trace["attempted"] is True
    assert trace["databases"] == ["mes_db", "erp_db"] and trace["trace_id"] == "t-9"

    # 带风险标记：数据进上下文，但不算依据——trace 和返回值必须是同一个结论
    trace, is_evidence = await outcome(dict(OK_DATA, has_risk=True))
    assert is_evidence is False and trace["outcome"] == "unverified" and trace["has_risk"] is True

    trace, is_evidence = await outcome({"unavailable": True, "error": "ReadTimeout"}, success=False)
    assert is_evidence is False
    assert (trace["outcome"], trace["reason"]) == ("unavailable", "ReadTimeout")

    trace, _ = await outcome(dict(OK_DATA, queried_database=False, execution_status="not_applicable"))
    assert trace["outcome"] == "not_applicable"

    trace, _ = await outcome(dict(OK_DATA, answer="  "))
    assert trace["outcome"] == "empty"


# ── 准备阶段的进度 ────────────────────────────────────────────────────────────

class _Memory:
    async def get_context(self, user_id, conv_id, query=""):
        class _Ctx:
            recent_messages: list = []

            def to_prompt_text(self):
                return ""
        return _Ctx()


class _Orchestrator:
    def __init__(self, intent):
        self._intent = intent

    async def recognize_intent(self, message, history=None):
        from app.core.intent_recognizer import IntentCategory, UrgencyLevel

        class _Intent:
            intent = IntentCategory(self._intent)
            intent_group = self._intent
            urgency = UrgencyLevel.LOW
            confidence = 0.9
            entities: dict = {}
            source_scores: dict = {}
            strategy = "ensemble"
            fast_path = False
            latency_ms = 1.0
        return _Intent()


async def _stages(main, monkeypatch, message, intent, *, sql_agent):
    """跑一遍准备阶段，返回 (报出去的进度, 实际发生的依据记录)。"""
    from app.tools.sql_agent_client import SQLAgentClient

    monkeypatch.setattr(main, "_memory", _Memory())
    monkeypatch.setattr(main, "_orchestrator", _Orchestrator(intent))
    monkeypatch.setattr(main, "_tool_manager", _DataTools(dict(OK_DATA)))
    monkeypatch.setattr(main, "_sql_agent",
                        SQLAgentClient(base_url="http://x:8000") if sql_agent else None)
    reported: list[dict] = []
    req = main.ChatRequest(message=message, user_id="u1")
    _orch_req, _intent, _used, trace = await main._prepare_request(
        req, "conv-1", "u1", progress_cb=reported.append)
    return reported, trace


@pytest.mark.parametrize("message, intent, sql_agent, gathering", [
    # 只问知识：最常见的一类，以前在出第一个字之前流里一条进度都没有
    ("柜门关不严怎么排查", "installation", False, {"knowledge": True, "order_data": False}),
    # 订单问题且接了业务数据：两路并发，合成一条报
    ("订单 SO-2024-001 到哪个工序了", "order_status", True, {"knowledge": True, "order_data": True}),
    # 同一个问题，这个实例没接业务数据：不说在查订单
    ("订单 SO-2024-001 到哪个工序了", "order_status", False, {"knowledge": True, "order_data": False}),
])
async def test_preparation_reports_what_it_is_about_to_do(main, monkeypatch, message, intent,
                                                          sql_agent, gathering):
    reported, trace = await _stages(main, monkeypatch, message, intent, sql_agent=sql_agent)

    assert [p["stage"] for p in reported] == ["helpdesk.understanding", "helpdesk.gathering"]
    assert all(p["label"] for p in reported)            # 不认识 stage 的旧前端还有话可显示
    assert {k: reported[1][k] for k in ("knowledge", "order_data")} == gathering
    # 报的是"要做什么"，必须和随后真的做了什么对得上——两处用的是同一对门控
    assert trace["knowledge"]["attempted"] is gathering["knowledge"]
    assert trace["business_data"]["attempted"] is gathering["order_data"]


async def test_preparation_does_not_announce_a_lookup_that_will_not_happen(main, monkeypatch):
    """寒暄既不检索也不查库：只报"在理解问题"，不报一条什么都没查的"正在查找资料"。"""
    reported, trace = await _stages(main, monkeypatch, "你好", "greeting", sql_agent=True)
    assert [p["stage"] for p in reported] == ["helpdesk.understanding"]
    assert trace["knowledge"]["attempted"] is False and trace["business_data"]["attempted"] is False


@pytest.mark.parametrize("switch, tools", [
    (None, None),                                            # 默认关闭：编排请求上不挂 TurnContext
    ("on", ["knowledge_search", "order_data_query"]),
])
async def test_the_tool_loop_is_wired_only_when_switched_on(monkeypatch, switch, tools):
    from app.tools.sql_agent_client import SQLAgentClient

    if switch is None:
        monkeypatch.delenv("AI_HELPDESK_TOOL_LOOP", raising=False)
    else:
        monkeypatch.setenv("AI_HELPDESK_TOOL_LOOP", switch)
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, "_memory", _Memory())
    monkeypatch.setattr(main, "_orchestrator", _Orchestrator("order_status"))
    monkeypatch.setattr(main, "_tool_manager", _DataTools(dict(OK_DATA)))
    monkeypatch.setattr(main, "_sql_agent", SQLAgentClient(base_url="http://x:8000"))

    req = main.ChatRequest(message="订单 SO-2024-001 到哪个工序了", user_id="u1")
    orch_req = (await main._prepare_request(req, "conv-1", "u1"))[0]
    if tools is None:
        assert orch_req.turn is None
    else:
        assert sorted(orch_req.turn.tools) == tools
        assert (orch_req.turn.user_id, orch_req.turn.conv_id) == ("u1", "conv-1")
        assert [r["tool"] for r in orch_req.turn.trajectory] == ["knowledge_search", "order_data_query"]


class _Log:
    """记下 logger 收到的话。导入 api.main 会重设根 logger 的 handler，caplog 在这里接不到。"""

    def __init__(self):
        self.lines: list[tuple[int, str]] = []

    def info(self, msg, *args):
        self.lines.append((logging.INFO, msg % args))

    def warning(self, msg, *args):
        self.lines.append((logging.WARNING, msg % args))


@pytest.mark.parametrize("deadline, logged, warned", [
    (None, "墙钟上限 127 秒（按慢工具的超时推算；只能查知识库的专家是 75 秒）", ""),
    ("75", "墙钟上限 75 秒（显式配置；只能查知识库的专家是 75 秒）", "每次都会被拒"),
    ("120", "墙钟上限 120 秒（显式配置", "只给调 order_data_query 之前的步骤留了 13 秒"),
])
def test_startup_states_the_loop_deadline_and_warns_when_queries_cannot_fit(monkeypatch, deadline, logged, warned):
    """轨迹试跑：SQL_AGENT_TIMEOUT_S=90 配上固定的 75 秒，模型发起的查库一次也跑不成，而且没人知道。"""
    from app.tools.sql_agent_client import SQLAgentClient

    monkeypatch.setenv("AI_HELPDESK_TOOL_LOOP", "on")
    monkeypatch.setenv("SQL_AGENT_TIMEOUT_S", "90")
    if deadline is None:
        monkeypatch.delenv("AI_HELPDESK_TOOL_LOOP_DEADLINE_S", raising=False)
    else:
        monkeypatch.setenv("AI_HELPDESK_TOOL_LOOP_DEADLINE_S", deadline)
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, "_tool_manager", _DataTools(dict(OK_DATA)))
    monkeypatch.setattr(main, "_sql_agent", SQLAgentClient(base_url="http://x:8000"))
    log = _Log()
    monkeypatch.setattr(main, "logger", log)

    main._log_loop_config()
    assert any(logged in line for level, line in log.lines if level == logging.INFO)
    warnings = [line for level, line in log.lines if level == logging.WARNING]
    if warned:
        assert len(warnings) == 1 and warned in warnings[0]
    else:
        assert warnings == []


def test_startup_says_nothing_about_the_loop_when_it_is_off(monkeypatch):
    monkeypatch.delenv("AI_HELPDESK_TOOL_LOOP", raising=False)
    main = _load_main(monkeypatch)
    log = _Log()
    monkeypatch.setattr(main, "logger", log)
    main._log_loop_config()
    assert log.lines == []


async def test_progress_is_optional_and_never_breaks_the_turn(main, monkeypatch):
    # 非流式没有进度队列
    monkeypatch.setattr(main, "_memory", _Memory())
    monkeypatch.setattr(main, "_orchestrator", _Orchestrator("installation"))
    monkeypatch.setattr(main, "_tool_manager", _DataTools(dict(OK_DATA)))
    monkeypatch.setattr(main, "_sql_agent", None)
    req = main.ChatRequest(message="柜门关不严怎么排查", user_id="u1")
    assert (await main._prepare_request(req, "c", "u1"))[0].message == req.message

    # 队列坏了（消费方已经断开之类）：这一轮照常准备完
    def broken(_event):
        raise RuntimeError("queue closed")
    assert (await main._prepare_request(req, "c", "u1", progress_cb=broken))[0].message == req.message


# ── 工单：落库、流转、通知是三件事 ────────────────────────────────────────────

class _Result:
    def __init__(self, handoff):
        self.handoff = handoff


def test_ticket_fields_never_claim_more_than_the_backend_knows(main, monkeypatch):
    assert main._ticket_fields(_Result(False), None) == {
        "ticket_id": None, "ticket_status": "", "ticket_notification": "",
    }
    # 判了转人工但工单没建成：要说出来，不能显示成"已转人工"
    assert main._ticket_fields(_Result(True), None)["ticket_status"] == "not_created"

    monkeypatch.setattr(main, "_ticket_notifier", None)
    assert main._ticket_fields(_Result(True), "tk_1") == {
        "ticket_id": "tk_1", "ticket_status": "open", "ticket_notification": "not_configured",
    }
    monkeypatch.setattr(main, "_ticket_notifier", TicketNotifier(object(), "http://hook.invalid"))
    assert main._ticket_fields(_Result(True), "tk_1")["ticket_notification"] == "queued"


@pytest.fixture
async def tickets(main, monkeypatch, tmp_path):
    store = TicketStore(db_path=str(tmp_path / "tickets.db"))
    monkeypatch.setattr(main, "_ticket_store", store)
    monkeypatch.setattr(main, "_ticket_notifier", None)
    yield store
    await store.close()


async def test_ticket_payload_carries_transitions_and_notification_state(client, tickets, main, monkeypatch):
    ticket = await tickets.create(conv_id="c1", user_id="u1", reason="用户明确要求人工",
                                  intent="human_handoff", last_message="台面开裂，我要人工处理。")

    listing = client.get("/tickets").json()
    assert listing["notifications_enabled"] is False
    [row] = listing["tickets"]
    assert row["id"] == ticket.id and row["status"] == "open"
    assert row["allowed_transitions"] == ["assigned", "resolved", "cancelled"]
    assert row["notification"] is None          # 没配通知：没有投递记录，不是"已通知"

    # 通知配了并入了队：pending。送达之后才是 delivered。
    monkeypatch.setattr(main, "_ticket_notifier", TicketNotifier(tickets, "http://hook.invalid"))
    delivery = await tickets.notify_enqueue(ticket.id, "http://hook.invalid", "{}")
    detail = client.get(f"/tickets/{ticket.id}").json()
    assert detail["notifications_enabled"] is True
    assert detail["notification"]["status"] == "pending" and detail["notification"]["attempts"] == 0

    await tickets.notify_mark(delivery.id, "failed", attempts=5, last_error="HTTP 404: gone")
    failed = client.get(f"/tickets/{ticket.id}").json()["notification"]
    assert (failed["status"], failed["attempts"], failed["last_error"]) == ("failed", 5, "HTTP 404: gone")

    await tickets.notify_mark(delivery.id, "delivered", attempts=1)
    assert client.get("/tickets", params={"status": "open"}).json()["tickets"][0]["notification"]["status"] \
        == "delivered"


async def test_status_changes_go_through_the_state_machine(client, tickets):
    ticket = await tickets.create(conv_id="c1", user_id="u1", reason="低置信度")

    assigned = client.post(f"/tickets/{ticket.id}/status", json={"status": "assigned"}).json()
    assert assigned["status"] == "assigned"
    assert assigned["allowed_transitions"] == ["open", "resolved", "cancelled"]

    resolved = client.post(f"/tickets/{ticket.id}/status", json={"status": "resolved"}).json()
    assert resolved["status"] == "resolved" and resolved["allowed_transitions"] == []

    # 终态不能回流：接口拒绝，工单保持原状态
    rejected = client.post(f"/tickets/{ticket.id}/status", json={"status": "open"})
    assert rejected.status_code == 422
    assert client.get(f"/tickets/{ticket.id}").json()["status"] == "resolved"
    assert client.post(f"/tickets/{ticket.id}/status", json={"status": "bogus"}).status_code == 422
    assert client.post("/tickets/tk_missing/status", json={"status": "assigned"}).status_code == 404

    assert [t["id"] for t in client.get("/tickets", params={"status": "resolved"}).json()["tickets"]] \
        == [ticket.id]
    assert client.get("/tickets", params={"status": "open"}).json()["tickets"] == []


def test_health_reports_what_this_instance_is_connected_to(main, monkeypatch):
    """/health 的 capabilities 是工作台"能查什么"说明的依据。"""
    from app.tools.sql_agent_client import SQLAgentClient

    class _Orchestrator:
        def get_stats(self):
            return {}

        def routing_config(self):
            return {}

    monkeypatch.setattr(main, "_orchestrator", _Orchestrator())
    monkeypatch.setattr(main, "_ticket_notifier", None)
    monkeypatch.setattr(main, "_sql_agent", None)
    monkeypatch.setitem(main._llm_info, "model", "claude-test")
    client = TestClient(main.app)
    health = client.get("/health").json()
    assert health["capabilities"] == {"business_data": False, "ticket_notifications": False}
    assert health["llm"]["model"] == "claude-test"

    monkeypatch.setattr(main, "_sql_agent", SQLAgentClient(base_url="http://x:8000"))
    assert client.get("/health").json()["capabilities"]["business_data"] is True
