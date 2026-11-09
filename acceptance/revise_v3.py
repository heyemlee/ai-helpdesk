"""Freeze v3 by clarifying policy questions in the immutable v2 corpus.

This revision is informed by v2 development evaluation, not a fresh blind
corpus. It does not inspect model recordings or regenerate business gold,
policy documents, or images.
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import shutil
import tempfile

from acceptance.common import DATA, ROOT, dataset, read, sha, write
from acceptance.generate import normalized, validate


VERSION = "acceptance-v3"
SOURCE = ROOT / "acceptance/data/v2"
SOURCE_MANIFEST_SHA256 = "88a8954e19b0c7e7870b41e42b16f47b61630a83553e9eb6446640ee61f92b9b"
SPLITS = ("development", "release")
QUESTION_COUNTS = {"development": 15, "release": 50}
REVISION = {
    "kind": "policy_question_clarification",
    "reason": "V2 development evaluation exposed ambiguity: policy questions named broad topics such as fees while the frozen documents specify processing times or procedure steps.",
    "measurement_history": {
        "v2_real_supplier_calls_before_revision": True,
        "v2_release_corpus_has_prior_model_exposure": True,
        "entire_corpus_is_previously_unmeasured_or_blind": False,
        "revision_informed_by": "v2 development policy-question ambiguity",
        "prior_formal_baseline_answer_contents_consulted": False,
    },
    "source_dataset_version": "acceptance-v2",
    "changed_policy_questions": {**QUESTION_COUNTS, "processing_time": 52, "procedure_steps": 13},
    "allowed_scenario_changes": ["steps[0].message", "prompt_templates", "template_family"],
    "preserved": ["truth_and_gold", "policy_documents", "records", "policy_scope_and_boundaries",
                  "scenario_ids_counts_and_groups", "non_policy_scenarios", "image_bytes_and_metadata",
                  "calibration_selection", "ambiguity_and_conflict_requirements", "thresholds"],
}


def source_dataset(directory=SOURCE):
    directory = Path(directory)
    if sha((directory / "manifest.json").read_bytes()) != SOURCE_MANIFEST_SHA256:
        raise ValueError("V3 requires the pinned v2 source manifest")
    result = dataset(directory)
    listed = set(result[0]["files"]) | {"manifest.json"}
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != listed:
        raise ValueError("Unexpected files in the frozen v2 source")
    return result


def question(scenario, corpus):
    product = scenario["policy_scope"]["product"]
    document = next(d for d in corpus["policies"] if d["lineage"] == scenario["policy_lineages"][0])
    topic = document["title"].removeprefix(product + " ").rsplit(" ", 1)[0]
    subject = "办理步骤" if scenario["subtype"] == "procedure" else "处理时限"
    if corpus["split"] == "development":
        return f"请查{product}的{topic}政策，说明所选日期和适用范围内的{subject}及有效版本，并提供引用。"
    region = scenario["policy_scope"]["region"]
    return f"我在整理 {region} 地区 {product} 客服答复。按所选日期和适用范围，{topic}的{subject}有什么规定？请说明有效版本并保留引用。"


def revise(corpus):
    revised = copy.deepcopy(corpus)
    revised["dataset_version"] = VERSION
    for scenario in revised["scenarios"]:
        if scenario["group"] != "policies":
            continue
        scenario["steps"][0]["message"] = question(scenario, corpus)
        scenario["prompt_templates"] = [normalized(step["message"]) for step in scenario["steps"] if "message" in step]
        scenario["template_family"] = sha(scenario["prompt_templates"])
    return revised


def revision_manifest(source, files):
    manifest = copy.deepcopy(source)
    del manifest["premeasurement_revision"]
    manifest.update(
        dataset_version=VERSION,
        prior_manifest_sha256=SOURCE_MANIFEST_SHA256,
        oracle="Business gold, policy documents and image pixels inherited unchanged from independently authored v2; only policy questions were clarified after v2 development evaluation.",
        revision=copy.deepcopy(REVISION),
        files=files,
    )
    return manifest


def differences(before, after, path=""):
    """Return every changed JSON leaf, including additions and removals."""
    if type(before) is not type(after):
        return [path]
    if isinstance(before, dict):
        changed = []
        for key in sorted(before.keys() | after.keys()):
            child = f"{path}.{key}" if path else key
            changed.extend([child] if key not in before or key not in after else differences(before[key], after[key], child))
        return changed
    if isinstance(before, list):
        if len(before) != len(after):
            return [path]
        return [p for index, (left, right) in enumerate(zip(before, after))
                for p in differences(left, right, f"{path}[{index}]")]
    return [] if before == after else [path]


def validate_revision(directory=DATA, *, source=SOURCE):
    directory, source = Path(directory), Path(source)
    original_manifest, *originals = source_dataset(source)
    manifest, *revisions = dataset(directory)
    actual_files = {p.relative_to(directory).as_posix(): sha(p.read_bytes())
                    for p in directory.rglob("*") if p.is_file() and p != directory / "manifest.json"}
    if set(actual_files) != set(original_manifest["files"]) or actual_files != manifest["files"]:
        raise ValueError("V3 file inventory differs from v2")
    if manifest != revision_manifest(original_manifest, actual_files):
        raise ValueError("V3 manifest changed outside the revision metadata allowlist")
    changed_questions, unchanged_scenarios, changed_paths = {}, {}, {}
    for split, original, revised in zip(SPLITS, originals, revisions):
        allowed = {"dataset_version"}
        changed_questions[split] = []
        for index, scenario in enumerate(original["scenarios"]):
            if scenario["group"] == "policies":
                allowed.update(f"scenarios[{index}].{field}" for field in
                               ("steps[0].message", "prompt_templates[0]", "template_family"))
                changed_questions[split].append(scenario["id"])
        changed = differences(original, revised)
        if set(changed) != allowed:
            raise ValueError(f"V3 {split} changes differ from the policy-question allowlist: {sorted(set(changed) ^ allowed)}")
        if revised != revise(original):
            raise ValueError(f"V3 {split} questions or derived metadata differ from the declared revision")
        if len(changed_questions[split]) != QUESTION_COUNTS[split]:
            raise ValueError("V3 policy-question counts changed")
        changed_paths[split] = changed
        unchanged_scenarios[split] = len(original["scenarios"]) - len(changed_questions[split])
    images = {name: digest for name, digest in actual_files.items() if name.startswith("images/")}
    if len(images) != 55 or any(original_manifest["files"][name] != digest for name, digest in images.items()):
        raise ValueError("V3 must preserve all 55 v2 images byte for byte")
    result = validate(directory)
    return {**result, "kind": "corpus_revision_diff", "dataset_version": VERSION,
            "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
            "changed_policy_questions": changed_questions, "changed_paths": changed_paths,
            "unchanged_non_policy_scenarios": unchanged_scenarios,
            "semantic_targets": {"processing_time": 52, "procedure_steps": 13},
            "unchanged_image_files": images,
            "truth_policy_documents_records_scopes_and_thresholds_unchanged": True}


def freeze(directory=DATA, *, source=SOURCE):
    directory, source = Path(directory).resolve(), Path(source).resolve()
    protected = (ROOT / "acceptance/data/v1", ROOT / "acceptance/data/v2", source)
    if any(directory.is_relative_to(path.resolve()) for path in protected):
        raise ValueError("Refusing to write inside a frozen source corpus")
    if directory.exists():
        raise ValueError("A frozen destination already exists; author a new version explicitly")
    source_manifest, *corpora = source_dataset(source)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".acceptance-v3-", dir=directory.parent) as folder:
        staging = Path(folder) / "corpus"
        staging.mkdir()
        for name in source_manifest["files"]:
            if name in {split + ".json" for split in SPLITS}:
                continue
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        for split, corpus in zip(SPLITS, corpora):
            write(staging / (split + ".json"), revise(corpus))
        files = {p.relative_to(staging).as_posix(): sha(p.read_bytes()) for p in sorted(staging.rglob("*")) if p.is_file()}
        write(staging / "manifest.json", revision_manifest(source_manifest, files))
        proof = validate_revision(staging, source=source)
        if directory.exists():
            raise ValueError("A frozen destination appeared during revision")
        staging.rename(directory)
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DATA)
    parser.add_argument("--freeze", action="store_true", help="Create a new directory from the pinned v2 corpus")
    parser.add_argument("--proof", type=Path, help="Write the complete offline difference proof outside the frozen corpus")
    args = parser.parse_args()
    if args.proof and any(args.proof.resolve().is_relative_to(path.resolve()) for path in
                          (args.output, ROOT / "acceptance/data")):
        parser.error("Difference proof must be outside the frozen corpus")
    proof = freeze(args.output) if args.freeze else validate_revision(args.output)
    if args.proof:
        write(args.proof, proof)
    print({key: proof[key] for key in ("valid", "dataset_version", "release", "development", "manifest_sha256")})
