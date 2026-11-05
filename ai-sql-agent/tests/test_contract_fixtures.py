"""
跨仓库契约用例（CONSUMER-TESTS.md 的 A/B/C/D/E/F 六组）。

**fixtures 不拷进本仓库。** 拷贝就是副本，副本会漂，这份契约已经为此翻过一次车
（契约附录 B）。CI 把 a2a-contract checkout 到固定 tag，这里按
`fixtures/index.json` 消费——用例清单只有一份，在契约仓库里。

契约仓库位置按这个顺序找：`A2A_CONTRACT_DIR` → `./.contract` → `../a2a-contract`。
一个都没有就整组跳过（本地开发不必 clone 契约仓库），CI 里那一步是必跑的。
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from ai_sql_agent.api.trust import assess, execution_status_of
from ai_sql_agent.core import get_settings

CONTRACT_TAG = "v1.13"
"""CI 钉在这个 tag 上。契约仓库改了版本，这里也要跟着改——那是一次有意的动作。"""


def _find_contract() -> Optional[Path]:
    candidates = [
        os.environ.get("A2A_CONTRACT_DIR", ""),
        ".contract",
        str(Path(__file__).resolve().parents[2] / "a2a-contract"),
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        if (path / "fixtures" / "index.json").exists():
            return path
    return None


CONTRACT = _find_contract()
SKIP_REASON = (
    "找不到 a2a-contract。设 A2A_CONTRACT_DIR，或 "
    f"git clone --depth 1 --branch {CONTRACT_TAG} <repo> .contract"
)


def _index() -> dict:
    return json.loads((CONTRACT / "fixtures" / "index.json").read_text(encoding="utf-8"))


def _registry():
    """把契约仓库的 schema 按文件名注册，让相对 $ref 能解析。"""
    from referencing import Registry, Resource

    resources = []
    for path in sorted((CONTRACT / "schema").glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc)
        resources.append((path.name, resource))
        if "$id" in doc:
            resources.append((doc["$id"], resource))
    return Registry().with_resources(resources)


def _validator(schema_name: str):
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (CONTRACT / schema_name).read_text(encoding="utf-8")
    )
    return Draft202012Validator(schema, registry=_registry())


def _is_valid(schema_name: str, payload: Any) -> bool:
    return _validator(schema_name).is_valid(payload)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class FixtureSchemaTest(unittest.TestCase):
    """A / B / D / E 组的形状断言：每条 fixture 的 expect 必须成立。"""

    def test_every_fixture_matches_its_expectation(self):
        cases = _index()["cases"]
        self.assertTrue(cases, "fixtures/index.json 里一条用例都没有")
        for case in cases:
            payload = json.loads(
                (CONTRACT / "fixtures" / case["file"]).read_text(encoding="utf-8")
            )
            valid = _is_valid(case["schema"], payload)
            self.assertEqual(
                valid,
                case["expect"] == "valid",
                f"{case['id']}（{case['why']}）：期望 {case['expect']}，实际 "
                f"{'valid' if valid else 'invalid'}",
            )

    def test_contract_version_is_the_one_we_pinned(self):
        # 契约仓库升了版本而这边没跟，属于「两侧不再跑同一组用例」，要显式发现
        self.assertEqual(_index()["contract_version"], CONTRACT_TAG)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class TrustVerdictTest(unittest.TestCase):
    """
    C 组：信任反例。**这一组最容易被跳过，也最值钱**——它们全都是合法载荷，
    schema 拦不住，只能靠业务判据。

    **`dp-trust-01` 在契约 v1.12 离开了 C 组，改判到 B 组**：`execution_status`
    提级为 `required` 之后，它那份不带该字段的载荷不再合法，schema 现在拦得住了。
    C 组 failed 这一档由 `dp-trust-07` 接手（同一份载荷，带着字段）。
    本类里针对 01 的两条用例仍然保留，断的东西变了——见各自的 docstring。

    **这个文件里没有 xfail。** 契约 v1.9 说清楚了：进度标记只能挂在自己真实产出的
    载荷上，不能挂在冻结的 fixture 上。判据见 src/ai_sql_agent/api/trust.py，
    上线证据见 tests/test_business_outcome.py。
    """

    def test_every_fixture_gets_the_expected_trust_verdict(self):
        for case in _index()["cases"]:
            if case["trust"] == "n/a":
                continue
            payload = json.loads(
                (CONTRACT / "fixtures" / case["file"]).read_text(encoding="utf-8")
            )
            verdict = assess(
                payload, schema_valid=_is_valid(case["schema"], payload)
            )
            self.assertEqual(
                verdict,
                case["trust"],
                f"{case['id']}（{case['why']}）",
            )

    def test_failed_step_fixture_is_rejected_for_the_missing_status_field(self):
        """
        契约仓库点名的那条用例。**v1.12 起它的性质变了，断言跟着改。**

        v1.11 之前：它是合法载荷（C 组），schema 拦不住，只能靠业务判据——
        `queried_database=true`、`has_risk=false`、`degraded=false` 三布尔全"干净"，
        而唯一一步 `success=false`。

        v1.12 起：`execution_status` 提级为 `required`，这份不带该字段的载荷
        **不再合法**，改判到 B 组。所以这里断两件事：schema 必须拒它，
        **且拒绝理由就是缺这个字段**——理由不对就说明它是因为别的原因被拒的，
        这条用例就没在保护它声称保护的东西。

        业务结论仍然是不可作出处，只是现在走的是"校验失败 → 回落"那条路。
        """
        payload = json.loads(
            (CONTRACT / "fixtures/data-part/trust-negative/01-failed-step.json")
            .read_text(encoding="utf-8")
        )
        self.assertFalse(
            _is_valid("schema/artifact-data-part.schema.json", payload),
            "v1.12 起缺 execution_status 的载荷不再合法（契约 §4.6.1 / §4.6.7）",
        )
        reasons = [
            e.message
            for e in _validator("schema/artifact-data-part.schema.json").iter_errors(payload)
        ]
        self.assertTrue(
            any("execution_status" in m for m in reasons),
            f"拒绝理由里没有 execution_status，实际是：{reasons}",
        )
        self.assertEqual(assess(payload, schema_valid=False), "not_usable")

    def test_failed_step_fixture_status_comes_from_derivation(self):
        """
        第二条断言：判定来源。

        这份 fixture 没有 `execution_status` 字段，所以结论只能靠
        §4.6.1 从 `steps[]` 推——`status_source == "derived"`。

        **v1.12 起这条断言的含义变了，但断言本身不变。** 提级之后
        §4.6.7 的过渡判据作废，"没有该字段"不再是契约认可的路径；
        推导能力保留下来是**对非合规载荷的防御**——服务端出 bug、版本错配、
        中间件吞字段都会让这种载荷真的出现，那时还得靠 `steps[]` 说清楚
        "这次查挂了"，而不是崩掉或者静默采信。所以：推导结论必须仍然算得对，
        但按 §4.6.7 一律当不可信（见上一条用例的 `assess` 断言）。

        缺口关没关，要看**我方自己产出的载荷**是不是 `contract`——
        那条断言在 tests/test_business_outcome.py::
        test_our_own_payload_reports_status_source_contract。
        """
        payload = json.loads(
            (CONTRACT / "fixtures/data-part/trust-negative/01-failed-step.json")
            .read_text(encoding="utf-8")
        )
        status, source = execution_status_of(payload)
        self.assertEqual(status, "failed")
        self.assertEqual(source, "derived")

    def test_a_group_and_b_group_are_green_at_the_same_time(self):
        """
        反向断言：一个「永远返回不可信」的实现能过 B 组但会让 A 组挂。
        两组必须同时绿，只绿一组说明判据被写死了。
        """
        verdicts = set()
        for case in _index()["cases"]:
            if case["group"] not in ("A", "B"):
                continue
            payload = json.loads(
                (CONTRACT / "fixtures" / case["file"]).read_text(encoding="utf-8")
            )
            verdicts.add(
                assess(payload, schema_valid=_is_valid(case["schema"], payload))
            )
        self.assertIn("usable_as_source", verdicts)
        self.assertIn("not_usable", verdicts)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class UnlistedDatabaseTest(unittest.TestCase):
    """
    D 组：v1.6 起**新增 `database` 标识符不算契约变更**，所以 `dp-valid-04`
    （`hr_db`，清单外但形状合法）是**常规路径不是异常路径**。
    """

    def test_unlisted_but_well_shaped_identifier_is_a_normal_path(self):
        payload = json.loads(
            (CONTRACT / "fixtures/data-part/valid/04-unlisted-database.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(_is_valid("schema/artifact-data-part.schema.json", payload))
        self.assertEqual(assess(payload, schema_valid=True), "usable_as_source")

    def test_we_only_emit_identifiers_from_the_current_manifest(self):
        """
        服务端侧断言用 `current-*`，不是 `known`——退役的 mes_db / pm_db
        不会出现在任何部署里，拿 `known` 去断言运行时会发出什么是错的。
        """
        import re

        from ai_sql_agent.a2a.server import active_database_manifest

        schema = json.loads(
            (CONTRACT / "schema/database-id.schema.json").read_text(encoding="utf-8")
        )
        pattern = re.compile(schema["pattern"])
        for name in active_database_manifest():
            self.assertRegex(name, pattern, f"{name} 不符合契约的形状约束")


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class EmittedStageSetTest(unittest.TestCase):
    """
    E 组的服务端额外断言：真跑一条流，看发出来的 stage 集合。

    **断言的目标集合从 `emitted-2026-09-04` 换成了 `contract-declared`。**
    CONSUMER-TESTS 说得很直白：「normalization 上线后改断言集合，改集合的
    那个提交就是『这条缺口关闭』的证据。」两者只差一个 `normalization`，
    它现在是真 stage（`text2sql_pipeline` 里那条裸 log_event 换成了 `stage()`）。
    """

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()
        get_settings().a2a_streaming = True

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def _progress_events(self) -> List[dict]:
        body = {"jsonrpc": "2.0", "id": "stage-probe", "method": "message/stream",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m",
                                       "contextId": f"stage_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text",
                                                  "text": "查询准时率低于0.9的供应商名称"}],
                                       "metadata": {"user_id": "u_stage"}}}}
        with self.client.stream("POST", "/a2a", json=body,
                                headers={"Accept": "text/event-stream"}) as response:
            response.read()
            events = [
                json.loads(line[5:].strip())["result"]
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
        return [
            event for event in events
            if event.get("kind") == "status-update" and not event.get("final")
        ]

    def _stage_defs(self, name: str) -> set:
        schema = json.loads(
            (CONTRACT / "schema/stage.schema.json").read_text(encoding="utf-8")
        )
        return set(schema["$defs"][name]["enum"])

    def test_emitted_stages_are_all_contract_declared(self):
        declared = self._stage_defs("contract-declared")
        emitted = {e["metadata"]["stage"] for e in self._progress_events()}
        self.assertTrue(emitted, "查库路径至少要有进度事件")
        self.assertTrue(
            emitted <= declared,
            f"发出了契约没声明的 stage：{sorted(emitted - declared)}",
        )

    def test_normalization_is_actually_emitted_now(self):
        """契约 §4.5 缺口框的关闭证据：九个声明的 stage 里不再少这一个。"""
        emitted = {e["metadata"]["stage"] for e in self._progress_events()}
        self.assertIn("normalization", emitted)

    def test_internal_stages_are_never_emitted(self):
        emitted = {e["metadata"]["stage"] for e in self._progress_events()}
        self.assertNotIn("memory_context", emitted, "内部管道 stage 不得出现（§4.5）")
        self.assertNotIn("sql_generation", emitted, "契约面是 generation，不是内部名")

    def test_every_progress_event_matches_the_envelope_schema(self):
        """信封本身也是契约面（§4.1）：整条 JSON-RPC 响应过 progress-event schema。"""
        body = {"jsonrpc": "2.0", "id": "envelope-probe", "method": "message/stream",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m",
                                       "contextId": f"env_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text",
                                                  "text": "查询准时率低于0.9的供应商名称"}],
                                       "metadata": {"user_id": "u_env"}}}}
        with self.client.stream("POST", "/a2a", json=body,
                                headers={"Accept": "text/event-stream"}) as response:
            response.read()
            envelopes = [
                json.loads(line[5:].strip())
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
        progress = [
            env for env in envelopes
            if env["result"].get("kind") == "status-update"
            and not env["result"].get("final")
        ]
        self.assertTrue(progress)
        for envelope in progress:
            self.assertTrue(
                _is_valid("schema/progress-event.schema.json", envelope),
                f"进度事件不过契约 schema：{envelope}",
            )

    def test_progress_metadata_carries_no_post_hoc_fields(self):
        """
        route / confidence / tables / changed 要到 stage 结束才有值。
        出现在进度事件里 = 把事后信息当成了事前信息（§4.5 的诚实说明）。
        """
        for event in self._progress_events():
            for forbidden in ("route", "confidence", "tables", "changed"):
                self.assertNotIn(forbidden, event["metadata"], forbidden)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class TerminalFrameTest(unittest.TestCase):
    """
    F 组（契约 v1.13）：我方真正发出去的终态帧要过 `schema/terminal-event.schema.json`。

    fixture 只证明契约仓库自己的样本是对的；这里用 `_terminal_frame` 现造三种终态，
    证明服务端发出去的也是这个形状。`canceled` 是 v1.13 新增的那一种（§4.4）。
    """

    SCHEMA = "schema/terminal-event.schema.json"

    def setUp(self):
        if not (CONTRACT / self.SCHEMA).exists():
            self.skipTest(f"契约里没有 {self.SCHEMA}（v1.13 之前的版本）")

    @staticmethod
    def _frame(state: str, error: str = "") -> Dict[str, Any]:
        from ai_sql_agent.a2a import server as a2a

        chunk = a2a._envelope("req-1", a2a._terminal_frame("t1", "c1", state, "trace-1", error=error))
        return json.loads(chunk[len("data: "):])

    def test_every_terminal_state_we_emit_matches_the_schema(self):
        for state, error in (("completed", ""), ("failed", "查询执行失败"), ("canceled", "cancelled: api")):
            with self.subTest(state=state):
                payload = self._frame(state, error)
                self.assertTrue(_is_valid(self.SCHEMA, payload),
                                [e.message for e in _validator(self.SCHEMA).iter_errors(payload)])

    def test_the_cancel_reason_spelling_is_not_a_state(self):
        # metadata.error 里的 cancelled: 是取消原因；state 只能是 A2A 规范的拼写 canceled
        self.assertFalse(_is_valid(self.SCHEMA, self._frame("cancelled", "cancelled: api")))


if __name__ == "__main__":
    unittest.main()
