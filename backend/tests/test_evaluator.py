"""评测框架：四维 LLM-as-Judge 打分 + 相对存档基线的回归检测。"""
import json

import pytest

from evaluation.evaluator import (
    EndToEndEvaluator,
    IntentTestCase,
    LLMJudge,
    QualityScores,
)

JUDGE_DIMENSIONS = ("relevance", "accuracy", "completeness", "helpfulness")


@pytest.fixture
def baseline_path(tmp_path):
    return tmp_path / "baseline.json"


def make_evaluator(baseline_path, release="v2.0.0") -> EndToEndEvaluator:
    return EndToEndEvaluator(
        orchestrator=None,
        recognizer=None,
        api_key="test-key",
        baseline_path=str(baseline_path),
        release=release,
    )


def stub_intent_metrics(evaluator, accuracy: float):
    async def _evaluate(cases):
        return {
            "accuracy": accuracy,
            "macro_f1": accuracy,
            "per_class": {},
            "total": len(cases),
            "correct": int(accuracy * len(cases)),
            "cases": [],
        }

    evaluator._intent_evaluator.evaluate = _evaluate


CASES = [IntentTestCase("补件什么时候到", "missing_part")]


# ── 四个质量维度 ──────────────────────────────────────────────────────────────

def test_judge_scores_exactly_four_dimensions():
    scores = QualityScores(relevance=0.9, accuracy=0.8, completeness=0.7, helpfulness=0.6)

    assert set(vars(scores)) >= set(JUDGE_DIMENSIONS)
    assert scores.overall == pytest.approx(0.75)


def test_judge_prompt_names_all_four_dimensions():
    for dimension in JUDGE_DIMENSIONS:
        assert dimension in LLMJudge.JUDGE_PROMPT


async def test_judge_failure_is_recorded_not_silently_averaged():
    class Boom:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("judge down")

    scores = await LLMJudge(Boom(), "m").judge("q", "a")

    assert scores.judge_failed is True
    assert scores.error


# ── 回归检测 ──────────────────────────────────────────────────────────────────

async def test_first_run_creates_the_baseline(baseline_path):
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.90)

    report = await evaluator.run(intent_cases=CASES)

    assert report.promoted is True
    assert baseline_path.exists()
    assert report.regressions == []


async def test_regression_detected_against_stored_baseline(baseline_path):
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.90)
    await evaluator.run(intent_cases=CASES)

    later = make_evaluator(baseline_path, release="v2.1.0")
    stub_intent_metrics(later, 0.70)
    report = await later.run(intent_cases=CASES)

    assert report.regressions, "相对基线跌了 22%，必须报回归"
    assert "intent_accuracy" in report.regressions[0]
    assert report.baseline_release == "v2.0.0"


async def test_baseline_is_not_overwritten_on_every_run(baseline_path):
    """
    评测跑一次就覆盖基线的话，回归检测只能发现"比上一次差"——
    连续小幅劣化每次都在容差内，累计跌幅却已经很大。
    """
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.90)
    await evaluator.run(intent_cases=CASES)

    # 连续三次各跌一点，每次相对上一次都不到 5%
    for accuracy in (0.87, 0.84, 0.81):
        stub_intent_metrics(evaluator, accuracy)
        report = await evaluator.run(intent_cases=CASES)

    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert stored["avg_scores"]["intent_accuracy"] == 0.90, "基线被意外覆盖了"
    assert report.regressions, "累计跌幅 10%，必须相对原始基线报出来"
    assert report.promoted is False


async def test_within_tolerance_is_not_a_regression(baseline_path):
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.90)
    await evaluator.run(intent_cases=CASES)

    stub_intent_metrics(evaluator, 0.88)  # 跌 2.2%，在 5% 容差内
    report = await evaluator.run(intent_cases=CASES)

    assert report.regressions == []


async def test_promote_updates_the_baseline(baseline_path):
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.80)
    await evaluator.run(intent_cases=CASES)

    stub_intent_metrics(evaluator, 0.95)
    await evaluator.run(intent_cases=CASES, promote=True)

    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert stored["avg_scores"]["intent_accuracy"] == 0.95


async def test_promote_baseline_endpoint_path(baseline_path):
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.80)
    await evaluator.run(intent_cases=CASES)

    stub_intent_metrics(evaluator, 0.92)
    await evaluator.run(intent_cases=CASES)
    promoted = evaluator.promote_baseline()

    assert promoted is not None
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert stored["avg_scores"]["intent_accuracy"] == 0.92


async def test_report_records_release_under_test(baseline_path):
    evaluator = make_evaluator(baseline_path, release="2026.08.17-abc1234")
    stub_intent_metrics(evaluator, 0.9)

    report = await evaluator.run(intent_cases=CASES)

    assert report.release == "2026.08.17-abc1234"
