"""
轨迹评测（app/evaluation/trajectory.py）的自测：用例文件、订单查询的桩、打分、汇总，
以及用脚本化假模型把两组成对跑一遍。不调任何真实模型。

契约夹具只在专门校验它的两条用例里读（a2a-contract 仓库在本地时）；找不到就跳过，
除非设了 A2A_CONTRACT_REQUIRED=1——和 test_contract_fixtures.py 同一个约定。
"""
import os

import pytest
from conftest import ScriptedModel, text_step, tool_step

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.agents.tool_loop import LoopConfig
from app.evaluation.trajectory import (
    UNAVAILABLE,
    ArmResult,
    ContractFixtures,
    Need,
    StubToolManager,
    TrajectoryCase,
    TrajectoryHarness,
    contract_dir,
    grade,
    load_cases,
    render_markdown,
    satisfies,
    summarize,
    validate,
)
from app.tools.tool_manager import ToolResult
from app.tools.turn_tools import delegate_order_query

REQUIRED = os.getenv("A2A_CONTRACT_REQUIRED", "").strip().lower() in {"1", "true", "yes", "on"}

OK_PAYLOAD = {
    "route": "database_query", "queried_database": True, "execution_status": "success",
    "has_risk": False, "degraded": False, "degrade_reason": "", "selected_databases": ["erp_db"],
    "trace_id": "t-eval", "steps": [{"database": "erp_db", "success": True, "row_count": 1,
                                     "rows": [{"job_no": "J25-00003", "ship_date": "2026-09-15"}],
                                     "error": None}],
}
NO_DATA_PAYLOAD = {**OK_PAYLOAD, "execution_status": "no_data",
                   "steps": [{"database": "erp_db", "success": True, "row_count": 0, "rows": [], "error": None}]}
KNOWLEDGE = [{"title": "缺件漏发与补件流程", "content": "补件的到货日期以工厂排产确认为准。", "score": 0.6,
              "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}]


def _contract() -> ContractFixtures:
    root = contract_dir()
    if root is None:
        if REQUIRED:
            pytest.fail("A2A_CONTRACT_REQUIRED=1 但找不到 a2a-contract 仓库")
        pytest.skip("本地没有 a2a-contract 仓库")
    return ContractFixtures(root)


class _Knowledge:
    """假的知识库 ToolManager：不管查什么都回同一段，不出网。"""

    async def call(self, name, params, context=None, **_):
        return ToolResult(success=True, data=KNOWLEDGE, tool_name=name)

    async def search_with_rewrite(self, name, query, top_k=5, context=None, **_):
        return ToolResult(success=True, data=KNOWLEDGE, tool_name=name)


# ── 用例文件 ──────────────────────────────────────────────────────────────────

def test_the_case_file_is_well_formed():
    cases, documents = load_cases()
    counts = {c: sum(case.category == c for case in cases) for c in "ABCDEF"}
    assert counts == {"A": 12, "B": 10, "C": 6, "D": 6, "E": 4, "F": 2}
    assert len({case.id for case in cases}) == 40
    # 每条要么有要拿到手的资料，要么有不许做的事：没有约束的用例量不出任何东西
    assert all(c.needs or c.no_model_tools or c.must_not_call or c.answer_must_not for c in cases)
    assert [d["title"] for d in documents] == ["台面日常保养"]


def test_validation_names_what_is_wrong():
    broken = TrajectoryCase(
        id="X1", category="Z", message="m", intent_label="not_an_intent",
        history=[{"role": "system", "content": "x"}], needs=[Need(tool="sql")],
        answer_must_not=["(unclosed"], stubs=[{"match": "J"}],
    )
    problems = "\n".join(validate([broken, broken]))
    for fragment in ("id 重复", "类别 Z", "意图 not_an_intent", "role", "工具 sql", "正则", "fixture"):
        assert fragment in problems


def test_every_stub_fixture_exists_in_the_contract_repo():
    fixtures = _contract()
    cases, _ = load_cases()
    names = {s["fixture"] for c in cases for s in c.stubs if s.get("fixture") not in (None, UNAVAILABLE)}
    assert names and all(fixtures.exists(name) for name in names), names


async def test_stubbed_delegations_go_through_the_same_trust_verdict():
    """桩回的是契约夹具，经过的是线上那一份 delegate_order_query：信任判定一字不差。"""
    stub = StubToolManager(_Knowledge(), _contract(), [
        {"match": "J25-?00003", "fixture": "valid/01-success-single-step.json", "answer": "2026-09-15 出货。"},
        {"match": "失败", "fixture": "trust-negative/07-failed-step-with-status.json"},
        {"match": "超时", "fixture": UNAVAILABLE, "error": "ReadTimeout"},
    ])

    async def outcome(query):
        trace: dict = {}
        _text, is_evidence = await delegate_order_query(stub, query, trace=trace)
        return trace["outcome"], is_evidence

    assert await outcome("J25-00003 出货") == ("evidence", True)
    assert await outcome("故意失败") == ("failed", False)
    assert await outcome("故意超时") == ("unavailable", False)
    assert await outcome("没有单号") == ("unverified", False)      # 都不匹配：下游查了，没查到
    assert stub.queries == ["J25-00003 出货", "故意失败", "故意超时", "没有单号"]


# ── 打分 ──────────────────────────────────────────────────────────────────────

def _entry(actor, tool, outcome, query="", step=None, dedup=False, sources=()):
    return {"actor": actor, "tool": tool, "outcome": outcome, "args": {"query": query},
            "step": step if step is not None else (0 if actor == "rule" else 1), "dedup": dedup,
            "trace": {"sources": [{"title": t} for t in sources]}}


def _arm(case, trajectory, answer="好的。", arm="loop", repeat=0, usage=None, latency=1000.0):
    result = ArmResult(case_id=case.id, category=case.category, repeat=repeat, arm=arm, answer=answer,
                       trajectory=trajectory, usage=usage or {"model_calls": 1}, latency_ms=latency,
                       handoff=False, violations=0, unavailable=False, delegations=[])
    result.grade = grade(case, result, LoopConfig())
    return result


def test_a_need_is_met_by_the_right_document_or_trusted_data():
    doc = Need(tool="knowledge_search", doc="安装与常见现场问题")
    assert satisfies(doc, _entry("rule", "knowledge_search", "found", sources=["安装与常见现场问题"]))
    assert not satisfies(doc, _entry("rule", "knowledge_search", "found", sources=["交期说明"]))
    assert not satisfies(doc, _entry("rule", "knowledge_search", "empty"))
    data = Need(tool="order_data_query")
    assert satisfies(data, _entry("model", "order_data_query", "evidence"))
    assert not satisfies(data, _entry("model", "order_data_query", "unverified"))


def test_grading_a_rule_miss_that_the_model_recovered():
    case = TrajectoryCase(id="B", category="B", message="那补的门板现在到哪了？", intent_label="missing_part",
                          needs=[Need(tool="order_data_query", args_regex="J25-?00003")])
    g = _arm(case, [_entry("rule", "order_data_query", "unverified", "那补的门板现在到哪了？"),
                    _entry("model", "order_data_query", "evidence", "J25-00003 补件进度")]).grade
    assert (g["needs"], g["met"], g["met_by_rule"], g["uncovered"], g["called"], g["args_ok"]) == (1, 1, 0, 1, 1, 1)
    assert (g["model_tool_calls"], g["unnecessary"], g["budget_hit"]) == (1, False, False)


def test_grading_the_things_that_must_not_happen():
    covered = TrajectoryCase(id="A", category="A", message="m", intent_label="order_status",
                             needs=[Need(tool="order_data_query")], no_model_tools=True)
    g = _arm(covered, [_entry("rule", "order_data_query", "evidence"),
                       _entry("model", "knowledge_search", "found")]).grade
    assert g["met_by_rule"] == 1 and g["unnecessary"] is True

    # 复用本轮之前的结果不算多余：没有真的再查一次
    g = _arm(covered, [_entry("rule", "order_data_query", "evidence"),
                       _entry("model", "order_data_query", "evidence", dedup=True)]).grade
    assert g["unnecessary"] is False

    quote = TrajectoryCase(id="D", category="D", message="m", intent_label="quote_pricing",
                           must_not_call=["order_data_query"])
    g = _arm(quote, [_entry("model", "order_data_query", "refused")]).grade
    assert (g["prohibited_attempts"], g["prohibited_executed"]) == (1, 0)
    # 下游这一轮已经不通、被跳过的那次也没有执行
    g = _arm(quote, [_entry("model", "order_data_query", "skipped")]).grade
    assert g["prohibited_executed"] == 0

    failure = TrajectoryCase(id="E", category="E", message="m", intent_label="order_status",
                             answer_must_not=[r"\d{4}-\d{1,2}-\d{1,2}"])
    assert _arm(failure, [], answer="预计 2026-09-15 出货。").grade["fabricated"]
    assert not _arm(failure, [], answer="这次没能查到系统记录，建议人工核实。").grade["fabricated"]

    # 模型在第 2 步还在调工具：第 3 步（最后一步）只能被强制作答
    g = _arm(covered, [_entry("model", "order_data_query", "evidence", step=2)]).grade
    assert g["budget_hit"] is True


def test_summary_pairs_the_two_arms_and_checks_the_gates():
    miss = TrajectoryCase(id="B", category="B", message="m", intent_label="missing_part",
                          needs=[Need(tool="order_data_query")])
    covered = TrajectoryCase(id="A", category="A", message="m", intent_label="order_status",
                             needs=[Need(tool="order_data_query")], no_model_tools=True)
    results = []
    for repeat in range(2):
        results += [
            _arm(miss, [_entry("rule", "order_data_query", "unverified")], arm="rules", repeat=repeat),
            _arm(miss, [_entry("rule", "order_data_query", "unverified"),
                        _entry("model", "order_data_query", "evidence")], arm="loop", repeat=repeat,
                 usage={"model_calls": 2}),
            _arm(covered, [_entry("rule", "order_data_query", "evidence")], arm="rules", repeat=repeat),
            _arm(covered, [_entry("rule", "order_data_query", "evidence")], arm="loop", repeat=repeat,
                 latency=1500.0),
        ]
    summary = summarize(results, [miss, covered])

    assert summary["arms"]["rules"]["coverage_bc"] == 0.0 and summary["arms"]["loop"]["coverage_bc"] == 1.0
    assert summary["paired"]["coverage_bc_diff"] == 1.0
    assert summary["paired"]["a_p50_latency_diff_ms"] == 500.0
    gates = {g["name"]: g["passed"] for g in summary["gates"]}
    assert gates["B、C 类资料到手率提升"] is True
    assert gates["A、D 类多余调用"] is True
    assert gates["A 类作答 p50 延迟增加"] is True
    assert gates["评审分（B、C 不低于规则组，整体降幅 ≤ 0.02）"] is None      # 没跑评审就不下结论

    markdown = render_markdown(summary, {"date": "2026-10-16", "意图来源": "label"})
    assert "B、C 类资料到手率" in markdown and "打开的门槛" in markdown and "| 过 |" in markdown


# ── 两组成对跑一遍（脚本化的假模型）──────────────────────────────────────────

async def test_both_arms_answer_from_one_preparation():
    """准备阶段只做一次；规则组拿不到单号对应的数据，循环组从前文取单号再查。"""
    case = TrajectoryCase(
        id="B01", category="B", message="那补的门板现在到哪了？", intent_label="missing_part",
        history=[{"role": "user", "content": "订单 J25-00003 少发了两块门板。"},
                 {"role": "assistant", "content": "已经帮您登记补件。"}],
        stubs=[{"match": "J25-?00003", "payload": OK_PAYLOAD, "answer": "J25-00003 的补件随 2026-09-15 那批出货。"},
               {"match": "", "payload": NO_DATA_PAYLOAD, "answer": "没有查到匹配的记录。"}],
        needs=[Need(tool="order_data_query", args_regex="J25-?00003")],
    )
    model = ScriptedModel(
        text_step("补件已登记，具体到货时间以工厂排产为准。"),                      # 规则组：单次生成
        tool_step(("call_1", "order_data_query", {"query": "J25-00003 补件进度"})),   # 循环组第 1 步
        text_step("J25-00003 的补件随 2026-09-15 那批出货。"),                        # 循环组第 2 步
    )
    orchestrator = AgentOrchestrator(api_key="test-key")
    for agent in orchestrator._pool[AgentType.ORDER]:
        agent._client = model
    harness = TrajectoryHarness(orchestrator=orchestrator, knowledge=_Knowledge(), intent_source="label")

    rules, loop = await harness.run_case(case)

    assert (rules.arm, loop.arm) == ("rules", "loop")
    assert "tools" not in model.requests[0] and "tools" in model.requests[1]
    # 两组的轨迹都从同一次规则预取开始：预取拿这句原话委派，查不到
    assert rules.trajectory[:2] == loop.trajectory[:2]
    assert [(e["tool"], e["outcome"]) for e in rules.trajectory] == [
        ("knowledge_search", "found"), ("order_data_query", "unverified")]
    assert (rules.grade["met"], rules.grade["called"]) == (0, 0)
    assert (loop.grade["met"], loop.grade["called"], loop.grade["args_ok"]) == (1, 1, 1)
    assert (rules.delegations, loop.delegations) == ([], ["J25-00003 补件进度"])
    assert (rules.usage["model_calls"], loop.usage["model_calls"]) == (1, 2)
    assert "2026-09-15" in loop.answer


async def test_each_category_gets_the_prefetch_it_was_written_for():
    """
    用标注意图把 40 条的准备阶段都跑一遍，核对用例本身有没有写对：A、F 类的数据用例预取
    就拿到可信数据；B 类预取拿不到（这一类的意义就在这里）；E 类预取确实走到了出错的那几种结局。
    截断是个例外：截断的结果照样能当出处，只是带着"不许说总数"的约束——那条用例考的是回答守没
    守住约束（answer_must_not），不是查没查成。知识库那一路接的是假知识库，这里不核对文档。
    """
    cases, _ = load_cases()
    harness = TrajectoryHarness(orchestrator=None, knowledge=_Knowledge(), fixtures=_contract(),
                                intent_source="label")
    wrong = []
    for case in cases:
        prepared, _stub = await harness.prepare(case)
        rule = [e for e in prepared.request.turn.trajectory if e["tool"] == "order_data_query"]
        needs_data = any(n.tool == "order_data_query" for n in case.needs)
        outcome = rule[0]["outcome"] if rule else None
        if case.category in ("A", "F") and needs_data and outcome != "evidence":
            wrong.append(f"{case.id}: 预取应当拿到可信数据，实际 {outcome}")
        if case.category == "B" and outcome == "evidence":
            wrong.append(f"{case.id}: 预取已经拿到数据，这条不算规则漏判")
        truncated = any("truncated" in str(s.get("fixture", "")) for s in case.stubs)
        if case.category == "E" and outcome not in ("failed", "unavailable", "unverified") and not truncated:
            wrong.append(f"{case.id}: 预取应当走到出错的结局，实际 {outcome}")
    assert wrong == []
