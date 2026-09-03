"""评测框架：四维 LLM-as-Judge 打分 + 相对存档基线的回归检测。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from evaluation.evaluator import (
    EndToEndEvaluator,
    IntentEvaluator,
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


def test_judge_prompt_states_the_business_boundaries():
    """
    判分标准必须和 SOP 对齐，否则评测会惩罚正确的行为。

    "加个岛台多少钱" 这类问题，SOP 明令不许给口头报价，Agent 照做了，
    judge 却按"没解决用户需求"扣分——错的是判分标准，不是 Agent。
    """
    prompt = LLMJudge.JUDGE_PROMPT
    for boundary in ("报价单", "交期", "退款", "系统记录"):
        assert boundary in prompt, f"judge 不知道这条业务边界: {boundary}"
    assert "拒答" in prompt, "judge 不知道正确拒答也算完整回答"
    # 反向约束：不能变成"只要拒答就给高分"
    assert "编造" in prompt
    assert "却推给人工" in prompt


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


# ── 并发 ──────────────────────────────────────────────────────────────────────
#
# 一次评测的时间几乎全花在等 LLM。串行跑的话，90 个意图用例加十几轮对话
# 就是三分多钟——真实测出来的 /eval/run 平均耗时是 182 秒。用例之间本来就
# 互不相干，并发是纯粹的等待重叠，唯一要守住的边界是：同一个对话用例的
# 多轮必须保持串行，后一轮要读前一轮的历史。

class _Value:
    """替身枚举：只需要 .value。"""
    def __init__(self, value: str):
        self.value = value


def _orch_result():
    return SimpleNamespace(
        response="好的，我来帮您处理。",
        agent_type=_Value("general"), primary_agent=None, supporting_agents=[],
        intent=None, handoff=False, answer_confidence=0.9, policy_violations=[],
    )


class _TracingOrchestrator:
    """记录每次 run 的进出场，用来还原真实的并发轨迹。"""

    def __init__(self, delays: dict[str, float] | None = None, delay: float = 0.02):
        self.trace: list[tuple[str, str]] = []
        self._delays = delays or {}
        self._delay = delay

    async def run(self, req):
        self.trace.append(("enter", req.conv_id))
        await asyncio.sleep(self._delays.get(req.message, self._delay))
        self.trace.append(("exit", req.conv_id))
        return _orch_result()

    def max_in_flight(self) -> int:
        live = peak = 0
        for kind, _ in self.trace:
            live += 1 if kind == "enter" else -1
            peak = max(peak, live)
        return peak


def dialog_evaluator(baseline_path, orchestrator, concurrency: int) -> EndToEndEvaluator:
    evaluator = EndToEndEvaluator(
        orchestrator=orchestrator, recognizer=None, api_key="test-key",
        baseline_path=str(baseline_path), concurrency=concurrency,
    )

    async def _judge(question, response, context=None):
        return QualityScores(0.9, 0.9, 0.9, 0.9)

    evaluator._judge.judge = _judge
    return evaluator


async def test_dialog_cases_run_concurrently(baseline_path):
    orch = _TracingOrchestrator()
    evaluator = dialog_evaluator(baseline_path, orch, concurrency=4)

    await evaluator.run(dialog_cases=[{"question": f"q{i}"} for i in range(4)])

    assert orch.max_in_flight() == 4, f"用例没有并发起来: {orch.trace}"


async def test_concurrency_is_capped(baseline_path):
    """并发度是给上游限流留的余量，不能因为用例多就无限放大。"""
    orch = _TracingOrchestrator()
    evaluator = dialog_evaluator(baseline_path, orch, concurrency=2)

    await evaluator.run(dialog_cases=[{"question": f"q{i}"} for i in range(6)])

    assert orch.max_in_flight() == 2


async def test_turns_within_one_case_stay_sequential(baseline_path):
    """
    多轮用例的第二轮要带着第一轮的历史进去。把轮次也并发掉，
    测的就不再是多轮对话，而是三个互相不认识的单轮。
    """
    orch = _TracingOrchestrator()
    evaluator = dialog_evaluator(baseline_path, orch, concurrency=8)

    await evaluator.run(dialog_cases=[{"turns": ["第一轮", "第二轮", "第三轮"]}])

    assert orch.trace == [
        ("enter", "eval_0"), ("exit", "eval_0"),
        ("enter", "eval_0"), ("exit", "eval_0"),
        ("enter", "eval_0"), ("exit", "eval_0"),
    ], f"同一个用例的多轮重叠了: {orch.trace}"


async def test_results_keep_case_order_regardless_of_finish_order(baseline_path):
    """
    报告要和基线逐条对上，用例顺序不能随完成先后漂移——
    慢的用例先发起，快的用例先跑完，结果排列仍然按用例下标。
    """
    orch = _TracingOrchestrator(delays={"慢": 0.05, "快": 0.0})
    evaluator = dialog_evaluator(baseline_path, orch, concurrency=4)

    report = await evaluator.run(dialog_cases=[{"question": "慢"}, {"question": "快"}])

    assert [r.test_id for r in report.results] == ["dialog_0", "dialog_1"]
    assert report.results[0].metadata["question"] == "慢"


# ── 意图用例的并发 ────────────────────────────────────────────────────────────

class _TracingRecognizer:
    def __init__(self):
        self.trace: list[str] = []
        self.live = 0
        self.peak = 0

    async def recognize(self, message: str, history=None):
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.01)
        self.live -= 1
        # 预测值直接回显用例内容，便于验证保序
        return SimpleNamespace(
            intent=_Value(message), confidence=1.0, reasoning="stub",
        )


async def test_intent_cases_run_concurrently_and_keep_order():
    recognizer = _TracingRecognizer()
    cases = [IntentTestCase(f"intent_{i}", f"intent_{i}") for i in range(6)]

    metrics = await IntentEvaluator(recognizer, concurrency=3).evaluate(cases)

    assert recognizer.peak == 3, "意图用例没有并发起来"
    # 保序：predicted 是回显的用例内容，错位会直接把准确率打下来
    assert metrics["accuracy"] == 1.0
    assert [c["message"] for c in metrics["cases"]] == [c.message for c in cases]
