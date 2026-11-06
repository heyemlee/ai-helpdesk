"""Versioned corpus revisions must preserve gold even after files are rehashed."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.common import DATA, ROOT, dataset, read, sha, write
from acceptance.revise_v3 import SOURCE, SOURCE_MANIFEST_SHA256, freeze, validate_revision


def file_hashes(directory):
    return {p.relative_to(directory).as_posix(): sha(p.read_bytes()) for p in directory.rglob("*") if p.is_file()}


class CorpusRevisionTests(unittest.TestCase):
    def test_frozen_revision_has_exact_change_counts_and_preserves_old_corpora(self):
        proof = validate_revision()
        self.assertEqual(DATA.name, "v3")
        self.assertEqual({split: len(ids) for split, ids in proof["changed_policy_questions"].items()},
                         {"development": 15, "release": 50})
        self.assertEqual({split: len(paths) for split, paths in proof["changed_paths"].items()},
                         {"development": 46, "release": 151})
        self.assertEqual(proof["unchanged_non_policy_scenarios"], {"development": 39, "release": 150})
        self.assertEqual(len(proof["unchanged_image_files"]), 55)
        self.assertEqual(sha((SOURCE / "manifest.json").read_bytes()), SOURCE_MANIFEST_SHA256)
        v1 = ROOT / "acceptance/data/v1"
        self.assertEqual(sha((v1 / "manifest.json").read_bytes()),
                         "c918170dfc710f153d96ae1bb6b7b54230e8eb06e933589f29477077701e6ea6")
        dataset(v1)

    def test_question_targets_match_document_content_without_inventing_deadlines(self):
        counts = {"processing_time": 0, "procedure_steps": 0}
        _, *corpora = dataset()
        for corpus in corpora:
            for scenario in corpus["scenarios"]:
                if scenario["group"] != "policies":
                    continue
                question = scenario["steps"][0]["message"]
                with self.subTest(scenario=scenario["id"]):
                    self.assertIn("有效版本", question)
                    self.assertIn("引用", question)
                    if scenario["subtype"] == "procedure":
                        counts["procedure_steps"] += 1
                        self.assertIn("办理步骤", question)
                        self.assertNotIn("时限", question)
                        self.assertNotIn("工作日", "".join(scenario["truth"]["required_text"]))
                    else:
                        counts["processing_time"] += 1
                        self.assertIn("处理时限", question)
                        self.assertNotIn("办理步骤", question)
        self.assertEqual(counts, {"processing_time": 52, "procedure_steps": 13})

    def test_manifest_discloses_prior_evaluation_and_unchanged_thresholds(self):
        previous, *_ = dataset(SOURCE)
        current, *_ = dataset()
        history = current["revision"]["measurement_history"]
        self.assertTrue(history["v2_real_supplier_calls_before_revision"])
        self.assertTrue(history["v2_release_corpus_has_prior_model_exposure"])
        self.assertFalse(history["entire_corpus_is_previously_unmeasured_or_blind"])
        self.assertFalse(history["prior_formal_baseline_answer_contents_consulted"])
        self.assertNotIn("premeasurement_revision", current)
        self.assertEqual(current["prior_manifest_sha256"], SOURCE_MANIFEST_SHA256)
        for key in ("thresholds", "release_counts", "development_counts", "calibration_scenarios", "deterministic_only", "isolation"):
            self.assertEqual(current[key], previous[key])

    def test_rehashed_gold_scope_records_and_non_policy_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "corpus"
            shutil.copytree(DATA, target)
            original = read(target / "development.json")
            manifest = read(target / "manifest.json")
            for mutation in ("gold", "policy_body", "record", "scope", "boundary", "case_alias", "non_policy", "question", "template"):
                with self.subTest(mutation=mutation):
                    corpus = copy.deepcopy(original)
                    policy = next(s for s in corpus["scenarios"] if s["group"] == "policies")
                    if mutation == "gold":
                        policy["truth"]["side_effects"]["handoff"] = 1
                    elif mutation == "policy_body":
                        corpus["policies"][0]["content"] += " 修改正文。"
                    elif mutation == "record":
                        corpus["records"]["erp_db"]["orders"][0]["order_status"] = "错误状态"
                    elif mutation == "scope":
                        policy["policy_scope"]["audience"] = "customer"
                    elif mutation == "boundary":
                        policy["policy_boundary"] = "before"
                    elif mutation == "case_alias":
                        policy["steps"][0]["case_alias"] = "other"
                    elif mutation == "non_policy":
                        corpus["scenarios"][0]["steps"][0]["message"] += " 新要求。"
                    elif mutation == "question":
                        policy["steps"][0]["message"] = "请直接给出费用金额。"
                    else:
                        policy["template_family"] = "forged"
                    write(target / "development.json", corpus)
                    changed_manifest = copy.deepcopy(manifest)
                    changed_manifest["files"]["development.json"] = sha((target / "development.json").read_bytes())
                    write(target / "manifest.json", changed_manifest)
                    with self.assertRaisesRegex(ValueError, "allowlist|declared revision"):
                        validate_revision(target)

    def test_rehashed_images_inventory_and_manifest_changes_are_rejected(self):
        for mutation in ("image", "threshold", "exposure", "extra_file", "nested_manifest"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "corpus"
                shutil.copytree(DATA, target)
                manifest = read(target / "manifest.json")
                if mutation == "image":
                    name = "images/development-images-001.png"
                    with (target / name).open("ab") as stream:
                        stream.write(b"changed")
                    manifest["files"][name] = sha((target / name).read_bytes())
                elif mutation == "threshold":
                    manifest["thresholds"]["fact_scenarios_min"] = .9
                elif mutation == "exposure":
                    manifest["revision"]["measurement_history"]["entire_corpus_is_previously_unmeasured_or_blind"] = True
                else:
                    name = "extra.json" if mutation == "extra_file" else "images/manifest.json"
                    write(target / name, {})
                    manifest["files"][name] = sha((target / name).read_bytes())
                write(target / "manifest.json", manifest)
                with self.assertRaisesRegex(ValueError, "55 v2 images|manifest changed|file inventory"):
                    validate_revision(target)

    def test_revision_rejects_changed_source_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "v2", Path(folder) / "v3"
            shutil.copytree(SOURCE, source)
            for mutation in ("manifest", "policy"):
                with self.subTest(mutation=mutation):
                    manifest = read(SOURCE / "manifest.json")
                    if mutation == "manifest":
                        manifest["oracle"] = "changed"
                    else:
                        corpus = read(source / "development.json")
                        corpus["policies"][0]["content"] += " changed"
                        write(source / "development.json", corpus)
                    write(source / "manifest.json", manifest)
                    with self.assertRaisesRegex(ValueError, "pinned v2|Frozen dataset hash mismatch"):
                        freeze(target, source=source)
                    self.assertFalse(target.exists())

    def test_freeze_reproduces_every_byte_and_refuses_existing_or_source_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "v3"
            freeze(target)
            expected = file_hashes(DATA)
            self.assertEqual(file_hashes(target), expected)
            with self.assertRaisesRegex(ValueError, "destination already exists"):
                freeze(target)
            self.assertEqual(file_hashes(target), expected)
        for destination in (DATA, SOURCE, SOURCE / "new-v3", ROOT / "acceptance/data/v1/new-v3"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                freeze(destination)

    def test_failed_validation_does_not_publish_partial_corpus(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "v3"
            with patch("acceptance.revise_v3.validate_revision", side_effect=ValueError("rejected")):
                with self.assertRaisesRegex(ValueError, "rejected"):
                    freeze(target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_default_generator_freezes_v3_and_rejects_preserved_corpus_targets(self):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "v3"
            command = [sys.executable, "-m", "acceptance.generate", "--freeze", "--output", str(target)]
            result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(file_hashes(target), file_hashes(DATA))
            result = subprocess.run([sys.executable, "-m", "acceptance.revise_v3", "--proof", str(SOURCE / "forbidden-proof.json")],
                                    cwd=ROOT, env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Difference proof must be outside", result.stderr)
            self.assertFalse((SOURCE / "forbidden-proof.json").exists())


if __name__ == "__main__":
    unittest.main()
