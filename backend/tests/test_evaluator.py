"""评测框架：四维 LLM-as-Judge 打分 + 相对存档基线的回归检测。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.evaluation.evaluator import (
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


def make_evaluator(baseline_path, release="v2.0.0", **kwargs) -> EndToEndEvaluator:
    return EndToEndEvaluator(
        orchestrator=None,
        recognizer=None,
        api_key="test-key",
        baseline_path=str(baseline_path),
        release=release,
        **kwargs,
    )


def stub_intent_metrics(evaluator, accuracy: float):
    async def _evaluate(cases, abort=None):
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


# ── 模型名：换了模型，"退化"就不全是代码的事 ──────────────────────────────────

async def test_report_and_baseline_record_the_model(baseline_path):
    evaluator = make_evaluator(baseline_path, model="model-a")
    stub_intent_metrics(evaluator, 0.9)
    report = await evaluator.run(intent_cases=CASES)

    assert report.model == "model-a" and report.baseline_model == ""
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["model"] == "model-a"
    assert make_evaluator(baseline_path).baseline.model == "model-a"


async def test_a_baseline_from_another_model_is_called_out(baseline_path):
    """控制台只显示退化那几行和建议：跨模型比的时候，两处都要说清楚是哪两个模型。"""
    first = make_evaluator(baseline_path, model="model-a")
    stub_intent_metrics(first, 0.90)
    await first.run(intent_cases=CASES)

    later = make_evaluator(baseline_path, release="v2.1.0", model="model-b")
    stub_intent_metrics(later, 0.70)
    report = await later.run(intent_cases=CASES)

    assert (report.model, report.baseline_model) == ("model-b", "model-a")
    assert report.regressions and all("基线是 model-a 跑的，本次是 model-b" in r for r in report.regressions)
    assert report.recommendations[0].startswith("基线是 model-a 跑的，本次是 model-b")
    assert "/eval/promote" in report.recommendations[0]


async def test_a_baseline_saved_before_models_were_recorded_says_so(baseline_path):
    """9/1 那份基线没有 model 字段：说不清是不是同一个模型，照样要提醒。"""
    baseline_path.write_text(json.dumps({
        "timestamp": "2026-09-01T23:28:31", "total": 1, "passed": 1, "pass_rate": 1.0,
        "avg_scores": {"intent_accuracy": 0.9}, "regressions": [], "recommendations": [],
        "release": "cabinet-domain", "results": [],
    }), encoding="utf-8")
    evaluator = make_evaluator(baseline_path, model="gpt-6-luna")
    stub_intent_metrics(evaluator, 0.7)

    report = await evaluator.run(intent_cases=CASES)

    assert report.baseline_model == ""
    assert "基线没有记录是哪个模型跑的，本次是 gpt-6-luna" in report.regressions[0]
    assert report.recommendations[0].startswith("基线没有记录是哪个模型跑的")


async def test_same_model_keeps_the_regression_lines_as_they_were(baseline_path):
    first = make_evaluator(baseline_path, model="model-a")
    stub_intent_metrics(first, 0.90)
    await first.run(intent_cases=CASES)

    later = make_evaluator(baseline_path, model="model-a")
    stub_intent_metrics(later, 0.70)
    report = await later.run(intent_cases=CASES)

    assert report.regressions[0].endswith("退化 22.2%)")
    assert not any("基线" in r and "跑的" in r for r in report.recommendations)


async def test_switching_the_model_in_the_console_changes_the_recorded_model(baseline_path):
    evaluator = make_evaluator(baseline_path, model="model-a")
    evaluator.reconfigure_llm(SimpleNamespace(), "model-c")
    stub_intent_metrics(evaluator, 0.9)

    report = await evaluator.run(intent_cases=CASES)

    assert report.model == "model-c"


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

    await evaluator.run(dialog_cases=[{"question": f"q{i}"} for i in range(5)])

    # 第一个用例单独跑完才放开并发（模型一上来就不可用时只撞一次，见 RunAbort），
    # 其余四个同时在跑
    assert orch.trace[:2] == [("enter", "eval_0"), ("exit", "eval_0")]
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

    # 打头的那个用例是单独跑的，并发的是后两个：慢的先发起，快的先跑完
    report = await evaluator.run(
        dialog_cases=[{"question": "打头"}, {"question": "慢"}, {"question": "快"}])

    assert orch.trace[-2:] == [("exit", "eval_2"), ("exit", "eval_1")]
    assert [r.test_id for r in report.results] == ["dialog_0", "dialog_1", "dialog_2"]
    assert [r.metadata["question"] for r in report.results] == ["打头", "慢", "快"]


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

    assert recognizer.peak == 3, "意图用例没有并发起来"    # 第一条单独跑，其余按并发度
    # 保序：predicted 是回显的用例内容，错位会直接把准确率打下来
    assert metrics["accuracy"] == 1.0
    assert [c["message"] for c in metrics["cases"]] == [c.message for c in cases]


# ── 转人工率 ──────────────────────────────────────────────────────────────────
#
# 判分维度看不出转人工变多了：一条守着边界、把客户转给人的回答，judge 照样给及格。
# 所以转人工率单独记、单独比，方向和分数相反——升了才算退化。

class _HandoffOrchestrator:
    """消息以"人"开头的转人工，以"断"开头的模拟模型服务不可用，其余正常作答。"""

    async def run(self, req):
        result = _orch_result()
        result.handoff = req.message.startswith("人")
        result.service_unavailable = req.message.startswith("断")
        return result


def handoff_evaluator(baseline_path, model="model-a") -> EndToEndEvaluator:
    evaluator = dialog_evaluator(baseline_path, _HandoffOrchestrator(), concurrency=4)
    evaluator._model = model
    return evaluator


def dialog(handoffs: int, answered: int, unavailable: int = 0) -> list[dict]:
    return ([{"question": f"人{i}"} for i in range(handoffs)]
            + [{"question": f"答{i}"} for i in range(answered)]
            + [{"question": f"断{i}"} for i in range(unavailable)])


async def test_report_records_the_handoff_rate_outside_the_judge_averages(baseline_path):
    evaluator = handoff_evaluator(baseline_path)

    report = await evaluator.run(dialog_cases=[{"turns": ["人", "答"]}, *dialog(1, 1, unavailable=1)])

    # 服务不可用那一轮没人判断过要不要人，不进分母
    assert (report.handoffs, report.handoff_turns, report.handoff_rate) == (2, 4, 0.5)
    assert "handoff_rate" not in report.avg_scores
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert (stored["handoffs"], stored["handoff_turns"], stored["handoff_rate"]) == (2, 4, 0.5)


async def test_no_dialog_turns_means_no_handoff_rate(baseline_path):
    evaluator = make_evaluator(baseline_path)
    stub_intent_metrics(evaluator, 0.9)

    report = await evaluator.run(intent_cases=CASES)

    assert report.handoff_rate is None and report.handoff_turns == 0


async def test_handoff_rate_rise_beyond_tolerance_is_a_regression(baseline_path):
    await handoff_evaluator(baseline_path).run(dialog_cases=dialog(12, 4))     # 75%

    report = await handoff_evaluator(baseline_path).run(dialog_cases=dialog(14, 2))  # 87.5%

    assert report.regressions == [
        "handoff_rate: 75.0% (12/16) → 87.5% (14/16) (相对基线 unknown 上升 12.5 个百分点)"
    ]
    assert report.avg_scores == {k: 0.9 for k in JUDGE_DIMENSIONS}, "判分没变，退化只来自转人工率"


@pytest.mark.parametrize("handoffs", [13, 8], ids=["one-more-turn", "fewer-handoffs"])
async def test_handoff_rate_within_tolerance_or_falling_is_not_a_regression(baseline_path, handoffs):
    await handoff_evaluator(baseline_path).run(dialog_cases=dialog(12, 4))     # 75%

    report = await handoff_evaluator(baseline_path).run(dialog_cases=dialog(handoffs, 16 - handoffs))

    assert report.regressions == []


async def test_handoff_tolerance_is_absolute_so_a_zero_baseline_still_compares(baseline_path):
    await handoff_evaluator(baseline_path).run(dialog_cases=dialog(0, 10))

    exactly = await handoff_evaluator(baseline_path).run(dialog_cases=dialog(1, 9))    # 正好 10 个百分点
    over = await handoff_evaluator(baseline_path).run(dialog_cases=dialog(2, 8))

    assert exactly.regressions == []
    assert len(over.regressions) == 1 and over.regressions[0].startswith("handoff_rate: 0.0% (0/10)")


async def test_handoff_regression_names_both_models_across_models(baseline_path):
    await handoff_evaluator(baseline_path, model="model-a").run(dialog_cases=dialog(2, 8))

    report = await handoff_evaluator(baseline_path, model="model-b").run(dialog_cases=dialog(8, 2))

    assert report.regressions[0].endswith("上升 60.0 个百分点；基线是 model-a 跑的，本次是 model-b)")
    assert report.recommendations[0].startswith("基线是 model-a 跑的，本次是 model-b")


def _legacy_baseline(results: list[dict]) -> dict:
    """加转人工率字段之前存的基线。"""
    return {
        "timestamp": "2026-09-01T00:00:00", "total": len(results), "passed": len(results),
        "pass_rate": 1.0, "avg_scores": {k: 0.9 for k in JUDGE_DIMENSIONS},
        "regressions": [], "recommendations": [], "results": results, "release": "old",
    }


async def test_a_baseline_without_handoff_data_compares_without_a_spurious_regression(baseline_path):
    # 更早的格式：每轮的 metadata 里也没有 handoff，补算不出来
    baseline_path.write_text(json.dumps(_legacy_baseline([
        {"test_id": f"dialog_{i}", "passed": True, "scores": {}, "metadata": {"question": "q"}}
        for i in range(4)
    ])), encoding="utf-8")
    evaluator = handoff_evaluator(baseline_path)
    assert evaluator.baseline.handoff_rate is None

    report = await evaluator.run(dialog_cases=dialog(4, 0))                    # 100% 转人工

    assert report.regressions == []
    assert report.handoff_rate == 1.0


async def test_a_baseline_saved_before_the_field_gets_its_rate_from_its_results(baseline_path):
    # 2026-11-06 那份基线就是这样：没有 handoff_rate 字段，但每轮 metadata 里记着 handoff
    baseline_path.write_text(json.dumps(_legacy_baseline([
        {"test_id": "intent_recognition", "passed": True, "scores": {}, "metadata": {"total": 90}},
        *[{"test_id": f"dialog_{i}", "passed": True, "scores": {},
           "metadata": {"handoff": i < 3, "service_unavailable": False}} for i in range(4)],
    ])), encoding="utf-8")
    evaluator = handoff_evaluator(baseline_path)

    assert (evaluator.baseline.handoffs, evaluator.baseline.handoff_turns) == (3, 4)
    report = await evaluator.run(dialog_cases=dialog(3, 1))
    assert report.regressions == []
