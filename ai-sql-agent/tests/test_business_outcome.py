"""
业务终态语义（契约 §4.6）的服务端断言。

要证的是一件很具体的事：**SQL 全跑失败时，调用方能不能从字段上发现。**

2026-09-04 复核时不能——`state` 是 `completed`（pipeline 没抛异常），
`queried_database` 也是 `true`（它的定义只是「产生了 pipeline result」），
两个字段都不否定。调用方于是把模型编的话当成系统数据讲给了客户。

这个文件同时是 CONSUMER-TESTS **A 组的服务端那一半**：真跑一次查询，把产出的
data part 拿去过契约的 schema——不是拿 fixture 反过来喂自己。
"""
from __future__ import annotations

import json
import unittest
import uuid
from typing import Any, Dict, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_sql_agent.api.result_summary import (
    STATUS_FAILED,
    STATUS_NO_DATA,
    STATUS_NOT_APPLICABLE,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
    StepLogResponse,
    compute_execution_status,
)
from ai_sql_agent.api.trust import execution_status_of, trust_verdict
from ai_sql_agent.core import get_settings
from ai_sql_agent.execution.objects import MCPExecutionResult

from test_contract_fixtures import CONTRACT, SKIP_REASON, _is_valid

SESSION = f"s_outcome_{uuid.uuid4().hex[:8]}"
QUERY = "查询准时率低于0.9的供应商名称"


def _step(success: bool, row_count: int = 0, **kwargs) -> StepLogResponse:
    return StepLogResponse(
        database="erp_db", sql="SELECT 1", final_sql="SELECT 1",
        success=success, row_count=row_count,
        error=None if success else "boom", **kwargs
    )


class ExecutionStatusTest(unittest.TestCase):
    """契约 §4.6.1 的五个值，逐条对判据。"""

    def test_conversation_route_is_not_applicable(self):
        self.assertEqual(
            compute_execution_status("conversation", []), STATUS_NOT_APPLICABLE
        )

    def test_all_succeeded_with_rows_is_success(self):
        self.assertEqual(
            compute_execution_status("database_query", [_step(True, 3)]), STATUS_SUCCESS
        )

    def test_all_succeeded_with_zero_rows_is_no_data(self):
        """「查到了，答案是没有」≠「没查成」。对客户是两句完全不同的话。"""
        self.assertEqual(
            compute_execution_status("database_query", [_step(True, 0)]), STATUS_NO_DATA
        )

    def test_mixed_is_partial_success(self):
        """
        跨库 2 步挂 1 步：成功那步的数据是真的。判 failed 会让调用方整段回落，
        把那半份真数据一起扔掉——比现状更糟，所以它需要第三个值。
        """
        self.assertEqual(
            compute_execution_status("database_query", [_step(True, 2), _step(False)]),
            STATUS_PARTIAL_SUCCESS,
        )

    def test_none_succeeded_is_failed(self):
        self.assertEqual(
            compute_execution_status("database_query", [_step(False)]), STATUS_FAILED
        )

    def test_database_route_with_no_steps_is_failed_not_no_data(self):
        """
        空步骤列表下 `all([])` 是 True、`sum([]) == 0`，判定顺序写反就会把
        「一步都没跑」错判成 no_data——一个是没查成，一个是查到了没有。
        """
        self.assertEqual(compute_execution_status("database_query", []), STATUS_FAILED)

    def test_counts_are_self_consistent(self):
        steps = [_step(True, 1), _step(False)]
        succeeded = sum(1 for s in steps if s.success)
        failed = sum(1 for s in steps if not s.success)
        self.assertEqual(succeeded + failed, len(steps))


class _FailingExecutor:
    """
    每条 SQL 都执行失败，但**不抛异常**——正是复核里那个场景：
    pipeline 正常跑完、正常返回，只是一行数据都没查到。
    """

    def __call__(self, request):
        # 路由器两种入参都接：修复回调传的是 dict
        if isinstance(request, dict):
            database, sql = request["database"], request["sql"]
        else:
            database, sql = request.database, request.sql
        return MCPExecutionResult(
            database=database, sql=sql,
            success=False, error="injected: 数据库连接被拒绝",
        )


class AllStepsFailedTest(unittest.TestCase):
    """P0-B 的验收条件：造这个场景，看调用方能不能分辨出来。"""

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def _rpc(self, method: str) -> dict:
        return {"jsonrpc": "2.0", "id": "req-fail", "method": method,
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m1",
                                       "contextId": f"{SESSION}_{uuid.uuid4().hex[:6]}",
                                       "parts": [{"kind": "text", "text": QUERY}],
                                       "metadata": {"user_id": "u_outcome"}}}}

    def test_send_reports_completed_transport_but_failed_business_outcome(self):
        from ai_sql_agent.execution.router import MCPRouter

        with patch.object(MCPRouter, "execute", _FailingExecutor()):
            response = self.client.post("/a2a", json=self._rpc("message/send"))

        task = response.json()["result"]
        data = next(
            part["data"]
            for part in task["artifacts"][0]["parts"]
            if part["kind"] == "data"
        )

        # 传输终态不变：载荷完整送达了，pipeline 没抛异常（契约 §4.6.8 的四条理由）
        self.assertEqual(task["status"]["state"], "completed")
        # 这两个字段都不否定——它们本来就否定不了，这正是缺口的形状
        self.assertTrue(data["queried_database"])
        self.assertFalse(data["has_risk"])

        # 业务终态否定得了：这是新增的那一层
        self.assertEqual(data["execution_status"], STATUS_FAILED)
        self.assertEqual(data["succeeded_steps"], 0)
        self.assertGreater(data["failed_steps"], 0)
        self.assertEqual(trust_verdict(data), "not_usable")

    def test_query_endpoint_says_the_same_thing(self):
        from ai_sql_agent.execution.router import MCPRouter

        with patch.object(MCPRouter, "execute", _FailingExecutor()):
            response = self.client.post("/query", json={
                "user_id": "u_outcome",
                "session_id": f"{SESSION}_q_{uuid.uuid4().hex[:6]}",
                "query": QUERY,
            })
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["execution_status"], STATUS_FAILED)
        self.assertEqual(body["succeeded_steps"], 0)


class TransportIsomorphismTest(unittest.TestCase):
    """
    契约 §4.4 / §4.4.1：**三个**承载者返回同一套 §4.6 字段。

    v1.8 由两条改为三条：直连 `POST /query` 一直在契约的保护范围之外，
    而它是 `SQL_AGENT_TRANSPORT` 的默认值——**跑生产流量的那条路**，
    也是整条 A2A 关掉时 §6.2 的回滚落点。

    回滚会依次落在 send 和直连 /query 上，任何一个的载荷和 stream 不一样，
    回滚就是静默降级——正是 §6.2 反复要防的那种。
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

    def _send_data(self) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": "i", "method": "message/send",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m", "contextId": f"iso_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text", "text": QUERY}],
                                       "metadata": {"user_id": "u_iso"}}}}
        task = self.client.post("/a2a", json=body).json()["result"]
        return next(p["data"] for p in task["artifacts"][0]["parts"] if p["kind"] == "data")

    def _stream_data(self) -> Optional[Dict[str, Any]]:
        body = {"jsonrpc": "2.0", "id": "i", "method": "message/stream",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m", "contextId": f"iso_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text", "text": QUERY}],
                                       "metadata": {"user_id": "u_iso"}}}}
        with self.client.stream("POST", "/a2a", json=body,
                                headers={"Accept": "text/event-stream"}) as response:
            response.read()
            events = [
                json.loads(line[5:].strip())["result"]
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
        for event in events:
            if event.get("kind") == "artifact-update":
                return next(
                    p["data"] for p in event["artifact"]["parts"] if p["kind"] == "data"
                )
        return None

    def _query_data(self) -> Dict[str, Any]:
        """第三个承载者：直连 /query，data part 就在响应体顶层（§4.4.1）。"""
        return self.client.post("/query", json={
            "user_id": "u_iso",
            "session_id": f"iso_q_{uuid.uuid4().hex[:8]}",
            "query": QUERY,
        }).json()

    def test_query_endpoint_emits_the_contract_field_name_steps(self):
        """
        §4.4.1 点名的那个静默缺陷：/query 把逐步骤日志叫 `step_logs`，
        A2A 那两条叫 `steps`——同一个 StepLogResponse，两个名字。

        后果不是「字段名不好看」：调用方按 §4.6.7 处理缺失字段，`steps` 取不到
        就按「缺失 = 不可信」处置 ⇒ **每一次直连查询都不再是出处**，
        而链路看起来完全正常，没有报错、没有降级事件、没有任何症状。
        """
        body = self._query_data()
        self.assertIn("steps", body, "契约面的名字是 steps（§4.4.1）")
        self.assertEqual(
            body["steps"], body["step_logs"],
            "过渡期两个字段同时发，内容必须是同一份数据",
        )

    def test_query_payload_is_trusted_the_same_way_as_a2a(self):
        """同一份判据读三个承载者，结论必须一致——否则回滚就是静默降级。"""
        self.assertEqual(trust_verdict(self._query_data()), trust_verdict(self._send_data()))

    def test_all_three_carriers_carry_the_same_business_fields(self):
        send, stream, query = self._send_data(), self._stream_data(), self._query_data()
        self.assertIsNotNone(stream)
        for key in ("route", "execution_status", "queried_database", "has_risk",
                    "degraded", "result_truncated", "succeeded_steps",
                    "failed_steps", "repaired_steps", "currency"):
            self.assertEqual(send[key], stream[key], f"send vs stream: {key}")
            self.assertEqual(send[key], query[key], f"send vs /query: {key}")

    def test_both_transports_carry_the_same_field_set(self):
        send, stream = self._send_data(), self._stream_data()
        self.assertIsNotNone(stream)
        self.assertEqual(
            sorted(send.keys()), sorted(stream.keys()),
            "走哪条路信息更多，本身就是缺陷",
        )
        for key in ("execution_status", "succeeded_steps", "failed_steps",
                    "repaired_steps", "result_truncated", "as_of", "as_of_source",
                    "currency"):
            self.assertIn(key, send, key)
            self.assertIn(key, stream, key)

    def test_business_fields_agree_across_transports(self):
        send, stream = self._send_data(), self._stream_data()
        for key in ("route", "execution_status", "queried_database",
                    "has_risk", "degraded", "result_truncated"):
            self.assertEqual(send[key], stream[key], key)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class ProducedPayloadMatchesContractTest(unittest.TestCase):
    """
    CONSUMER-TESTS A 组的服务端那一半：真跑一次查询，产出的 data part 过 schema。

    B 组的服务端断言顺带也在这里——「自己产出的 data part 永远不长成那六种样子」，
    过一遍 schema 就是它。
    """

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def _data_part(self) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": "i", "method": "message/send",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m",
                                       "contextId": f"schema_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text", "text": QUERY}],
                                       "metadata": {"user_id": "u_schema"}}}}
        task = self.client.post("/a2a", json=body).json()["result"]
        return next(p["data"] for p in task["artifacts"][0]["parts"] if p["kind"] == "data")

    def test_real_data_part_passes_the_contract_schema(self):
        data = self._data_part()
        validator_ok = _is_valid("schema/artifact-data-part.schema.json", data)
        if not validator_ok:
            from jsonschema import Draft202012Validator  # noqa: F401

            import tests.test_contract_fixtures as fixtures  # noqa: F401
        self.assertTrue(validator_ok, f"产出的 data part 不过契约 schema：{data}")

    def test_query_endpoint_payload_also_passes_the_contract_schema(self):
        """
        §4.4.1：载荷形状的规则适用于**全部三个承载者**，直连 /query 也不例外。
        它在 v1.8 之前一直没有契约用例覆盖——而它是跑生产流量的那条路。
        """
        body = self.client.post("/query", json={
            "user_id": "u_schema",
            "session_id": f"schema_q_{uuid.uuid4().hex[:8]}",
            "query": QUERY,
        }).json()
        self.assertTrue(
            _is_valid("schema/artifact-data-part.schema.json", body),
            f"/query 的响应体不过契约 schema：{ {k: v for k, v in body.items() if k != 'steps'} }",
        )

    def test_our_own_payload_reports_status_source_contract(self):
        """
        CONSUMER-TESTS 的 C 组把 `dp-trust-01` 拆成两条断言（v1.7 改），
        第二条是 **`status_source == "contract"`**：结论必须来自服务端下发的字段，
        不是调用方自己 reduce。

        **缺口的实质不是「结论错」，是「结论由谁定义」**——§4.6.2 警告过
        「三个汇总数调用方不应自己 reduce，reduce 的口径会漂」。
        只要来源还是 derived，那句警告就还成立，缺口就没真关。

        这一条对**我方产出的载荷**必须是 contract，而且三个承载者都得是。
        """
        for label, payload in (
            ("send", self._data_part()),
            ("/query", self.client.post("/query", json={
                "user_id": "u_src",
                "session_id": f"src_{uuid.uuid4().hex[:8]}",
                "query": QUERY,
            }).json()),
        ):
            _status, source = execution_status_of(payload)
            self.assertEqual(source, "contract", f"{label} 的 status_source")

    def test_real_data_part_has_the_new_business_outcome_fields(self):
        data = self._data_part()
        self.assertIn(data["execution_status"],
                      {STATUS_SUCCESS, STATUS_NO_DATA, STATUS_PARTIAL_SUCCESS,
                       STATUS_FAILED, STATUS_NOT_APPLICABLE})
        self.assertEqual(
            data["succeeded_steps"] + data["failed_steps"], len(data["steps"]),
            "三个汇总数与 steps[] 必须自洽（§4.6.2）",
        )
        for step in data["steps"]:
            if step["truncated"]:
                self.assertIsNotNone(
                    step["row_limit"],
                    "§4.6.3：truncated=true 时 row_limit 必须有值",
                )


class CurrencyAndSourceRecordsTest(unittest.TestCase):
    """
    契约 §4.6.5 / §4.6.6。

    §4.6.5 是这批缺口里**唯一一条已经出过业务事故的**：服务端不返回币种，
    调用方只好在自己那边配一个全局 `SQL_AGENT_CURRENCY`，那段注释自己写着
    「接美国厂的库时，报给 builder 的金额就带了错误的币种」。
    """

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer

        # 用一份就地拼的语义层，不依赖当前部署配了哪套元数据。
        # 依赖 AI_SQL_SEMANTIC_DIR 的话这组用例在 CI（内置演示元数据、没有单位）
        # 会整组 skip——一个永远不跑的用例和没有用例差不多。
        cls.semantic = SemanticLayer(
            databases={
                "erp_db": DatabaseSemantics(
                    name="erp_db",
                    tables={
                        "job": {
                            "primary_key": ["job_no"],
                            "columns": {
                                "job_no": {"description": "工程号"},
                                "contract_amount": {
                                    "description": "合同金额（美元）",
                                    "unit": "USD",
                                },
                                "job_status": {"description": "状态"},
                            },
                        }
                    },
                )
            }
        )

    def _units(self, sql: str, columns):
        from ai_sql_agent.pipeline.provenance import column_units

        return column_units(
            self.semantic, database="erp_db", sql=sql, dialect="sqlite", columns=columns
        )

    def test_money_column_carries_its_currency(self):
        self.assertEqual(
            self._units("SELECT job_no, contract_amount FROM job", ["contract_amount"]),
            {"contract_amount": "USD"},
        )

    def test_aggregate_over_a_money_column_keeps_the_currency(self):
        """`SUM(contract_amount) AS total` —— 调用方最常拿到的就是这种聚合列。"""
        self.assertEqual(
            self._units("SELECT SUM(contract_amount) AS total FROM job", ["total"]),
            {"total": "USD"},
        )

    def test_count_is_not_money(self):
        """计数的单位是「条」不是美元。把 COUNT 也算成保留单位会报出假币种。"""
        self.assertEqual(self._units("SELECT COUNT(*) AS n FROM job", ["n"]), {})

    def test_ratio_has_no_inherited_unit(self):
        """除法改变单位，不能把分子的币种继承下来。"""
        self.assertEqual(
            self._units(
                "SELECT contract_amount / 12 AS monthly FROM job", ["monthly"]
            ),
            {},
        )

    def test_unknown_column_gets_no_unit_rather_than_a_guess(self):
        self.assertEqual(
            self._units("SELECT job_status FROM job", ["job_status"]), {}
        )

    def test_source_records_carry_the_primary_key(self):
        from ai_sql_agent.pipeline.provenance import source_records

        records = source_records(
            self.semantic,
            database="erp_db",
            sql="SELECT job_no, contract_amount FROM job",
            dialect="sqlite",
            columns=["job_no", "contract_amount"],
            rows=[{"job_no": "J25-00003", "contract_amount": 1000}],
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["table"], "job")
        self.assertEqual(records[0]["key_columns"], ["job_no"])
        self.assertEqual(records[0]["keys"], ["J25-00003"])

    def test_no_primary_key_in_the_result_means_no_source_records(self):
        """
        追不到记录时就不给，不编一个近似的出来——溯源手段退回
        trace_id + final_sql（契约 §4.6.6 的「实现之前的处置」）。
        """
        from ai_sql_agent.pipeline.provenance import source_records

        self.assertEqual(
            source_records(
                self.semantic,
                database="erp_db",
                sql="SELECT SUM(contract_amount) AS total FROM job",
                dialect="sqlite",
                columns=["total"],
                rows=[{"total": 1000}],
            ),
            [],
        )

    def test_mixed_currencies_do_not_collapse_into_one(self):
        """
        多币种时顶层 `currency` 必须是 None，让调用方逐列读。
        给一个「主要币种」正是那个全局常量犯的错。
        """
        from ai_sql_agent.pipeline.provenance import dominant_currency

        self.assertEqual(dominant_currency([{"a": "USD"}, {"b": "USD"}]), "USD")
        self.assertIsNone(dominant_currency([{"a": "USD"}, {"b": "CNY"}]))
        self.assertIsNone(dominant_currency([{}]))


if __name__ == "__main__":
    unittest.main()
