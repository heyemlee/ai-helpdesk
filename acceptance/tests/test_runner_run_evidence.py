"""Accepted Runs survive polling failures without becoming successful results."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from acceptance.budget import Ledger
from acceptance.campaign import ScopedLedger
from acceptance.common import DATA, dataset, read, sha
from acceptance.oracle import score
from acceptance.runner import Employee, execute
from acceptance.usage_evidence import check_attribution


class RunEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.corpus = dataset()[1]
        self.limits = {
            "scope": "release_and_baseline", "confirmed": True, "id": "unit-run-evidence",
            "user_approval_reference": "offline-unit-test-only", "requests": 20, "scenarios": 1,
            "model": "gpt-6.1-sol", "max_output_tokens": 2048,
            "supplier_base_url": "https://supplier.invalid/v1",
            "corpus_sha256": sha((DATA / "manifest.json").read_bytes()),
        }
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite", self.limits)
        self.clock = 0
        self.polls = []

    def fixture(self, name):
        observed = read(Path(__file__).parent / "fixtures" / (name + ".json"))["observed"]
        scenario = next(s for s in self.corpus["scenarios"] if s["id"] == observed["id"])
        return scenario, observed

    def charge(self, run_id):
        # Synthetic receipts exercise the real ledger validator, never the gateway.
        for service in ("helpdesk", "sql"):
            attempt = self.ledger.reserve(service, "responses", {
                "model": self.limits["model"], "max_output_tokens": 2048,
            }, run_id=run_id if service == "helpdesk" else "sql-" + run_id,
                parent_run_id=run_id if service == "sql" else None)
            self.ledger.finish(attempt, status=200, model=self.limits["model"],
                usage={"input_tokens": 1, "output_tokens": 1}, receipt={
                    "transport": "supplier_http", "supplier_base_url": self.limits["supplier_base_url"],
                    "endpoint": "responses", "requested_model": self.limits["model"],
                    "response_sha256": sha({"synthetic_attempt": attempt}),
                    "response_id": "synthetic-" + str(attempt),
                })

    def observe(self, fixture_name, replies, *, timeout=3, case_status=200, effects_error=None):
        scenario, fixture = self.fixture(fixture_name)
        first_run = fixture["runs"][0]["run_id"]
        pending = {run: iter(values) for run, values in replies.items()}
        charged = set()

        def accepted(run_id):
            if run_id not in charged:
                charged.add(run_id)
                self.charge(run_id)

        def respond(request):
            path = request.url.path
            if request.method == "POST" and path == "/auth/conversations":
                return httpx.Response(201, json={"conv_id": "isolated-test-conversation"})
            if request.method == "POST" and path == "/attachments":
                return httpx.Response(201, json={"id": "isolated-test-image"})
            if request.method == "POST" and path == "/tickets/cases/inputs":
                accepted(first_run)
                return httpx.Response(202, json={"run_id": first_run, "case_id": fixture["case"]["case_id"]})
            if request.method == "GET" and path.startswith("/tickets/cases/runs/"):
                run_id = path.rsplit("/", 1)[-1]
                self.polls.append(run_id)
                reply = next(pending[run_id])
                if isinstance(reply, int):
                    return httpx.Response(reply, json={"detail": "synthetic polling failure"})
                followup = (reply.get("result") or {}).get("next_run_id")
                if followup:
                    accepted(followup)
                return httpx.Response(200, json=reply)
            if request.method == "GET" and path == "/tickets/" + fixture["case"]["case_id"] + "/case":
                return httpx.Response(case_status, json=fixture["case"] if case_status == 200 else {"detail": "synthetic Case outage"})
            raise AssertionError("Unexpected test request: " + str(request.url))

        def advance(seconds):
            self.clock += seconds

        employee = Employee({"api": "https://fixture.invalid", "origin": "https://fixture.invalid"}, "test")
        employee.close()
        employee.client = httpx.Client(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond))
        self.addCleanup(employee.close)
        with patch("acceptance.runner.time.monotonic", side_effect=lambda: self.clock), \
                patch("acceptance.runner.time.sleep", side_effect=advance), \
                patch("acceptance.runner.effects", new=AsyncMock(return_value=fixture["side_effects"], side_effect=effects_error)):
            observed = execute(employee, scenario, DATA, ScopedLedger(self.ledger, "round-1"), timeout=timeout)
        scope = "round-1:" + scenario["id"]
        contexts = {scope: {r["run_id"] for r in observed["runs"]}}
        counts = check_attribution(self.ledger.report(), contexts, {"round-1"})
        self.assertEqual(contexts[scope], charged)
        self.assertEqual(counts[scope], 2 * len(charged))
        return observed, score(scenario, observed, self.corpus)

    def test_terminal_state_updates_the_original_run_record(self):
        _, fixture = self.fixture("orders")
        final = fixture["runs"][0]
        run = final["run_id"]
        observed, result = self.observe("orders", {run: [
            {"run_id": run, "status": "queued"}, {"run_id": run, "status": "running"}, final,
        ]})
        self.assertEqual(observed["runs"], [final])
        self.assertTrue(observed["completed"])
        self.assertTrue(result["fact_correct"])
        self.assertEqual(self.polls, [run, run, run])

    def test_timeout_before_polling_retains_only_the_accepted_identity(self):
        _, fixture = self.fixture("orders")
        run = fixture["runs"][0]["run_id"]
        observed, result = self.observe("orders", {run: []}, timeout=0)
        self.assertEqual(observed["runs"], [{"run_id": run}])
        self.assertEqual(observed["failure"]["type"], "TimeoutError")
        self.assertFalse(observed["completed"])
        self.assertFalse(result["run_terminal"])
        self.assertFalse(result["fact_correct"])
        self.assertEqual(self.polls, [])

    def test_timeout_retains_the_last_observed_nonterminal_state(self):
        _, fixture = self.fixture("orders")
        run = fixture["runs"][0]["run_id"]
        running = {"run_id": run, "status": "running", "attempt": 1, "result": None}
        observed, result = self.observe("orders", {run: [running]}, timeout=.25)
        self.assertEqual(observed["runs"], [running])
        self.assertEqual(observed["failure"]["type"], "TimeoutError")
        self.assertFalse(observed["completed"])
        self.assertFalse(result["run_terminal"])
        self.assertFalse(result["fact_correct"])

    def test_first_poll_http_failure_keeps_the_accepted_identity_without_retry(self):
        _, fixture = self.fixture("orders")
        run = fixture["runs"][0]["run_id"]
        observed, result = self.observe("orders", {run: [502]})
        self.assertEqual(observed["runs"], [{"run_id": run}])
        self.assertIn("HTTP 502", observed["failure"]["detail"])
        self.assertFalse(observed["completed"])
        self.assertFalse(result["fact_correct"])
        self.assertEqual(self.polls, [run])

    def test_later_poll_http_failure_keeps_the_last_state_without_retry(self):
        _, fixture = self.fixture("orders")
        run = fixture["runs"][0]["run_id"]
        running = {"run_id": run, "status": "running", "attempt": 1}
        observed, result = self.observe("orders", {run: [running, 503]})
        self.assertEqual(observed["runs"], [running])
        self.assertIn("HTTP 503", observed["failure"]["detail"])
        self.assertFalse(observed["completed"])
        self.assertFalse(result["run_terminal"])
        self.assertFalse(result["fact_correct"])
        self.assertEqual(self.polls, [run, run])

    def test_image_followup_keeps_both_run_identities_and_supplier_attribution(self):
        _, fixture = self.fixture("images")
        first, second = fixture["runs"]
        observed, result = self.observe("images", {first["run_id"]: [first], second["run_id"]: [second]})
        self.assertEqual(observed["runs"], [first, second])
        self.assertTrue(observed["completed"])
        self.assertTrue(result["fact_correct"])

    def test_poll_and_final_snapshot_failures_still_return_the_accepted_run(self):
        _, fixture = self.fixture("orders")
        run = fixture["runs"][0]["run_id"]
        observed, result = self.observe("orders", {run: [503]}, case_status=503,
                                       effects_error=ConnectionError("synthetic database outage"))
        self.assertEqual(observed["runs"], [{"run_id": run}])
        self.assertIn("HTTP 503", observed["failure"]["detail"])
        self.assertEqual([e["stage"] for e in observed["observation_errors"]], ["case", "side_effects"])
        self.assertNotIn("case", observed)
        self.assertNotIn("side_effects", observed)
        self.assertFalse(observed["completed"])
        self.assertFalse(result["fact_correct"])
        self.assertIn("side_effects_unmeasured", result["invariant_violations"])
        self.assertEqual(self.polls, [run])

    def test_terminal_run_with_unavailable_side_effect_evidence_cannot_pass(self):
        _, fixture = self.fixture("orders")
        final = fixture["runs"][0]
        observed, result = self.observe("orders", {final["run_id"]: [final]},
                                       effects_error=ConnectionError("synthetic database outage"))
        self.assertEqual(observed["runs"], [final])
        self.assertEqual(observed["observation_errors"][0]["stage"], "side_effects")
        self.assertNotIn("side_effects", observed)
        self.assertTrue(result["run_terminal"])
        self.assertFalse(observed["completed"])
        self.assertIn("side_effects_unmeasured", result["errors"])
        self.assertIn("side_effects_unmeasured", result["invariant_violations"])
        self.assertFalse(result["fact_correct"])

    def test_image_followup_timeout_preserves_the_child_without_marking_it_terminal(self):
        _, fixture = self.fixture("images")
        first, second = fixture["runs"]
        running = {"run_id": second["run_id"], "status": "running"}
        observed, result = self.observe("images", {first["run_id"]: [first], second["run_id"]: [running]}, timeout=.25)
        self.assertEqual(observed["runs"], [first, running])
        self.assertEqual(observed["failure"]["type"], "TimeoutError")
        self.assertFalse(observed["completed"])
        self.assertFalse(result["run_terminal"])
        self.assertFalse(result["fact_correct"])

    def test_failed_terminal_run_remains_a_failed_business_result(self):
        _, fixture = self.fixture("orders")
        failed = copy.deepcopy(fixture["runs"][0])
        failed.update(status="failed", result=None, error="synthetic execution failure")
        observed, result = self.observe("orders", {failed["run_id"]: [failed]})
        self.assertEqual(observed["runs"], [failed])
        self.assertTrue(result["run_terminal"])
        self.assertFalse(result["fact_correct"])


if __name__ == "__main__":
    unittest.main()
