"""Independent, development-only controls for the frozen fb14f67 wire format.

New metadata/lookup states below are explicitly synthetic. The real development
recordings, their stored scores and their original helper identifiers stay intact.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from acceptance.common import ROOT, read, sha
from acceptance.common_facts import common_score
from acceptance.oracle import score
from acceptance.source_semantics import (CATALOG, CATALOG_SHA256, EMPTY_POLICY_NOTICE, bound_metadata,
    catalog, digest, direct_columns)

FIXTURES = Path(__file__).parent / "fixtures"
OLD = FIXTURES / "oracle-recordings-61d88ef"
IMAGE = FIXTURES / "source-semantics-fb14f67"
NOTE = "以上为可核对的资料。涉及补件、责任、金额或交期承诺，请由员工按适用政策审核后处理。"


def current(observed):
    return next(d for d in observed["case"]["drafts"] if d.get("valid"))


def query(observed):
    return next(r for r in current(observed)["evidence"] if r.get("source") == "business_query")


def wire_metadata(sql, output, origin):
    database, table, column = origin.split(".")
    source = {"database": database, "table": table, "column": column, **catalog()[origin]["metadata"]}
    return {**source, "output_field": output, "query_hash": digest(sql),
            "schema_revision": digest(json.dumps(source, ensure_ascii=False, sort_keys=True))}


def rehash(meta):
    source = {k: v for k, v in meta.items() if k not in {"query_hash", "schema_revision", "output_field"}}
    meta["schema_revision"] = digest(json.dumps(source, ensure_ascii=False, sort_keys=True))


class SourceSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Deliberately avoid dataset(), which also reads the release corpus.
        cls.corpus = read(ROOT / "acceptance/data/v3/development.json")
        cls.scenarios = {s["id"]: s for s in cls.corpus["scenarios"]}

    def evaluate(self, sid, observed):
        return score(self.scenarios[sid], observed, self.corpus)

    def delivery(self, *, language="zh", no_handoff=True):
        o = deepcopy(read(OLD / "development-orders-003.json")["observed"])
        if no_handoff:
            o["side_effects"]["handoff"] = 0
        d, q = current(o), query(o)
        p = q["evidence_payload"]
        for si, field, origin, old_label in [(0, "delivery_date", "erp_db.orders.delivery_date", "配送日期"),
                                           (1, "delivery_status", "svc_db.delivery.delivery_status", "delivery status")]:
            step = p["steps"][si]
            meta = wire_metadata(step["final_sql"], field, origin)
            step.setdefault("column_metadata", {})[field] = meta
            for c in q["evidence"]:
                if c["step_index"] == si and c["field"] == field:
                    c["column_metadata"] = meta
            entry = catalog()[origin]
            label = meta["label_" + language]
            needle = next(line for line in d["body"].splitlines() if line.startswith("- " + old_label + ":"))
            caption = "字段含义" if language == "zh" else "Field meaning"
            d["body"] = d["body"].replace(needle, needle.replace(old_label, label)
                + f"\n  {caption}: " + entry["visible_description"][language])
        return o

    def test_catalog_is_independent_pinned_and_contains_no_runtime_data(self):
        self.assertEqual(sha(CATALOG.read_bytes()), CATALOG_SHA256)
        self.assertEqual(len(catalog()), 60)
        doc = read(CATALOG)
        self.assertTrue(doc["source_revision"].startswith("000e2b7"))
        self.assertEqual(len(doc["source_files"]), 4)
        for entry in catalog().values():
            self.assertNotIn("samples", entry["metadata"])
            self.assertNotIn("connection", entry["metadata"])
        self.assertEqual(sha((IMAGE / "development-images-001.json").read_bytes()), read(IMAGE / "provenance.json")["sha256"])

    def test_final_v115_metadata_and_configured_labels_are_recognized(self):
        for language in ("zh", "en"):
            with self.subTest(language=language):
                o = self.delivery(language=language)
                p = query(o)["evidence_payload"]
                m = p["steps"][0]["column_metadata"]["delivery_date"]
                self.assertEqual(m["output_field"], "delivery_date")
                self.assertIsNotNone(bound_metadata(p["steps"][0], "delivery_date"))
                verdict = self.evaluate("development-orders-003", o)
                self.assertTrue(verdict["fact_correct"], verdict)

    def test_sql_projection_is_independent_and_conservative(self):
        allowed = {
            'SELECT "o"."delivery_date" AS "promised", "o"."order_no" FROM "orders" AS "o" WHERE "o"."order_no" = \'FROM fake JOIN bad\'':
                {"promised": ("orders", "delivery_date"), "order_no": ("orders", "order_no")},
            'SELECT o.order_no, i.quantity AS ordered_qty FROM orders o JOIN order_item i ON o.order_no=i.order_no WHERE i.model IN (\'x\',\'y\')':
                {"order_no": ("orders", "order_no"), "ordered_qty": ("order_item", "quantity")},
            'SELECT DISTINCT [delivery_status], [order_no] FROM [delivery] /* JOIN wrong */;':
                {"delivery_status": ("delivery", "delivery_status"), "order_no": ("delivery", "order_no")},
            'SELECT O.DELIVERY_DATE AS promised FROM ORDERS O': {"promised": ("orders", "delivery_date")},
        }
        for sql, expected in allowed.items():
            with self.subTest(sql=sql):
                self.assertEqual(direct_columns(sql), expected)
        rejected = [
            "SELECT * FROM orders", "SELECT MAX(delivery_date) AS delivery_date FROM orders",
            "SELECT delivery_date, signed_date AS delivery_date FROM orders",
            "SELECT delivery_date AS D, signed_date AS d FROM orders",
            "SELECT order_no FROM orders o JOIN order_item i ON o.order_no=i.order_no",
            "SELECT delivery_date FROM main.orders", "SELECT delivery_date FROM (SELECT * FROM orders)",
            "WITH o AS (SELECT * FROM orders) SELECT delivery_date FROM o",
            "SELECT delivery_date FROM orders; SELECT signed_date FROM orders",
            "SELECT delivery_date FROM orders UNION SELECT signed_date FROM orders",
            "SELECT o.delivery_date FROM orders o, order_item i",
        ]
        for sql in rejected:
            with self.subTest(sql=sql):
                self.assertEqual(direct_columns(sql), {})

    def test_dynamic_label_follows_direct_output_alias_not_alias_spelling(self):
        o = self.delivery()
        q = query(o)
        p = q["evidence_payload"]
        step = p["steps"][0]
        step["final_sql"] = "SELECT O.ORDER_NO AS order_no, O.DELIVERY_DATE AS promised FROM ORDERS O WHERE O.ORDER_NO='SO-2026-11002'"
        step["rows"][0]["promised"] = step["rows"][0].pop("delivery_date")
        step["column_metadata"] = {"promised": wire_metadata(step["final_sql"], "promised", "erp_db.orders.delivery_date"),
                                   "order_no": wire_metadata(step["final_sql"], "order_no", "erp_db.orders.order_no")}
        query_hash = digest(step["final_sql"])
        for cell in [*p["evidence"], *q["evidence"]]:
            if cell["step_index"] != 0:
                continue
            if cell["field"] == "delivery_date":
                cell["field"] = "promised"
                if "column_metadata" in cell:
                    cell["column_metadata"] = step["column_metadata"]["promised"]
            cell["query_hash"] = query_hash
            cell["entity"]["query_hash"] = query_hash
            identity = [step["database"], query_hash, step["as_of"], 0, cell["row_index"], cell["field"], cell["value"]]
            cell["evidence_id"] = "ev_" + digest(json.dumps(identity, ensure_ascii=False, sort_keys=True))
        verdict = self.evaluate("development-orders-003", o)
        self.assertTrue(verdict["fact_correct"], verdict)
        step["column_metadata"]["promised"]["output_field"] = "delivery_date"
        self.assertFalse(self.evaluate("development-orders-003", o)["fact_correct"])

    def test_null_field_uses_current_source_metadata_without_inventing_a_cell(self):
        recording = read(FIXTURES / "missing_field.json")
        o = deepcopy(recording["observed"])
        q = query(o)
        p = q["evidence_payload"]
        step = next(s for s in p["steps"] if "phone" in s["rows"][0])
        step["column_metadata"] = {"phone": wire_metadata(step["final_sql"], "phone", "erp_db.orders.phone")}
        self.assertIsNone(step["rows"][0]["phone"])
        body = current(o)["body"]
        line = next(line for line in body.splitlines() if line.startswith("- ") and "未登记" in line)
        current(o)["body"] = body.replace(line, "- 手机号: 未登记\n  字段含义: " + catalog()["erp_db.orders.phone"]["visible_description"]["zh"])
        verdict = self.evaluate(o["id"], o)
        self.assertTrue(verdict["fact_correct"], verdict)
        step["column_metadata"]["phone"]["column"] = "customer_name"
        rehash(step["column_metadata"]["phone"])
        self.assertFalse(self.evaluate(o["id"], o)["fact_correct"])

    def test_wrong_metadata_binding_even_with_self_signed_hashes_fails(self):
        changes = ("wrong_column", "wrong_table", "wrong_database", "wrong_description", "wrong_label",
                   "wrong_output_field", "missing_output_field", "extra_key", "wrong_hash", "wrong_revision",
                   "swapped_output_keys", "missing_metadata", "missing_sql", "no_source_ids", "stale", "failed",
                   "truncated", "wrong_unit", "wrong_raw_value", "wrong_evidence_id", "missing_wire_cell")
        for change in changes:
            with self.subTest(change=change):
                o = self.delivery()
                q = query(o)
                p = q["evidence_payload"]
                step = p["steps"][0]
                m = step["column_metadata"]["delivery_date"]
                if change in {"wrong_column", "wrong_table", "wrong_database", "wrong_description", "wrong_label", "wrong_output_field"}:
                    key, value = {"wrong_column": ("column", "signed_date"), "wrong_table": ("table", "order_item"),
                        "wrong_database": ("database", "svc_db"), "wrong_description": ("description", "保证明日免费送达。"),
                        "wrong_label": ("label_zh", "实际送达日期"), "wrong_output_field": ("output_field", "signed_date")}[change]
                    old = m[key]
                    m[key] = value
                    rehash(m)
                    if key == "description":
                        current(o)["body"] = current(o)["body"].replace(catalog()["erp_db.orders.delivery_date"]["visible_description"]["zh"], value)
                    if key == "label_zh":
                        current(o)["body"] = current(o)["body"].replace("- " + old + ":", "- " + value + ":")
                elif change == "missing_output_field":
                    m.pop("output_field")
                elif change == "extra_key":
                    m["promise"] = "free delivery"
                    rehash(m)
                elif change == "wrong_hash":
                    m["query_hash"] = "0" * 64
                elif change == "wrong_revision":
                    m["schema_revision"] = "0" * 64
                elif change == "swapped_output_keys":
                    wrong = wire_metadata(step["final_sql"], "order_no", "erp_db.orders.order_no")
                    m.clear()
                    m.update(wrong)
                elif change == "missing_metadata":
                    step.pop("column_metadata")
                elif change == "missing_sql":
                    step["final_sql"] = ""
                elif change == "no_source_ids":
                    p["trace_id"] = p["run_id"] = ""
                elif change == "stale":
                    current(o)["created_at"] += 301
                elif change == "failed":
                    p["execution_status"] = "failed"
                elif change == "truncated":
                    step["truncated"] = True
                elif change == "wrong_unit":
                    next(c for c in q["evidence"] if c["field"] == "delivery_date")["unit"] = "USD"
                elif change == "wrong_raw_value":
                    step["rows"][0]["delivery_date"] = "2030-01-01"
                elif change == "wrong_evidence_id":
                    next(c for c in q["evidence"] if c["field"] == "delivery_date")["evidence_id"] = "forged"
                elif change == "missing_wire_cell":
                    p["evidence"] = [c for c in p["evidence"] if c["field"] != "delivery_date"]
                self.assertFalse(self.evaluate("development-orders-003", o)["fact_correct"])

    def test_meaning_must_follow_its_own_correct_claim_and_cannot_hide_promises(self):
        for change in ("other_field", "same_line_promise", "new_line_promise", "wrong_fact", "detached", "duplicate"):
            with self.subTest(change=change):
                o = self.delivery()
                d = current(o)
                line = "  字段含义: " + catalog()["erp_db.orders.delivery_date"]["visible_description"]["zh"]
                if change == "other_field":
                    d["body"] = d["body"].replace(line, "  字段含义: " + catalog()["erp_db.orders.signed_date"]["visible_description"]["zh"])
                elif change == "same_line_promise":
                    d["body"] = d["body"].replace(line, line + "保证明天送达。")
                elif change == "new_line_promise":
                    d["body"] += "\n保证明天免费送达。"
                elif change == "wrong_fact":
                    d["body"] = d["body"].replace("- 交付日期: 2026-11-12", "- 交付日期: 2030-01-01")
                elif change == "detached":
                    d["body"] = line + "\n" + d["body"].replace(line, "")
                elif change == "duplicate":
                    d["body"] = d["body"].replace(line, line + "\n" + line)
                self.assertFalse(self.evaluate("development-orders-003", o)["fact_correct"])

    def image(self):
        o = deepcopy(read(IMAGE / "development-images-001.json")["observed"])
        o["side_effects"]["handoff"] = 0
        d = current(o)
        ref = next(r for r in d["evidence"] if r.get("source") == "verified_image" and r["field"] == "date")
        source = ref["business_source"]
        # Authored synthetic original-image query. No later business query is
        # consulted to fill metadata or identifiers into this source.
        sql = "SELECT o.order_no AS order_ref, i.model, i.quantity AS ordered_qty, o.delivery_date, o.order_status FROM orders o JOIN order_item i ON o.order_no=i.order_no WHERE o.order_no='SO-2026-11039'"
        source.update(final_sql=sql, query_hash=digest(sql), step_index=0,
                      output_columns=["order_ref", "model", "ordered_qty", "delivery_date", "order_status"],
                      column_metadata=wire_metadata(sql, "delivery_date", "erp_db.orders.delivery_date"))
        self.sync_image(o, ref)
        line = next(line for line in d["body"].splitlines() if line.startswith("图片已核实 [I1]"))
        d["body"] = d["body"].replace(line, line + "\n\n字段含义: " + catalog()["erp_db.orders.delivery_date"]["visible_description"]["zh"])
        return o, ref

    @staticmethod
    def sync_image(o, ref):
        for review in o["case"]["image_reviews"]:
            for field in review["result"]["fields"]:
                if field["field"] == ref["field"]:
                    field["source"] = deepcopy(ref["business_source"])
        o["case"]["facts"][ref["field"]]["business_source"] = deepcopy(ref["business_source"])

    def test_image_uses_own_persisted_execution_and_frozen_description(self):
        o, ref = self.image()
        self.assertNotEqual(ref["business_source"]["run_id"], query(o)["evidence_payload"]["run_id"])
        verdict = self.evaluate("development-images-001", o)
        self.assertTrue(verdict["fact_correct"], verdict)

    def test_image_metadata_counterexamples_cannot_borrow_later_query(self):
        for change in ("no_original_sql", "wrong_hash", "wrong_output_columns", "wrong_output_field", "wrong_field",
                       "fabricated_description", "stale", "no_ids", "missing_review", "wrong_review",
                       "wrong_attachment", "wrong_digest", "wrong_fact_source", "unverified"):
            with self.subTest(change=change):
                o, ref = self.image()
                source = ref["business_source"]
                if change == "no_original_sql":
                    # A correct later query cannot rescue missing original SQL.
                    later = query(o)["evidence_payload"]["steps"][0]
                    later["column_metadata"] = {"delivery_date": wire_metadata(later["final_sql"], "delivery_date", "erp_db.orders.delivery_date")}
                    source.pop("final_sql")
                elif change == "wrong_hash":
                    source["query_hash"] = "0" * 64
                elif change == "wrong_output_columns":
                    source["output_columns"].remove("delivery_date")
                elif change == "wrong_output_field":
                    source["column_metadata"]["output_field"] = "order_status"
                elif change == "wrong_field":
                    source["column_metadata"] = wire_metadata(source["final_sql"], "delivery_date", "erp_db.orders.order_status")
                elif change == "fabricated_description":
                    source["column_metadata"]["description"] = "实际送达日期。"
                    rehash(source["column_metadata"])
                elif change == "stale":
                    current(o)["created_at"] += 301
                elif change == "no_ids":
                    source["run_id"] = source["trace_id"] = ""
                self.sync_image(o, ref)
                if change == "missing_review":
                    o["case"]["image_reviews"] = []
                elif change == "wrong_review":
                    o["case"]["image_reviews"][0]["case_id"] = "other"
                elif change == "wrong_attachment":
                    ref["attachment_id"] = "other"
                elif change == "wrong_digest":
                    ref["sha256"] = "0" * 64
                elif change == "wrong_fact_source":
                    o["case"]["facts"]["date"]["source"] = "image_candidate"
                elif change == "unverified":
                    next(f for f in o["case"]["image_reviews"][0]["result"]["fields"] if f["field"] == "date")["outcome"] = "unverified"
                self.assertFalse(self.evaluate("development-images-001", o)["fact_correct"])

    def test_valid_policy_effective_dates_and_expiry_are_bound_to_cited_version(self):
        for sid in ("development-policies-001", "development-policies-002"):
            o = deepcopy(read(OLD / (sid + ".json"))["observed"])
            o["side_effects"]["handoff"] = 0
            ref = next(r for r in current(o)["evidence"] if r.get("policy_revision"))
            p = ref["policy"]
            line = f"生效时点：{p['effective_at']}；失效时点：{p['expires_at'] or '未设置'}"
            current(o)["body"] = current(o)["body"].replace("\n\n" + NOTE, "\n\n" + line + "\n\n" + NOTE)
            self.assertTrue(self.evaluate(sid, o)["fact_correct"])
            for change in ("wrong_start", "wrong_end", "wrong_scope", "no_source", "appended_promise", "no_heading"):
                with self.subTest(sid=sid, change=change):
                    changed = deepcopy(o)
                    d = current(changed)
                    if change == "wrong_start":
                        d["body"] = d["body"].replace(line, line.replace(p["effective_at"], "2025-01-01T00:00:00Z"))
                    elif change == "wrong_end":
                        d["body"] = d["body"].replace(line, line.split("失效时点：")[0] + "失效时点：" + ("未设置" if p["expires_at"] else "2030-01-01T00:00:00Z"))
                    elif change == "wrong_scope":
                        next(r for r in d["evidence"] if r.get("policy_revision"))["policy"]["region"] = "NV"
                    elif change == "no_source":
                        d["evidence"] = []
                    elif change == "appended_promise":
                        d["body"] = d["body"].replace(line, line + "；费用全免。")
                    elif change == "no_heading":
                        d["body"] = "\n".join(l for l in d["body"].splitlines() if not l.startswith("政策摘录"))
                    self.assertFalse(self.evaluate(sid, changed)["fact_correct"])

    def empty_policy(self):
        scope = deepcopy(self.scenarios["development-policies-004"]["policy_scope"])
        body = EMPTY_POLICY_NOTICE + "\n\n" + NOTE
        state = {"attempted": True, "policy_lookup": True, "lookup_status": "empty", "degraded": False,
                 "lookup_scope": scope, "evidence_ok": None}
        result = {"case_applied": True, "draft_id": "draft", "case_id": "case", "response": body,
                  "knowledge": state, "knowledge_sources": [], "knowledge_used": False}
        return {"completed": True, "runs": [{"run_id": "run", "status": "succeeded", "result": result}],
                "case": {"id": "case", "status": "in_progress", "control_mode": "assistant", "pending_fields": [],
                         "policy_scope": scope, "drafts": [{"id": "draft", "run_id": "run", "valid": True, "body": body, "evidence": []}]},
                "side_effects": {"handoff": 0, "notification": 0}}

    def test_successful_empty_lookup_is_distinct_from_failure_or_missing_state(self):
        o = self.empty_policy()
        self.assertTrue(self.evaluate("development-policies-004", o)["fact_correct"])
        for change in ("failed", "missing", "found", "degraded", "not_attempted", "wrong_scope", "wrong_time",
                       "sources", "other_run", "not_applied", "wrong_draft", "contradictory_search", "failed_run",
                       "same_line_promise", "new_line_promise", "failure_sentence", "legacy_failure_abstention"):
            with self.subTest(change=change):
                changed = deepcopy(o)
                result = changed["runs"][0]["result"]
                state = result["knowledge"]
                if change in {"failed", "found"}:
                    state["lookup_status"] = "unavailable" if change == "failed" else "found"
                elif change == "missing":
                    state.pop("lookup_status")
                elif change == "degraded":
                    state["degraded"] = True
                elif change == "not_attempted":
                    state["attempted"] = False
                elif change == "wrong_scope":
                    state["lookup_scope"]["region"] = "NV"
                elif change == "wrong_time":
                    state["lookup_scope"]["as_of"] = "2030-01-01T00:00:00Z"
                elif change == "sources":
                    result["knowledge_sources"] = [{"policy_revision": "unaccounted"}]
                elif change == "other_run":
                    current(changed)["run_id"] = "other"
                elif change == "not_applied":
                    result["case_applied"] = False
                elif change == "wrong_draft":
                    result["draft_id"] = "other"
                elif change == "contradictory_search":
                    result["trajectory"] = [{"tool": "knowledge_search", "trace": {"lookup_status": "unavailable"}}]
                elif change == "failed_run":
                    changed["runs"][0]["status"] = "failed"
                elif change == "same_line_promise":
                    current(changed)["body"] = current(changed)["body"].replace(EMPTY_POLICY_NOTICE, EMPTY_POLICY_NOTICE + "费用全免。")
                elif change == "new_line_promise":
                    current(changed)["body"] += "\n费用全免。"
                elif change == "failure_sentence":
                    state["lookup_status"] = "unavailable"
                    current(changed)["body"] = "政策查询尚未完成或通过核验，不能确定发布状态与适用性。"
                elif change == "legacy_failure_abstention":
                    state["lookup_status"] = "unavailable"
                    current(changed)["body"] = "材料已保存。目前缺少适用的已发布政策或有效业务证据，请补充适用产品、区域、受众及业务记录后核实。"
                result["response"] = current(changed)["body"]
                self.assertFalse(self.evaluate("development-policies-004", changed)["fact_correct"])
        self.assertFalse(self.evaluate("development-policies-001", o)["fact_correct"])
        scenario = deepcopy(self.scenarios["development-policies-004"])
        document = next(d for d in self.corpus["policies"] if d["policy"]["status"] == "published"
                        and d["policy"]["product"] == scenario["policy_scope"]["product"])
        scope = {k: document["policy"][k] for k in ("product", "region", "audience")}
        scope["as_of"] = document["policy"]["effective_at"]
        scenario["policy_scope"] = o["case"]["policy_scope"] = o["runs"][0]["result"]["knowledge"]["lookup_scope"] = scope
        self.assertFalse(score(scenario, o, self.corpus)["fact_correct"])

    def test_original_three_failures_and_helper_ids_are_not_rewritten(self):
        for sid in ("development-orders-003", "development-policies-001", "development-policies-002"):
            original = read(OLD / (sid + ".json"))
            verdict = self.evaluate(sid, original["observed"])
            self.assertFalse(verdict["fact_correct"])
            self.assertEqual(verdict["invariant_violations"], ["unauthorized_or_duplicate_handoff"])
        self.assertFalse(self.evaluate("development-orders-003", self.delivery(no_handoff=False))["fact_correct"])

    def test_common_facts_uses_identical_catalog_rules_and_keeps_real_errors(self):
        scenario = self.scenarios["development-orders-003"]
        plain = current(self.delivery(language="en"))["body"]
        for text in (plain, plain.replace("Delivery date", "交付日期").replace("Delivery status", "配送状态")):
            first = common_score(scenario, text, self.corpus)
            self.assertEqual(first, common_score(deepcopy(scenario), text, deepcopy(self.corpus)))
            self.assertEqual(first["status"], "review_required", first)
            self.assertIn("unparsed_field_meaning_provenance", first["errors"])
            self.assertNotIn("unbound_numeric_claim", first["errors"])
            self.assertFalse(common_score(scenario, text.replace("2026-11-12", "2030-01-01"), self.corpus)["fact_correct"])
            self.assertFalse(common_score(scenario, text + "\n另收 999999 美元。", self.corpus)["fact_correct"])
        wrong = plain.replace(catalog()["erp_db.orders.delivery_date"]["visible_description"]["en"],
                              catalog()["erp_db.orders.signed_date"]["visible_description"]["en"])
        self.assertEqual(common_score(scenario, wrong, self.corpus)["status"], "incorrect")
        self.assertIn("incorrect_field_meaning", common_score(scenario, wrong, self.corpus)["errors"])

    def test_common_known_numeric_definition_is_reviewable_not_a_row_number(self):
        scenario = deepcopy(self.scenarios["development-orders-003"])
        order = scenario["truth"]["allowed_entities"][0]
        row = next(r for r in self.corpus["records"]["prod_db"]["work_order"] if r["order_no"] == order)
        scenario["truth"]["facts"] = [{"entity": order, "field": "is_rush", "value": row["is_rush"], "unit": None,
                                       "source": {"database": "prod_db", "table": "work_order"}}]
        line = "字段含义: " + catalog()["prod_db.work_order.is_rush"]["visible_description"]["zh"]
        text = f"订单 {order}\n- 是否加急: {row['is_rush']}\n" + line
        verdict = common_score(scenario, text, self.corpus)
        self.assertEqual(verdict["status"], "review_required", verdict)
        self.assertNotIn("unbound_numeric_claim", verdict["errors"])
        self.assertFalse(common_score(scenario, text.replace(line, line + "另收999美元。"), self.corpus)["fact_correct"])
        self.assertFalse(common_score(scenario, text + "\n另收999美元。", self.corpus)["fact_correct"])
        self.assertFalse(common_score(scenario, text.replace(f"是否加急: {row['is_rush']}", "是否加急: 999"), self.corpus)["fact_correct"])

    @staticmethod
    def fictional_common_case():
        # Independently invented values, unrelated to either version's answers.
        entity = "SO-2026-99991"
        values = {"contract_amount": 123.5, "order_status": "生产中", "delivery_date": "2026-12-01"}
        source = {"database": "erp_db", "table": "orders"}
        scenario = {"group": "orders", "truth": {"allowed_entities": [entity], "facts": [
            {"entity": entity, "field": k, "value": v, "unit": "USD" if k == "contract_amount" else None,
             "source": source} for k, v in values.items()]}}
        corpus = {"records": {"erp_db": {"orders": [{"order_no": entity, **values}]}}}
        return scenario, corpus, entity

    def test_common_balanced_markdown_units_and_chinese_dates_apply_to_both_versions(self):
        scenario, corpus, entity = self.fictional_common_case()
        for amount in ("USD 123.50", "123.50 USD", "美元123.50", "123.50美元", "US$ 123.50", "USD 123.50美元"):
            for day in ("2026年12月1日", "2026-12-01"):
                with self.subTest(amount=amount, day=day):
                    text = f"订单 {entity}\n**合同金额：{amount}**；**订单状态：生产中**；**交付日期：{day}**"
                    verdict = common_score(scenario, text, corpus)
                    self.assertEqual(verdict["status"], "correct", verdict)
                    self.assertEqual(verdict, common_score(deepcopy(scenario), text, deepcopy(corpus)))
        for style in ("**{}**", "__{}__", "`{}`", "*{}*"):
            text = f"订单 {entity}\n" + style.format("合同金额") + "：USD 123.50；" + style.format("订单状态：生产中") + "；" + style.format("交付日期：2026年12月1日")
            self.assertTrue(common_score(scenario, text, corpus)["fact_correct"])

    def test_common_discussion_without_a_date_value_stays_pending(self):
        scenario, corpus, entity = self.fictional_common_case()
        for discussion in ("交付日期尚未到", "交付日期记录", "交付日期：尚未核实", "交付日期待确认"):
            text = f"订单 {entity}\n合同金额：USD 123.50；订单状态：生产中；{discussion}"
            verdict = common_score(scenario, text, corpus)
            self.assertEqual(verdict["status"], "review_required", verdict)
            self.assertIsNone(verdict["fact_correct"])
            self.assertNotIn("incorrect_claim:delivery_date", verdict["errors"])

    def test_common_format_normalization_never_rescues_explicit_wrong_values(self):
        scenario, corpus, entity = self.fictional_common_case()
        body = f"订单 {entity}\n**合同金额：USD 123.50**；**订单状态：生产中**；**交付日期：2026年12月1日**"
        changes = [body.replace("123.50", "999.50"), body.replace("USD", "CNY"), body.replace("USD ", ""),
                   body.replace("生产中", "已取消"), body.replace("生产中", "已发射"),
                   body.replace("2026年12月1日", "2026年12月2日"), body.replace("2026年12月1日", "2026年2月30日"),
                   body + "\n**另收999美元**", body + "\n合同金额：USD 999.50", body + "\n交付日期：2030-01-01"]
        for text in changes:
            with self.subTest(text=text):
                verdict = common_score(scenario, text, corpus)
                self.assertEqual(verdict["status"], "incorrect", verdict)
                self.assertFalse(verdict["fact_correct"])
        for text in (body + "\n**保证明天免费送达**", body.replace("USD 123.50**", "USD 123.50")):
            self.assertIsNot(common_score(scenario, text, corpus)["fact_correct"], True)

    def test_independently_authored_format_handoff_and_five_negative_controls(self):
        fixtures = read(IMAGE / "common-format-reproductions.json")
        self.assertFalse(fixtures["contains_real_answers"])
        self.assertEqual(len(fixtures["cases"]), 17)
        for case in fixtures["cases"]:
            with self.subTest(case=case["id"]):
                verdict = common_score(case["scenario"], case["variant"], case["context"])
                if case["kind"] == "negative_control":
                    self.assertEqual(verdict["status"], "incorrect", verdict)
                    self.assertFalse(verdict["fact_correct"])
                else:
                    self.assertIn(verdict["status"], {"correct", "review_required"}, verdict)
                self.assertEqual(verdict, common_score(deepcopy(case["scenario"]), case["variant"], deepcopy(case["context"])))

    def test_table_quote_time_and_relationship_controls_preserve_errors(self):
        fixtures = {c["id"]: c for c in read(IMAGE / "common-format-reproductions.json")["cases"]}
        table = fixtures["table_rows"]
        for text in (table["variant"].replace("7件", "8件"), table["variant"].replace("7件", "7 USD"),
                     table["variant"].replace("| 处理中 | 7件 |", "| 处理中 | 7件 | 19 USD |"),
                     table["variant"] + "\n额外费用：19 USD"):
            self.assertEqual(common_score(table["scenario"], text)["status"], "incorrect")
        quoted = fixtures["quoted_status"]
        self.assertEqual(common_score(quoted["scenario"], quoted["variant"].replace("配送中", "已取消"))["status"], "incorrect")
        clock = fixtures["query_timestamp"]
        for text in (clock["variant"] + "\n另收19 USD", clock["variant"].replace("09:10 UTC", "09:10 UTC；另收19 USD"),
                     clock["variant"].replace("2027年2月3日 09:10 UTC", "19 USD"),
                     clock["variant"].replace("2027年2月3日", "2027年2月30日")):
            self.assertEqual(common_score(clock["scenario"], text, clock["context"])["status"], "incorrect")
        linked = fixtures["related_part_name"]
        for change in ("missing_source", "other_order_source", "other_material", "extra_order"):
            with self.subTest(change=change):
                text, context = linked["variant"], deepcopy(linked["context"])
                if change == "missing_source":
                    context = None
                elif change == "other_order_source":
                    context["records"]["svc_db"]["replacement_part"][0]["order_no"] = "SO-2026-99002"
                elif change == "other_material":
                    text = text.replace("Hinge BX-99001-24", "Hinge BX-99002-24")
                else:
                    text += "\n订单 SO-2026-99002"
                self.assertEqual(common_score(linked["scenario"], text, context)["status"], "incorrect")

    def test_common_null_statements_bind_to_the_labeled_source_field(self):
        scenario, corpus, entity = self.fictional_common_case()
        # Same output name in two sources, with different values. A nearby NULL
        # must never authorize a missing-value claim about the delivery phone.
        corpus["records"]["erp_db"]["orders"][0]["phone"] = None
        corpus["records"]["svc_db"] = {"delivery": [{"order_no": entity, "phone": "5550199"}]}
        base = f"订单 {entity}\n合同金额：123.50 USD\n订单状态：生产中\n交付日期：2026-12-01"
        for field, value in (("手机号", "未登记"), ("Customer phone", "Not registered")):
            text = base + f"\n{field}: {value}"
            computed = common_score(scenario, text, corpus)
            self.assertEqual(computed["status"], "correct", computed)
            self.assertEqual(computed, common_score(deepcopy(scenario), text, deepcopy(corpus)))
        for value in ("未登记", "Not registered"):
            computed = common_score(scenario, base + "\nDelivery phone: " + value, corpus)
            self.assertEqual(computed["status"], "incorrect", computed)
        changed = deepcopy(corpus)
        changed["records"]["erp_db"]["orders"][0]["phone"] = "5550101"
        self.assertEqual(common_score(scenario, base + "\n手机号: 未登记", changed)["status"], "incorrect")
        self.assertEqual(common_score(scenario, base + "\n手机号: 5550101", corpus)["status"], "incorrect")
        self.assertIsNot(common_score(scenario, base + "\n手机号: 未登记")["fact_correct"], True)


if __name__ == "__main__":
    unittest.main()
