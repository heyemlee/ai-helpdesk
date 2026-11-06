"""Budget binding for development reruns; never contact a provider."""
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acceptance.common import DATA, sha, write
from acceptance.runner import development_ledger, main


class DevelopmentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = self.root / "stack"
        self.stack.mkdir()
        self.budget = self.root / "approval.json"
        self.approval = {"scope": "release_and_baseline", "confirmed": True,
                         "purpose": "development_revalidation", "id": "test-development-001",
                         "user_approval_reference": "unit-test-only", "requests": 100, "scenarios": 12,
                         "model": "gpt-6.1-sol", "max_output_tokens": 2048,
                         "supplier_base_url": "https://supplier.invalid/v1",
                         "corpus_sha256": sha((DATA / "manifest.json").read_bytes())}
        self.cfg = {"root": str(self.stack), "split": "development", "manifest_sha256": self.approval["corpus_sha256"]}
        self.provenance = {"real_provider": True, "provider_mode": "real", "execution": "immutable_containers"}
        write(self.budget, self.approval)
        write(self.stack / "provider-state/approval.json", self.approval)
        write(self.stack / "private-config.json", self.cfg)
        write(self.stack / "provenance.json", self.provenance)

    def test_matching_budget_uses_separate_persistent_ledger_without_reset(self):
        with patch("acceptance.gateway.ROOT", self.root):
            ledger = development_ledger(self.stack, self.cfg, self.provenance, self.budget)
            ledger.register_scenario("development-images-001")
            ledger.reserve("sql", "responses", {"model": "gpt-6.1-sol", "max_output_tokens": 2048})
            reopened = development_ledger(self.stack, self.cfg, self.provenance, self.budget)
        self.assertEqual(reopened.report()["reserved_requests"], 1)
        self.assertEqual(ledger.path, self.root / ".stage6/test-development-001.sqlite")
        self.assertFalse((self.root / ".stage6/calibration.sqlite").exists())

    def test_wrong_identity_limits_destination_or_purpose_is_rejected_before_opening_ledger(self):
        for key, value in (("id", "test-other-001"), ("requests", 101), ("scenarios", 13),
                           ("supplier_base_url", "https://different.invalid/v1"), ("purpose", "formal_acceptance")):
            with self.subTest(key=key), patch("acceptance.runner.release_ledger") as loader:
                write(self.budget, {**self.approval, key: value})
                with self.assertRaises(ValueError):
                    development_ledger(self.stack, self.cfg, self.provenance, self.budget)
                loader.assert_not_called()

    def test_wrong_split_or_mutable_stack_is_rejected(self):
        for target, key, value in (("cfg", "split", "release"), ("provenance", "execution", "source_copy"),
                                   ("provenance", "real_provider", False), ("cfg", "root", str(self.root))):
            cfg, provenance = copy.deepcopy(self.cfg), copy.deepcopy(self.provenance)
            (cfg if target == "cfg" else provenance)[key] = value
            with self.subTest(key=key), patch("acceptance.runner.release_ledger") as loader:
                with self.assertRaises(ValueError):
                    development_ledger(self.stack, cfg, provenance, self.budget)
                loader.assert_not_called()

    def test_real_flags_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--stack", str(self.stack), "--output", str(self.root / "new"),
                  "--real-calibration", "--budget", str(self.budget)])
        self.assertEqual(caught.exception.code, 2)

    def test_real_disabled_by_default_and_release_scenario_rejected(self):
        for extra in ([], ["--budget", str(self.budget), "--scenario", "release-images-001"]):
            with self.subTest(extra=extra), patch("acceptance.runner.prepare") as prepare:
                with self.assertRaises(ValueError):
                    main(["--stack", str(self.stack), "--output", str(self.root / "new"), *extra])
                prepare.assert_not_called()

    def test_existing_output_is_rejected_before_opening_ledger(self):
        output = self.root / "evidence"
        output.mkdir()
        original = output / "report.json"
        original.write_text("original evidence")
        with patch("acceptance.runner.development_ledger") as loader, self.assertRaises(ValueError):
            main(["--stack", str(self.stack), "--output", str(output), "--budget", str(self.budget)])
        loader.assert_not_called()
        self.assertEqual(original.read_text(), "original evidence")
