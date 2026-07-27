"""
跨仓库契约用例：把 a2a-contract 的 golden fixtures 喂进本仓库的消费路径。

**为什么用例不放在这里维护。** 契约同时约束两侧，用例分叉就是副本漂移换个
地方复发——这份契约已经为此翻过一次车。所以清单只有一份
（`fixtures/index.json`），CI 把契约仓库 checkout 到固定 tag，两侧各读同一份。
**不要把 fixture 拷进本仓库**：拷贝就是副本，副本会漂。

跑之前需要契约仓库在本地。CI 里 checkout 到 `A2A_CONTRACT_DIR`；本地开发时
自动找同级目录的 `../a2a-contract`。找不到就 skip——除非设了
`A2A_CONTRACT_REQUIRED=1`，那时候 skip 变成 fail。CI 必须设它：
一个"契约仓库没拉下来就自动跳过"的门禁等于没有门禁，而且它坏掉的时候
测试报告是绿的。

组别与断言口径见契约仓库的 `CONSUMER-TESTS.md`。对调用方最关键的是：

- **B 组**（schema 反例）：缺字段 / 字符串布尔必须被拒，且**回落方向必须是
  "不可信"**。第二条比第一条更重要——校验失败之后走哪条分支才是真正的安全属性。
- **C 组**（信任反例）：形状完好但数据不能用，**schema 一个都拦不住**，
  只能靠业务判定。
- **D 组**：清单外的合法标识符（`hr_db`）是**常规路径不是异常路径**，
  v1.6 起新增库不算契约变更。

**v1.8 起硬规则由「两条 transport」改成「三个承载者」**（§4.4.1）：
`message/stream` / `message/send` / 直连 `POST /query`。第三个是
`SQL_AGENT_TRANSPORT` 的默认值，也就是**今天跑生产流量的那条**——
在 v1.8 之前它一直在契约的保护范围之外。E 组只适用于 A2A 那两条
（`/query` 同步无事件），这是范围差异不是同构破例。
"""
import json
import os
import pathlib

import pytest

from app.core.evidence import assess, normalize_delegation_result
from app.tools.a2a_client import A2AClient, progress_label

# ── 定位契约仓库 ─────────────────────────────────────────────────────────────

_TRUE = {"1", "true", "yes", "on"}
REQUIRED = os.getenv("A2A_CONTRACT_REQUIRED", "").strip().lower() in _TRUE

# 契约仓库钉在 tag 上，不跟 main（附录 B）。CI 里 checkout 哪个 tag 见
# .github/workflows/ci.yml 的 contract job；改 tag 是一次要过评审的改动。
PINNED_TAG = "v1.11"


def _contract_dir() -> pathlib.Path | None:
    """
    找契约仓库。**显式配置了就只认它**，不回退。

    回退在这里是有害的：`A2A_CONTRACT_DIR` 指错时悄悄换成另一个副本，
    测试照样绿，而绿的是别的东西。宁可找不到就报错。
    """
    explicit = os.getenv("A2A_CONTRACT_DIR", "").strip()
    if explicit:
        path = pathlib.Path(explicit)
        return path if (path / "fixtures" / "index.json").is_file() else None
    # 没配就按本地开发的惯例找：两个仓库并排放在同一个 Projects 目录下
    sibling = pathlib.Path(__file__).resolve().parents[3] / "a2a-contract"
    return sibling if (sibling / "fixtures" / "index.json").is_file() else None


CONTRACT = _contract_dir()

if CONTRACT is None and REQUIRED:
    raise RuntimeError(
        "A2A_CONTRACT_REQUIRED=1 但找不到 a2a-contract。"
        f"把它 checkout 到 tag {PINNED_TAG} 并用 A2A_CONTRACT_DIR 指过去。"
    )

pytestmark = pytest.mark.skipif(
    CONTRACT is None,
    reason=f"找不到 a2a-contract（设 A2A_CONTRACT_DIR 指向 tag {PINNED_TAG} 的检出）",
)


def _load(rel: str) -> dict:
    return json.loads((CONTRACT / rel).read_text(encoding="utf-8"))


CASES = _load("fixtures/index.json")["cases"] if CONTRACT else []
DATA_PART_CASES = [c for c in CASES if c["file"].startswith("data-part/")]
PROGRESS_CASES = [c for c in CASES if c["file"].startswith("progress-event/")]


def _ids(cases):
    return [c["id"] for c in cases]


# ── schema 校验 ──────────────────────────────────────────────────────────────

def _validator_for(schema_rel: str):
    """
    按 fixture 声明的 schema 建一个校验器。

    `artifact-data-part.schema.json` 里 `$ref` 到了 `database-id.schema.json`，
    所以要把 `schema/` 下所有文件登记进 registry，不然 $ref 解析会去联网。
    """
    jsonschema = pytest.importorskip(
        "jsonschema",
        reason="pip install jsonschema referencing（CI 里在 contract job 装）",
    )
    from referencing import Registry, Resource

    resources = []
    for path in sorted((CONTRACT / "schema").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc)
        # 两个名字都登记：$id 的绝对 URI，以及 fixture/schema 里用的相对文件名
        resources.append((doc["$id"], resource))
        resources.append((path.name, resource))
    registry = Registry().with_resources(resources)

    schema = json.loads((CONTRACT / schema_rel).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, registry=registry)


def _schema_errors(case: dict, payload: dict) -> list:
    return list(_validator_for(case["schema"]).iter_errors(payload))


# ── 三个承载者的取出方式（CONSUMER-TESTS 的那张表）──────────────────────────

def _send_artifact(data_part: dict) -> dict:
    """`message/send`：`result.artifacts[0].parts[]` 里 kind == "data" 那一项。"""
    return {
        "artifactId": "a1", "name": "answer",
        "parts": [{"kind": "text", "text": "（答案正文）"}, {"kind": "data", "data": data_part}],
    }


def _stream_artifact(data_part: dict) -> dict:
    """`message/stream`：`artifact-update` 事件的 `result.artifact`。同构（§4.4）。"""
    frame = {"kind": "artifact-update", "taskId": "t1", "contextId": "c1",
             "artifact": _send_artifact(data_part), "lastChunk": True}
    return frame["artifact"]


# `/query` 的逐步骤日志正处在改名的过渡期（契约 §4.4.1）：服务端要把
# `step_logs` 改成 `steps`，过渡期两个同时发。三种形态都必须走通——
# 只认一个名字的实现会在过渡的某一端静默失效：取不到 `steps` 就按
# 「缺失即不可信」处置，**每一次直连查询都不再是出处，而链路看起来完全正常**。
QUERY_SHAPES = ("legacy", "transitional", "renamed")


def _query_response(data_part: dict, shape: str = "transitional") -> dict:
    """
    直连 `/query` 的响应形状：同样的字段在顶层，逐步骤日志的名字按过渡阶段变。

    - `legacy`       —— 只有 `step_logs`（改名之前，也就是今天）；
    - `transitional` —— 两个都发（改名过渡期，契约要求的形态）；
    - `renamed`      —— 只有 `steps`（弃用 `step_logs` 之后，走 §6 变更）。
    """
    out = {k: v for k, v in data_part.items() if k != "steps"}
    if "steps" in data_part:
        if shape in ("legacy", "transitional"):
            out["step_logs"] = data_part["steps"]
        if shape in ("transitional", "renamed"):
            out["steps"] = data_part["steps"]
    out.setdefault("answer", "（答案正文）")
    return out


def _consume_all_bearers(data_part: dict) -> list:
    """
    三个承载者各走一遍（`/query` 再按三种过渡形态各走一遍），返回信任结论。

    A2A 的两条必须**逐字同构**（§4.4）；三个承载者必须得到**同一个业务判定**
    （§4.4.1）——回滚会依次落在 `send` 和 `/query` 上，判定分叉时回滚就是
    静默降级，而那正是 §6.2 反复要防的事。
    """
    send = A2AClient._normalize(_send_artifact(data_part), {})
    stream = A2AClient._normalize(_stream_artifact(data_part), {})
    assert send == stream, "send 与 stream 的归一结果必须逐字相同（§4.4 同构）"
    verdicts = [assess(send), assess(stream)]
    verdicts += [
        assess(normalize_delegation_result(_query_response(data_part, shape)))
        for shape in QUERY_SHAPES
    ]
    return verdicts



# ── A / D 组：正例 ───────────────────────────────────────────────────────────

VALID_DATA_PARTS = [c for c in DATA_PART_CASES if c["expect"] == "valid"]
INVALID_DATA_PARTS = [c for c in DATA_PART_CASES if c["expect"] == "invalid"]


@pytest.mark.parametrize("case", VALID_DATA_PARTS, ids=_ids(VALID_DATA_PARTS))
def test_valid_fixtures_pass_schema(case):
    errors = _schema_errors(case, _load(f"fixtures/{case['file']}"))
    assert not errors, f"{case['id']} 应当过 schema，实际: {[e.message for e in errors]}"


@pytest.mark.parametrize("case", DATA_PART_CASES, ids=_ids(DATA_PART_CASES))
def test_every_data_part_is_judged_the_same_on_every_bearer(case):
    """
    **三个承载者一致**（契约 §4.4.1，v1.8 由两条改为三条）。

    缺字段 / 字符串布尔 / 失败步骤 / 带风险 / 降级 / 截断——每一种形态，
    `message/send`、`message/stream`、直连 `/query`（三种改名过渡形态）
    必须给出同一个业务判定。

    第三个承载者是 v1.8 才纳入的，而它是 `SQL_AGENT_TRANSPORT` 的默认值：
    在此之前，**跑生产流量的那条路一直没有契约用例覆盖**。
    """
    verdicts = _consume_all_bearers(_load(f"fixtures/{case['file']}"))
    first = verdicts[0]
    for other in verdicts[1:]:
        assert other == first, f"{case['id']} 在不同承载者上判定不一致"


# ── B 组：schema 反例必须被拒，且回落方向是"不可信" ─────────────────────────

@pytest.mark.parametrize("case", INVALID_DATA_PARTS, ids=_ids(INVALID_DATA_PARTS))
def test_schema_invalid_fixtures_are_rejected(case):
    """第一条断言：校验必须失败。"""
    errors = _schema_errors(case, _load(f"fixtures/{case['file']}"))
    assert errors, f"{case['id']} 应当被 schema 拒绝，但校验通过了"


@pytest.mark.parametrize("case", INVALID_DATA_PARTS, ids=_ids(INVALID_DATA_PARTS))
def test_schema_invalid_fixtures_fall_back_to_untrusted(case):
    """
    第二条断言，比第一条更重要：**校验失败之后走哪条分支**。

    - 缺 `has_risk` / `degraded` → 判为不可信（**不是**"无风险、未降级"）；
    - `"false"` 这类字符串 → 判为不可信（**不是** `bool("false") == True`）；
    - 三个全缺 → 判为不可信。

    这一组直接对应审查 P0-3 的 (a)(b)：原来的 `bool(data.get(k, False))`
    在这六个载荷上全部会给出"可信"。
    """
    for verdict in _consume_all_bearers(_load(f"fixtures/{case['file']}")):
        assert verdict.usable_as_source is False, f"{case['id']}: {verdict}"


def test_the_two_groups_are_not_both_green_by_writing_the_judgement_dead():
    """
    反过来验一次：一个"永远返回不可信"的实现能过 B 组，但会让 A 组挂。

    CONSUMER-TESTS 专门提醒了这一条。没有它，把 `usable_as_source` 写死成
    False 就能让整份文件变绿——那不是修好了判据，是删掉了判据。
    """
    usable = [c for c in DATA_PART_CASES if c["trust"] == "usable_as_source"]
    assert usable, "清单里至少要有一条正面用例，否则这条自检没有意义"
    for case in usable:
        for verdict in _consume_all_bearers(_load(f"fixtures/{case['file']}")):
            assert verdict.usable_as_source is True, f"{case['id']} 正例被判成了不可信"


# ── C 组：信任反例——schema 通过，只能靠业务判定 ─────────────────────────────

@pytest.mark.parametrize("case", DATA_PART_CASES, ids=_ids(DATA_PART_CASES))
def test_trust_verdict_matches_the_manifest(case):
    """
    每条 fixture 的 `trust` 就是期望的处置，逐条对。

    `partial` 有两条断言而不是一条：**可用**，且**带明确的表述约束**。
    只断言"可用"会漏掉截断那条的真正风险（数据是真的，但"共有 N 条"是假的）；
    只断言"不可用"会把部分成功里那半份真数据一起扔掉，比现状更糟（§4.6.1）。
    """
    expected = case["trust"]
    for verdict in _consume_all_bearers(_load(f"fixtures/{case['file']}")):
        if expected == "usable_as_source":
            assert verdict.usable_as_source is True, f"{case['id']}: {verdict.reasons}"
        elif expected == "not_usable":
            assert verdict.usable_as_source is False, f"{case['id']}: {verdict}"
        elif expected == "partial":
            assert verdict.usable_as_source is True, f"{case['id']} 不该整段丢弃"
            assert verdict.caveats, f"{case['id']} 必须带上表述约束"
        elif expected == "no_data":
            # "查到了，答案是没有" ≠ "没查成"。两者都不能当出处，
            # 但对客户是两句不同的话，所以判定必须能分开。
            assert verdict.execution_status == "no_data", f"{case['id']}: {verdict}"
            assert verdict.usable_as_source is False
        elif expected == "n/a":
            pass
        else:  # pragma: no cover - 清单里出现了新的取值
            pytest.fail(f"{case['id']} 的 trust 取值 {expected!r} 本文件还没处理")


def test_truncated_result_forbids_total_statements():
    """§4.6.3：截断之后任何"共有 N 条 / 全部 / 一共"的说法都不成立。"""
    verdict = _consume_all_bearers(_load("fixtures/data-part/trust-negative/05-truncated.json"))[0]
    assert verdict.truncated is True
    assert any("总量表述" in c for c in verdict.caveats)


def test_partial_success_keeps_the_half_that_worked():
    """§4.6.1：`partial_success` 不能并进 `failed`，那会把真数据一起扔掉。"""
    verdict = _consume_all_bearers(
        _load("fixtures/data-part/trust-negative/02-partial-success.json")
    )[0]
    assert verdict.execution_status == "partial_success"
    assert verdict.usable_as_source is True
    assert verdict.caveats


@pytest.mark.xfail(
    strict=True,
    reason="服务端尚未下发 execution_status（契约 §4.6.1 标 🔨）。"
           "今天这个判定是调用方按 §4.6.1 的判据自己 reduce steps[] 得出的，"
           "§4.6.2 提醒过自己 reduce 的口径会漂。服务端上线后摘掉这个 xfail——"
           "**那才是 P0-5 这条缺口关闭的证据**，不要为了变绿去改断言。",
)
def test_failed_step_verdict_comes_from_the_server_not_from_our_own_reduce():
    """
    dp-trust-01 的业务判定必须由**服务端下发的字段**决定。

    这条用例盯的不是"结论对不对"——结论今天已经对了（见
    `test_trust_verdict_matches_the_manifest`，它是绿的、不许放宽）。
    盯的是**结论从哪来**：`status_source == "derived"` 意味着调用方在替
    服务端定义"查询算不算成功"，而服务端那边 `queried_database` 的定义只有
    一行 `pipeline_result is not None`，两边的口径迟早会分开。
    """
    verdict = _consume_all_bearers(
        _load("fixtures/data-part/trust-negative/01-failed-step.json")
    )[0]
    assert verdict.execution_status == "failed"
    assert verdict.status_source == "contract"


# ── D 组：database 标识符 ────────────────────────────────────────────────────

def test_chinese_labels_cover_every_known_identifier():
    """
    中文映射表必须覆盖 `$defs["known"]`（含两个退役标识符）。

    退役的 `mes_db` / `pm_db` 保留映射是有意的：benchmark 夹具和历史 trace
    里还会出现。**但不要拿 `known` 去断言运行时会发出什么**——那两个不会
    出现在任何部署里。
    """
    from app.tools.a2a_client import DATABASE_LABEL
    known = _load("schema/database-id.schema.json")["$defs"]["known"]["enum"]
    missing = [db for db in known if db not in DATABASE_LABEL]
    assert not missing, f"这些标识符没有中文映射: {missing}"


def test_unlisted_identifier_is_the_normal_path_not_an_error_path():
    """
    dp-valid-04（`hr_db`）：清单外但形状合法的标识符必须**照常采信**。

    v1.6 起 `database` 的契约面是形状 + 一份可拉取的清单，**新增标识符不算
    契约变更**。所以"遇到不认识的库"是常规路径：不得崩溃、不得因为不认识
    就判不可信，中文映射退化为显示原始标识符即可。
    """
    payload = _load("fixtures/data-part/valid/04-unlisted-database.json")
    for verdict in _consume_all_bearers(payload):
        assert verdict.usable_as_source is True, "不认识的库不是不可信的理由"

    label = progress_label("execution", "hr_db")
    assert label == "正在查询（hr_db）", "映射不到就显示原始标识符，不要抛"


def test_progress_label_degrades_for_any_shape_valid_identifier():
    """形状合法的任意标识符都不能把展示路径打挂（同上，锁住退化行为）。"""
    for db in ("hr_db", "fin_db", "x9_db"):
        assert progress_label("execution", db) == f"正在查询（{db}）"


# ── E 组：progress 事件信封 ─────────────────────────────────────────────────

@pytest.mark.parametrize("case", PROGRESS_CASES, ids=_ids(PROGRESS_CASES))
def test_progress_event_fixtures_match_the_schema(case):
    errors = _schema_errors(case, _load(f"fixtures/{case['file']}"))
    if case["expect"] == "valid":
        assert not errors, f"{case['id']}: {[e.message for e in errors]}"
    else:
        assert errors, f"{case['id']} 应当被拒绝，但校验通过了"


@pytest.mark.parametrize("case", [c for c in PROGRESS_CASES if c["expect"] == "valid"],
                         ids=_ids([c for c in PROGRESS_CASES if c["expect"] == "valid"]))
def test_progress_metadata_is_consumed_without_crashing(case):
    """
    调用方消费进度事件时**不得**因为字段缺失抛异常（§4.3「必须容忍缺失」）。

    正例里就有不带 `step` / `database` 的（`01-routing`），这条锁住那个分支。
    """
    meta = _load(f"fixtures/{case['file']}")["result"].get("metadata") or {}
    progress_label(meta.get("stage", ""), meta.get("database"))  # 不抛即可


def test_unknown_stage_is_ignored_not_an_error():
    """
    服务端加一个 stage 不该让调用方挂（§4.2「不要对事件条数做任何假设」同源）。

    `progress_label` 返回 None = 不展示，调用侧据此跳过。**不是**抛异常，
    也不是把原始 stage 名直接显示给客户。
    """
    assert progress_label("a_stage_from_the_future") is None
    assert progress_label("memory_context") is None, "内部 stage 不展示（§4.5）"
