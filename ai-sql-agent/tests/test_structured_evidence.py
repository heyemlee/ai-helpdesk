"""Execution time survives serialization; result rows, not prose, produce evidence."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ai_sql_agent.api.result_summary import summarize_result
from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.execution.objects import MCPExecutionRequest
from ai_sql_agent.execution.sqlite_executor import SQLiteMCPExecutor


def result(execution):
    step = SimpleNamespace(database="erp_db", sql=execution.sql, final_sql=execution.sql,
                           execution_result=execution.to_dict(), repair_attempts=[], validation={},
                           column_units={"amount": "USD"}, source_records=[])
    return SimpleNamespace(pipeline_result=SimpleNamespace(step_logs=[step], selected_databases=["erp_db"],
        selected_tables={}, degraded=False, degrade_reason=""), decision=SimpleNamespace(
        route=SimpleNamespace(value="database_query"), confidence=1.0, reason="test"),
        answer="Amount 150 and compensation 5000 (for reference only)", queried_database=True)


class StructuredEvidenceTest(unittest.TestCase):
    def test_real_execution_and_old_snapshot_keep_distinct_times(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE orders(order_ref TEXT, amount INT)")
                db.execute("INSERT INTO orders VALUES('SO-Ab',150)")
            with authorization_scope(ALLOW_ALL):
                execution = SQLiteMCPExecutor("erp_db", path).execute(MCPExecutionRequest("erp_db", "SELECT * FROM orders"))
            self.assertTrue(execution.success)
            observed = datetime.fromisoformat(execution.observed_at.replace("Z", "+00:00"))
            self.assertLess((datetime.now(timezone.utc) - observed).total_seconds(), 10)
            execution.as_of, execution.as_of_source = "2020-01-01T00:00:00Z", "snapshot"
            saved = result(execution)
            first = summarize_result(saved)
            self.assertEqual(first.observed_at, observed.isoformat())
            self.assertEqual(first.evidence[1].value, 150)
            self.assertNotIn("5000", str(first.data_part()))
            self.assertEqual(first.evidence[1].unit, "USD")
            saved.pipeline_result.step_logs[0].execution_result["observed_at"] = "2020-01-02T00:00:00Z"
            old = summarize_result(saved)
            replay = summarize_result(saved)
            self.assertEqual(old.observed_at, "2020-01-02T00:00:00+00:00")
            self.assertEqual(old.observed_at, replay.observed_at)
            self.assertEqual(old.evidence, replay.evidence)
            self.assertEqual(first.evidence, old.evidence)  # stable IDs do not depend on event seq / summary time
            saved.pipeline_result.step_logs[0].execution_result.pop("observed_at")
            self.assertIsNone(summarize_result(saved).observed_at)
