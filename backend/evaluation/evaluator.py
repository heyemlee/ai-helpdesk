"""
亮点：端到端 Agent 评测框架

核心问题：如何评测端到端 Agent？

评测维度：
  1. 意图识别准确率 —— 预测意图 vs 标注意图，计算 Accuracy / F1
  2. 响应质量评分 —— 用 LLM 作为评判者（LLM-as-Judge），
     从相关性、准确性、完整性、有用性四个维度打分
  3. 端到端对话评测 —— 模拟完整多轮对话，评估整体体验
  4. 回归测试 —— 与历史基线对比，防止性能退化

LLM-as-Judge 是评测 Agent 质量的关键技术：
  人工标注成本高、主观性强；用 LLM 评判可以规模化、可重复。
"""
import asyncio
import json
import logging
import os
import pathlib
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core.intent_recognizer import IntentRecognizer
from core.llm_utils import (
    STRUCTURED_MAX_TOKENS,
    LLMClient,
    create_llm_client,
    extract_structured_text,
    structured_create,
)

logger = logging.getLogger(__name__)


def _default_concurrency() -> int:
    """
    评测的并发用例数，EVAL_CONCURRENCY 可调。

    默认 4 是保守值：评测打的是和线上同一个 LLM 供应商，跑评测不该把额度
    和限流挤给真实用户。上游余量足够时调高能接近线性提速——一次评测的时间
    几乎全花在等 LLM，本机 CPU 不是瓶颈。
    """
    try:
        return max(1, int(os.getenv("EVAL_CONCURRENCY", "4")))
    except ValueError:
        logger.warning("EVAL_CONCURRENCY 不是整数，回退到 4")
        return 4


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class IntentTestCase:
    message:          str
    expected_intent:  str
    context:          dict[str, Any] | None = None


@dataclass
class QualityScores:
    """LLM-as-Judge 评分结果。"""
    relevance:    float   # 相关性：回答是否针对问题
    accuracy:     float   # 准确性：信息是否正确
    completeness: float   # 完整性：是否完整解决问题
    helpfulness:  float   # 有用性：用户是否能据此行动
    judge_failed: bool = False
    error: str | None = None

    @property
    def overall(self) -> float:
        return statistics.mean([self.relevance, self.accuracy, self.completeness, self.helpfulness])


@dataclass
class EvalResult:
    test_id:    str
    passed:     bool
    scores:     dict[str, float]
    detail:     str = ""
    metadata:   dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    """评测报告。"""
    timestamp:        str
    total:            int
    passed:           int
    pass_rate:        float
    avg_scores:       dict[str, float]
    regressions:      list[str]          # 相比基线退化的指标
    recommendations:  list[str]
    results:          list[EvalResult]
    release:          str = "unknown"    # 被评测的版本号，用于定位是哪次发布引入退化
    baseline_release: str = ""           # 对比基线的版本号
    baseline_timestamp: str = ""
    promoted:         bool = False       # 本次报告是否被提升为新基线


# ── LLM-as-Judge ─────────────────────────────────────────────────────────────

class LLMJudge:
    """
    用 LLM 评判 Agent 响应质量。

    为什么用 LLM 而不是人工？
    - 可规模化：数千条测试用例自动评测
    - 可重复：相同输入得到稳定评分
    - 多维度：同时评估相关性、准确性等多个维度

    注意：LLM Judge 本身也有偏差，建议定期用人工标注校准。
    """

    # 判分标准必须和业务规则对齐，否则评测会惩罚正确的行为。
    # 真实案例："这套厨房加个岛台大概要加多少钱？"——SOP 明令没有正式报价单
    # 就不许给金额（AnswerPolicy 会硬拦，BillingAgent 的 system prompt 也写了），
    # Agent 照做了，judge 却按"没解决用户需求"打 0.65。这不是 Agent 的问题，
    # 是判分标准不知道有这条边界。下面这段把边界告诉 judge，同时留好反向约束，
    # 免得它变成"只要拒答就给高分"的免死金牌。
    JUDGE_PROMPT = """你是橱柜工厂与销售展厅的客服质量评估专家。请对以下客服响应进行评分。

用户问题: {question}
Agent 响应: {response}
{context_section}

评分前先读这条业务边界，它决定了"什么才算完整回答"：

这个客服**不允许**做下面这些事，做了就是真实的客诉和赔付风险：
1. 没有正式报价单时给出任何金额或价格区间；
2. 承诺具体的完工/送货/到货日期（交期以工厂排产实际情况为准）；
3. 确认退款金额或退款到账时间（实际退补款必须由人工财务审核）；
4. 没查到系统记录时描述订单状态。

因此：**在边界内正确拒答、并说清楚下一步怎么走**（走正式报价流程、
需要订单号才能查、转人工财务审核），本身就是一个完整且有帮助的回答，
completeness 和 helpfulness 都应给高分——不要因为"没有直接给出用户想要的
数字"而扣分。同理，已经转交人工的回答，不该因为"没有当场解决"扣分。

但以下情况仍然要打低分：
- 编造金额、日期或订单状态（accuracy 给 0）；
- 既不回答、也不说明为什么不能答或下一步怎么走（completeness 低）；
- 本来就能直接答的问题（工艺做法、材质、保修范围、流程说明）却推给人工
  （relevance 和 helpfulness 低）。

请从以下四个维度评分（0.0-1.0），返回 JSON：
- relevance: 响应是否直接针对用户问题（0=完全无关，1=完全相关）
- accuracy: 信息是否准确，且没有越过上面的业务边界（0=编造或越界，1=准确合规）
- completeness: 在业务边界内该说的是否都说清楚了，包含下一步怎么走
- helpfulness: 用户看完能不能据此采取下一步行动

只返回 JSON，例如: {{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}}"""

    def __init__(self, client: LLMClient, model: str):
        self._client = client
        self._model  = model

    async def judge(
        self,
        question: str,
        response: str,
        context: str | None = None,
    ) -> QualityScores:
        ctx_section = f"背景信息: {context}" if context else ""
        prompt = self.JUDGE_PROMPT.format(
            question=question,
            response=response,
            context_section=ctx_section,
        )
        prompt = self._clean_text(prompt)
        try:
            resp = await structured_create(
                self._client,
                model=self._model, max_tokens=STRUCTURED_MAX_TOKENS, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_structured_text(resp)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            return QualityScores(
                relevance=float(data.get("relevance", 0.5)),
                accuracy=float(data.get("accuracy", 0.5)),
                completeness=float(data.get("completeness", 0.5)),
                helpfulness=float(data.get("helpfulness", 0.5)),
            )
        except Exception as ex:
            logger.warning(f"LLM Judge 失败: {ex}")
            return QualityScores(
                0.5, 0.5, 0.5, 0.5,
                judge_failed=True,
                error=str(ex),
            )

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 LLM 请求编码失败。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")


# ── 意图识别评测 ──────────────────────────────────────────────────────────────

class IntentEvaluator:
    """评测意图识别的准确率和 F1。"""

    def __init__(self, recognizer: IntentRecognizer, concurrency: int | None = None):
        self._recognizer = recognizer
        self._concurrency = max(1, concurrency or _default_concurrency())

    async def evaluate(self, cases: list[IntentTestCase]) -> dict[str, Any]:
        # 用例之间互不相干，串行跑纯粹是在排队等网络。gather 保序，
        # 下面按下标 zip 出 predictions/ground_truth 的口径不受影响。
        sem = asyncio.Semaphore(self._concurrency)

        async def recognize(case: IntentTestCase):
            async with sem:
                return await self._recognizer.recognize(case.message)

        results = await asyncio.gather(*[recognize(c) for c in cases])

        predictions, ground_truth = [], []
        case_details: list[dict[str, Any]] = []

        for case, result in zip(cases, results):
            predicted = result.intent.value
            predictions.append(predicted)
            ground_truth.append(case.expected_intent)
            case_details.append({
                "message": case.message,
                "expected": case.expected_intent,
                "predicted": predicted,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
            })

        # 纯 Python 计算指标
        correct = sum(p == g for p, g in zip(predictions, ground_truth))
        accuracy = correct / len(predictions) if predictions else 0.0

        # 每类 F1
        labels = sorted(set(ground_truth + predictions))
        per_class: dict[str, dict[str, float]] = {}
        for label in labels:
            tp = sum(p == label and g == label for p, g in zip(predictions, ground_truth))
            fp = sum(p == label and g != label for p, g in zip(predictions, ground_truth))
            fn = sum(p != label and g == label for p, g in zip(predictions, ground_truth))
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec  = tp / (tp + fn) if (tp + fn) else 0.0
            f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            per_class[label] = {"precision": prec, "recall": rec, "f1": f1}

        macro_f1 = statistics.mean(v["f1"] for v in per_class.values()) if per_class else 0.0

        return {
            "accuracy":   round(accuracy, 4),
            "macro_f1":   round(macro_f1, 4),
            "per_class":  per_class,
            "total":      len(cases),
            "correct":    correct,
            "cases":      case_details,
        }


# ── 端到端评测器 ──────────────────────────────────────────────────────────────

class EndToEndEvaluator:
    """
    端到端 Agent 评测。

    评测流程：
      1. 运行意图识别评测（准确率/F1）
      2. 运行对话质量评测（LLM-as-Judge）
      3. 与历史基线对比（回归检测）
      4. 生成可操作的优化建议
    """

    # 质量及格线
    PASS_THRESHOLD = 0.75
    # 相对基线跌幅超过该比例判定为回归
    REGRESSION_TOLERANCE = 0.05

    def __init__(
        self,
        orchestrator,
        recognizer: IntentRecognizer,
        api_key:  str,
        base_url: str | None = None,
        model:    str = "claude-3-5-sonnet-20241022",
        baseline_path: str | None = None,
        release:  str = "unknown",
        grounding_provider: Callable[[str, Any], Awaitable[str]] | None = None,
        concurrency: int | None = None,
    ):
        """
        grounding_provider: `async (message, intent_result) -> knowledge_text`。

        传入时，评测会在调编排器之前先做一次意图识别、再按意图检索知识库，
        把检索结果作为出处挂到 Request 上——和 /chat 主链路完全一致。

        不传时评测跳过检索，Agent 手里没有任何出处，答案策略会把所有带
        价格/时效的具体表述判为"无依据"并改写成保守话术。那样测出来的
        completeness 天然偏低，而且和线上不是一回事——所以这个参数该传就传。

        concurrency: 同时在跑的用例数，默认取 EVAL_CONCURRENCY（4）。
        """
        client = create_llm_client(api_key, base_url)

        self._orchestrator     = orchestrator
        self._judge            = LLMJudge(client, model)
        self._recognizer       = recognizer
        self._grounding        = grounding_provider
        self._concurrency      = max(1, concurrency or _default_concurrency())
        self._intent_evaluator = IntentEvaluator(recognizer, self._concurrency)
        self._history:         list[EvalReport] = []
        self._release = release
        self._baseline_path = pathlib.Path(baseline_path) if baseline_path else None
        self._baseline: EvalReport | None = self._load_baseline()

    async def run(
        self,
        intent_cases:    list[IntentTestCase] | None = None,
        dialog_cases:    list[dict[str, Any]] | None = None,
        promote:         bool = False,
    ) -> EvalReport:
        """
        运行完整评测。

        intent_cases: 意图识别测试用例
        dialog_cases:
          - 单轮: [{"question": "..."}]
          - 多轮: [{"turns": ["第一轮", "第二轮", ...]}]
        promote:
          是否把本次结果写为新基线。默认 False——**评测跑一次就覆盖基线的话，
          回归检测永远只能发现"比上一次差"，发布连续小幅劣化会完全检测不到**。
          基线只在人工确认这批分数可以接受时才提升（/eval/promote 或 promote=True）。
          仓库里没有基线文件时会自动落盘第一份，作为后续对比的起点。
        """
        results: list[EvalResult] = []
        all_scores: dict[str, list[float]] = {
            "relevance": [], "accuracy": [], "completeness": [], "helpfulness": []
        }

        # 1. 意图识别评测
        intent_metrics: dict[str, Any] = {}
        if intent_cases:
            intent_metrics = await self._intent_evaluator.evaluate(intent_cases)
            passed = intent_metrics["accuracy"] >= self.PASS_THRESHOLD
            results.append(EvalResult(
                test_id="intent_recognition",
                passed=passed,
                scores={"accuracy": intent_metrics["accuracy"], "macro_f1": intent_metrics["macro_f1"]},
                detail=f"准确率 {intent_metrics['accuracy']:.1%}，Macro-F1 {intent_metrics['macro_f1']:.3f}",
                metadata={
                    "total": intent_metrics.get("total", 0),
                    "correct": intent_metrics.get("correct", 0),
                    "cases": intent_metrics.get("cases", []),
                },
            ))

        # 2. 对话质量评测（调用 orchestrator 产出回复，再用 LLM Judge 评分）
        if dialog_cases:
            # 用例之间各有各的 conv_id，互不影响，可以并发；**一个用例内部的
            # 多轮必须留在同一个协程里串行**——后一轮要读前一轮的历史。
            # 并发的是"几个用户同时说话"，这本来就是线上的常态。
            sem = asyncio.Semaphore(self._concurrency)

            async def run_case(idx: int, case: dict[str, Any]) -> list[EvalResult]:
                async with sem:
                    return await self._evaluate_dialog_case(case, idx)

            # gather 保序：结果排列和串行时一致，报告和基线才能逐条对上
            for case_results in await asyncio.gather(
                *[run_case(i, c) for i, c in enumerate(dialog_cases)]
            ):
                results.extend(case_results)
                for r in case_results:
                    for k in all_scores:
                        if k in r.scores:
                            all_scores[k].append(r.scores[k])

        # 3. 汇总
        avg_scores = {
            k: round(statistics.mean(v), 4) for k, v in all_scores.items() if v
        }
        if intent_metrics:
            avg_scores["intent_accuracy"] = intent_metrics["accuracy"]

        passed_count = sum(1 for r in results if r.passed)
        pass_rate    = passed_count / len(results) if results else 0.0

        # 4. 回归检测：始终对比磁盘上的基线，而不是本进程上一次的结果
        regressions = self._detect_regressions(avg_scores)

        # 5. 优化建议
        recommendations = self._recommendations(avg_scores, intent_metrics)

        report = EvalReport(
            timestamp=datetime.now().isoformat(),
            total=len(results),
            passed=passed_count,
            pass_rate=round(pass_rate, 4),
            avg_scores=avg_scores,
            regressions=regressions,
            recommendations=recommendations,
            results=results,
            release=self._release,
            baseline_release=self._baseline.release if self._baseline else "",
            baseline_timestamp=self._baseline.timestamp if self._baseline else "",
        )
        self._history.append(report)

        # 只有显式提升，或者仓库里还没有基线时，才写基线文件
        if promote or self._baseline is None:
            report.promoted = True
            self._save_baseline(report)
        return report

    def promote_baseline(self, report: EvalReport | None = None) -> EvalReport | None:
        """把指定报告（默认最近一次）提升为新基线。"""
        target = report or (self._history[-1] if self._history else None)
        if target is None:
            return None
        target.promoted = True
        self._save_baseline(target)
        return target

    @property
    def baseline(self) -> EvalReport | None:
        return self._baseline

    @property
    def concurrency(self) -> int:
        return self._concurrency

    async def _evaluate_dialog_case(self, case: dict[str, Any], case_idx: int) -> list[EvalResult]:
        """评测单轮或多轮对话用例。"""
        from agents.agent_orchestrator import Request as OrcReq

        questions = self._dialog_turns(case)
        if not questions:
            return []

        conv_id = str(case.get("conv_id") or f"eval_{case_idx}")
        user_id = str(case.get("user_id") or "eval_user")
        history: list[dict[str, str]] = []
        results: list[EvalResult] = []

        for turn_idx, question in enumerate(questions):
            context = self._history_context(history)
            turn_history = history[-6:] if history else None
            orch_req = OrcReq(
                message=question,
                user_id=user_id,
                conv_id=conv_id,
                context=context,
                history=turn_history,
            )

            # 配了 grounding_provider 就复刻 /chat 的前置步骤：先识别意图，
            # 再按意图检索知识库，把检索结果挂成出处。没有这一步，答案策略
            # 会把每一处价格/时效都判为无依据。
            if self._grounding is not None:
                intent_result = await self._recognizer.recognize(question, turn_history)
                knowledge = await self._grounding(question, intent_result)
                orch_req.entities = intent_result.entities
                orch_req.intent = intent_result.intent
                orch_req.intent_group = intent_result.intent_group
                orch_req.urgency = intent_result.urgency
                orch_req.intent_confidence = intent_result.confidence
                # 只有检索到的知识才算出处，历史和记忆不能作为价格/时效的依据
                orch_req.grounding_sources = [knowledge] if knowledge else []
                if knowledge:
                    orch_req.context = f"{context}\n\n{knowledge}".strip()

            orch_result = await self._orchestrator.run(orch_req)
            actual_answer = orch_result.response

            scores = await self._judge.judge(question, actual_answer, context=context or None)
            passed = scores.overall >= self.PASS_THRESHOLD

            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": actual_answer})

            test_id = f"dialog_{case_idx}" if len(questions) == 1 else f"dialog_{case_idx}_turn_{turn_idx}"
            results.append(EvalResult(
                test_id=test_id,
                passed=passed,
                scores={
                    "relevance": scores.relevance,
                    "accuracy": scores.accuracy,
                    "completeness": scores.completeness,
                    "helpfulness": scores.helpfulness,
                    "overall": scores.overall,
                },
                detail=f"Q: {question[:30]}... → 综合评分 {scores.overall:.3f}",
                metadata={
                    "question": question,
                    "response": actual_answer,
                    "agent_type": orch_result.agent_type.value,
                    "primary_agent": (
                        orch_result.primary_agent.value if orch_result.primary_agent else None
                    ),
                    "supporting_agents": [a.value for a in orch_result.supporting_agents],
                    "intent": orch_result.intent.value if orch_result.intent else None,
                    "turn": turn_idx,
                    "conv_id": conv_id,
                    "judge_failed": scores.judge_failed,
                    "judge_error": scores.error,
                    # 答案策略拦截情况：评测报告里能直接看到哪条回复说了无依据的承诺
                    "handoff": orch_result.handoff,
                    "answer_confidence": orch_result.answer_confidence,
                    "policy_violations": [v.to_dict() for v in orch_result.policy_violations],
                },
            ))

        return results

    @staticmethod
    def _dialog_turns(case: dict[str, Any]) -> list[str]:
        turns = case.get("turns")
        if isinstance(turns, list):
            return [str(t) for t in turns if str(t).strip()]
        question = case.get("question")
        return [str(question)] if question else []

    @staticmethod
    def _history_context(history: list[dict[str, str]]) -> str:
        if not history:
            return ""
        lines = [f"{m['role']}: {m['content']}" for m in history[-8:]]
        return "[评测多轮历史]\n" + "\n".join(lines)

    def _detect_regressions(self, current: dict[str, float]) -> list[str]:
        """
        与**存档基线**对比，找出跌幅超过 REGRESSION_TOLERANCE 的指标。

        对比对象刻意是磁盘上的基线而不是上一次运行：连续多次小幅劣化时，
        "跟上一次比"每次都在容差内，累计跌幅却已经很大。
        """
        if self._baseline is None:
            return []
        base = self._baseline.avg_scores
        label = self._baseline.release or self._baseline.timestamp or "baseline"
        regressions = []
        for metric, value in current.items():
            if metric in base and base[metric] > 0:
                delta = (value - base[metric]) / base[metric]
                if delta < -self.REGRESSION_TOLERANCE:
                    regressions.append(
                        f"{metric}: {base[metric]:.3f} → {value:.3f} "
                        f"(相对基线 {label} 退化 {abs(delta):.1%})"
                    )
        return regressions

    def _recommendations(
        self,
        scores: dict[str, float],
        intent_metrics: dict[str, Any],
    ) -> list[str]:
        recs = []
        if scores.get("intent_accuracy", 1.0) < 0.90:
            recs.append("意图识别准确率 < 90%：增加 Few-shot 示例，或对低 F1 的意图类别补充训练数据")
        if scores.get("relevance", 1.0) < 0.75:
            recs.append("相关性偏低：检查 Agent system_prompt，确保 Agent 聚焦于用户问题")
        if scores.get("completeness", 1.0) < 0.75:
            recs.append("完整性偏低：Agent 可能过早结束回答，考虑在 prompt 中要求提供完整解决方案")
        if scores.get("helpfulness", 1.0) < 0.75:
            recs.append("有用性偏低：回答可能过于抽象，考虑要求 Agent 提供具体操作步骤")
        if not recs:
            recs.append("所有指标均达标，继续保持")
        return recs

    @property
    def history(self) -> list[EvalReport]:
        return self._history

    def _load_baseline(self) -> EvalReport | None:
        if not self._baseline_path or not self._baseline_path.exists():
            return None
        try:
            data = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            return self._report_from_dict(data)
        except Exception as ex:
            logger.warning(f"读取评测基线失败: {ex}")
            return None

    def _save_baseline(self, report: EvalReport) -> None:
        if not self._baseline_path:
            return
        try:
            self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
            self._baseline_path.write_text(
                json.dumps(asdict(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._baseline = report
        except Exception as ex:
            logger.warning(f"保存评测基线失败: {ex}")

    @staticmethod
    def _report_from_dict(data: dict[str, Any]) -> EvalReport:
        return EvalReport(
            timestamp=data.get("timestamp", ""),
            total=int(data.get("total", 0)),
            passed=int(data.get("passed", 0)),
            pass_rate=float(data.get("pass_rate", 0.0)),
            avg_scores=dict(data.get("avg_scores", {})),
            regressions=list(data.get("regressions", [])),
            recommendations=list(data.get("recommendations", [])),
            release=str(data.get("release", "unknown")),
            baseline_release=str(data.get("baseline_release", "")),
            baseline_timestamp=str(data.get("baseline_timestamp", "")),
            promoted=bool(data.get("promoted", False)),
            results=[
                EvalResult(
                    test_id=r.get("test_id", ""),
                    passed=bool(r.get("passed", False)),
                    scores=dict(r.get("scores", {})),
                    detail=r.get("detail", ""),
                    metadata=dict(r.get("metadata", {})),
                )
                for r in data.get("results", [])
            ],
        )


# ── 内置测试用例（开箱即用）──────────────────────────────────────────────────
#
# 标注边界规则（扩集时必须遵守，否则标签会互相打架、准确率数字失去意义）：
#
#   1. **细粒度优先**：能看出具体诉求的，一律标细粒度意图（9 个），不标宽泛大类。
#      "什么时候做好" → order_status，不是 order。
#   2. **宽泛大类只用于诉求不明确**：看得出是哪个域、但还不知道要干什么，才标
#      query / technical / billing / order / request。
#      "我想问一下我这个订单的事" → order（还不知道是问进度、改单还是补件）。
#   3. **escalation vs human_handoff**：要求升级到主管/经理/负责人 → escalation；
#      明确要求转人工/真人客服 → human_handoff。
#   4. **complaint vs feedback**：负面情绪且无具体诉求 → complaint；正面评价 → feedback。
#      带具体诉求的不满要标到诉求上（"门板裂了太差劲" → damage_defect）。
#
# 覆盖度：18 个细粒度意图 × 5 条 = 90 条。扩集前只有 11 条、覆盖 11 个意图，
# query / request / escalation / technical / billing / order / feedback 这 7 个是零覆盖——
# 也就是说旧的 "100% intent accuracy" 是在 61% 的意图上测出来的。

DEFAULT_INTENT_CASES: list[IntentTestCase] = [
    # ── 会话类（4）────────────────────────────────────────────────────────────
    IntentTestCase("你好",                                 "greeting"),
    IntentTestCase("在吗？",                               "greeting"),
    IntentTestCase("早上好",                               "greeting"),
    IntentTestCase("哈喽，有人吗",                         "greeting"),
    IntentTestCase("您好，打扰了",                         "greeting"),

    IntentTestCase("你们服务太差了！",                     "complaint"),
    IntentTestCase("这已经是第三次出问题了，太让人失望",   "complaint"),
    IntentTestCase("跟你们合作这么久，从来没这么糟心过",   "complaint"),
    IntentTestCase("我真的受够了",                         "complaint"),
    IntentTestCase("你们这个态度我很不满意",               "complaint"),

    IntentTestCase("师傅装得很好，谢谢",                   "feedback"),
    IntentTestCase("这次做得不错，很满意",                 "feedback"),
    IntentTestCase("柜子质量比我想的好",                   "feedback"),
    IntentTestCase("服务态度挺好的，点个赞",               "feedback"),
    IntentTestCase("谢谢你们，处理得很及时",               "feedback"),

    IntentTestCase("帮我处理一下这个事",                   "request"),
    IntentTestCase("麻烦你帮我登记一下",                   "request"),
    IntentTestCase("能帮我安排一下吗？",                   "request"),
    IntentTestCase("帮我跟进一下这件事",                   "request"),
    IntentTestCase("麻烦帮我记录到系统里",                 "request"),

    # ── 宽泛大类（5）──────────────────────────────────────────────────────────
    IntentTestCase("想问一下你们这边的流程是怎么走的",     "query"),
    IntentTestCase("你们营业时间是几点到几点？",           "query"),
    IntentTestCase("能不能介绍一下你们的服务范围？",       "query"),
    IntentTestCase("有没有什么需要我先准备的资料？",       "query"),
    IntentTestCase("我想了解一下大概情况",                 "query"),

    IntentTestCase("我要找你们主管",                       "escalation"),
    IntentTestCase("让你们经理来跟我说",                   "escalation"),
    IntentTestCase("这个事你做不了主，找能拍板的人",       "escalation"),
    IntentTestCase("我要投诉到你们领导那里",               "escalation"),
    IntentTestCase("叫你们负责人给我回电话",               "escalation"),

    IntentTestCase("这个板材是什么材质的？",               "technical"),
    IntentTestCase("你们的柜体用的什么五金？",             "technical"),
    IntentTestCase("台面能不能做无缝拼接？",               "technical"),
    IntentTestCase("这种饰面耐不耐刮？",                   "technical"),
    IntentTestCase("吊柜承重大概多少？",                   "technical"),

    IntentTestCase("付款方式都有哪些？",                   "billing"),
    IntentTestCase("你们开发票吗？",                       "billing"),
    IntentTestCase("账期一般怎么算？",                     "billing"),
    IntentTestCase("能不能走对公转账？",                   "billing"),
    IntentTestCase("结算流程是怎样的？",                   "billing"),

    IntentTestCase("我想问一下我这个订单的事",             "order"),
    IntentTestCase("关于我那单，有点情况想说",             "order"),
    IntentTestCase("我这单能不能再确认一下",               "order"),
    IntentTestCase("订单方面有个问题想咨询",               "order"),
    IntentTestCase("我这批货想跟你核对一下",               "order"),

    # ── 细粒度业务意图（9）────────────────────────────────────────────────────
    IntentTestCase("这批柜子什么时候能做好？",             "order_status"),
    IntentTestCase("我这单现在做到哪一步了？",             "order_status"),
    IntentTestCase("生产进度能查一下吗？",                 "order_status"),
    IntentTestCase("排产排到我了吗？",                     "order_status"),
    IntentTestCase("订单 #12345 现在什么状态",             "order_status"),

    IntentTestCase("能不能改一下送货日期？",               "delivery"),
    IntentTestCase("什么时候能送到工地？",                 "delivery"),
    IntentTestCase("送货是你们自己的车吗？",               "delivery"),
    IntentTestCase("这周五能安排送货吗？",                 "delivery"),
    IntentTestCase("货到了没人签收怎么办？",               "delivery"),

    IntentTestCase("帮我把这单取消掉",                     "order_change"),
    IntentTestCase("我想把门板颜色换一下",                 "order_change"),
    IntentTestCase("尺寸能不能再改一次？",                 "order_change"),
    IntentTestCase("这单加两个吊柜可以吗？",               "order_change"),
    IntentTestCase("把送货地址改成圣何塞的工地",           "order_change"),

    IntentTestCase("少发了一块门板，补件怎么走？",         "missing_part"),
    # 这条曾被规则通道误判为 installation（扩集时发现）：
    # 当时 MISSING_PART 只有 "少发了" 没有 "少了"，于是全句只有 "铰链" 一个锚点命中，
    # 构不成跨意图冲突，快通道就抢答了。修法是补上 "少了"（锚点 52→53），
    # 让它与 "铰链" 同时命中、触发既有的跨意图冲突逻辑，整体让位给三路融合。
    # 保留这条用例作为该修复的回归哨兵。
    IntentTestCase("拆箱发现少了两个铰链",                 "missing_part"),
    IntentTestCase("装箱单上有的东西没找到",               "missing_part"),
    IntentTestCase("你们漏发了一个抽屉面板",               "missing_part"),
    IntentTestCase("发错货了，型号跟单子上对不上",         "missing_part"),

    IntentTestCase("这套橱柜报价多少？",                   "quote_pricing"),
    IntentTestCase("加个岛台大概要加多少钱？",             "quote_pricing"),
    IntentTestCase("能不能给个大概预算？",                 "quote_pricing"),
    IntentTestCase("这个配置的价格怎么算",                 "quote_pricing"),
    IntentTestCase("报价单什么时候能出来？",               "quote_pricing"),

    IntentTestCase("定金付了怎么还显示未付？",             "payment_issue"),
    IntentTestCase("尾款被重复扣款了",                     "payment_issue"),
    IntentTestCase("这笔钱怎么对不上账",                   "payment_issue"),
    IntentTestCase("我转账了但你们说没收到",               "payment_issue"),
    IntentTestCase("退款什么时候能到账？",                 "payment_issue"),

    IntentTestCase("柜门关不严，铰链怎么调？",             "installation"),
    IntentTestCase("抽屉推进去有异响",                     "installation"),
    IntentTestCase("吊柜装歪了怎么办",                     "installation"),
    IntentTestCase("安装师傅什么时候到？",                 "installation"),
    IntentTestCase("台面和墙之间有缝要怎么处理",           "installation"),

    IntentTestCase("送来的门板有划痕",                     "damage_defect"),
    IntentTestCase("W3012 这块门板送来就是裂的",           "damage_defect"),
    IntentTestCase("台面边角磕掉了一块",                   "damage_defect"),
    IntentTestCase("柜体侧板有个鼓包",                     "damage_defect"),
    IntentTestCase("饰面起泡了，能返工吗",                 "damage_defect"),

    IntentTestCase("我要投诉，转人工！",                   "human_handoff"),
    IntentTestCase("转人工客服",                           "human_handoff"),
    IntentTestCase("能不能让真人跟我说",                   "human_handoff"),
    IntentTestCase("我不想跟机器人聊了",                   "human_handoff"),
    IntentTestCase("接个人工过来",                         "human_handoff"),
]

# 对话用例：覆盖每个专家域、两类答案策略拦截（价格 / 时效）、转人工、正面反馈，
# 外加两组多轮。扩集前 5 组，其中 order 域占 3 组，technical / billing 各 1 组，
# 安装、付款、送货、转人工、反馈全部无覆盖。
DEFAULT_DIALOG_CASES: list[dict[str, Any]] = [
    # ── order 域 ──────────────────────────────────────────────────────────────
    {"question": "我这单 #12345 说好上周做完，到现在还没消息"},
    {"question": "帮我把送货地址改成圣何塞的工地"},
    # ── technical 域 ──────────────────────────────────────────────────────────
    {"question": "W3012 这块门板送来就是裂的"},
    {"question": "柜门关不严，铰链我自己能调吗？"},
    {"question": "你们台面能做无缝拼接吗？用的什么材质"},
    # ── billing 域 ────────────────────────────────────────────────────────────
    {"question": "这套厨房加个岛台大概要加多少钱？"},
    {"question": "尾款被扣了两次，能帮我查一下吗？"},
    # ── 答案策略：时效承诺必须被拦 ────────────────────────────────────────────
    {"question": "这周五能安排送到圣何塞工地吗？"},
    # ── 转人工路径 ────────────────────────────────────────────────────────────
    {"question": "别跟我扯了，直接转人工"},
    # ── 正面反馈：不该触发检索，也不该长篇大论 ────────────────────────────────
    {"question": "师傅装得很好，谢谢你们"},
    # ── 多轮：补件（原有）─────────────────────────────────────────────────────
    {"turns": ["你好，我这单少了一块门板", "订单号是 #12345", "补件大概多久能到？"]},
    # ── 多轮：改单 → 末轮触发报价策略 ─────────────────────────────────────────
    {"turns": ["我想把门板颜色换成白的", "订单号 #67890", "换色要加钱吗？"]},
]
