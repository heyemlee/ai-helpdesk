#!/usr/bin/env python
"""
工具循环的轨迹评测：规则组和循环组成对跑，按确定性规则打分，写一份 JSON 和一份 Markdown 摘要。

**会调真实模型。** 不加 --yes 只打印计划和调用量估算，一次模型都不调；--dry-run 只校验
用例和契约夹具。全部串行、一条一条跑；模型服务一旦不可用就停下，已经跑完的照样写进报告，
脚本不会自己重试。打分规则和门槛见 app/evaluation/trajectory.py。

用法（在 backend/ 下）：
    .venv/bin/python scripts/run_trajectory_eval.py --dry-run          # 校验用例和夹具
    .venv/bin/python scripts/run_trajectory_eval.py                    # 只看计划和调用量估算
    .venv/bin/python scripts/run_trajectory_eval.py --pilot --yes      # 试跑：每类 1 条，各 1 次
    .venv/bin/python scripts/run_trajectory_eval.py --yes              # 正式：B、C 各 3 次，其余各 1 次

订单查询走桩，读 a2a-contract 的 fixtures/data-part/：设 A2A_CONTRACT_DIR，或把那个仓库放在
本仓库同级目录。报告默认写到 data/runtime/trajectory/<时间>/（不进版本库）。
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 配置优先级同 run_eval.py：命令行前置的环境变量 > .env > shell 里的既有默认值
try:
    from dotenv import load_dotenv

    _EXPLICIT = {
        k: v for k in ("LLM_PROVIDER",
                       "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                       "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                       "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
        if (v := os.environ.get(k, "").strip())
    }
    if _EXPLICIT.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com":
        _EXPLICIT.pop("ANTHROPIC_BASE_URL")
    load_dotenv(_ROOT / ".env", override=True)
    os.environ.update(_EXPLICIT)
except ImportError:
    pass

from app.agents.agent_orchestrator import AgentOrchestrator  # noqa: E402
from app.agents.tool_loop import LoopConfig, deadline_for, deadline_warnings  # noqa: E402
from app.core.answer_policy import AnswerPolicy  # noqa: E402
from app.core.embedder import Embedder  # noqa: E402
from app.core.intent_recognizer import IntentRecognizer  # noqa: E402
from app.core.llm_utils import create_llm_client, resolve_llm_config  # noqa: E402
from app.core.reranker import Reranker  # noqa: E402
from app.core.skill_loader import SkillManager  # noqa: E402
from app.evaluation.evaluator import LLMJudge  # noqa: E402
from app.evaluation.trajectory import (  # noqa: E402
    ARMS,
    CATEGORIES,
    DEFAULT_CASES,
    UNAVAILABLE,
    ContractFixtures,
    StubSqlAgent,
    TrajectoryHarness,
    contract_dir,
    load_cases,
    render_markdown,
    summarize,
)
from app.storage.vector_store import LocalStore  # noqa: E402
from app.tools.knowledge_base import KnowledgeBase  # noqa: E402
from app.tools.tool_manager import Tool, ToolManager  # noqa: E402
from app.tools.turn_tools import Prefetch, loop_tools  # noqa: E402

JUDGED = ("A", "B", "C")


def _plan(cases, categories, repeats: int, repeats_bc: int, pilot: bool):
    """要跑的 (用例, 第几次重复)。试跑每类只取第一条、只跑一次。"""
    chosen = [c for c in cases if c.category in categories]
    if pilot:
        firsts: dict[str, object] = {}
        for case in chosen:
            firsts.setdefault(case.category, case)
        return [(case, 0) for case in firsts.values()]
    plan = []
    for case in chosen:
        times = repeats_bc if case.category in ("B", "C") else repeats
        plan.extend((case, i) for i in range(times))
    return plan


def _estimate(plan, arms, intent: str, judge: bool) -> tuple[int, int]:
    """
    真实模型调用次数的粗估（下限, 上限）。每组成对样本：
      准备阶段：意图投票 0–1 次（label 模式 0），知识库预取的改写和 LLM 重排 0–4 次；
      规则组作答 1 次；循环组作答 1–3 次（并行专家时翻倍，这里不算）；评审每组 1 次（只评 A、B、C）。
    """
    low = high = 0
    for case, _ in plan:
        intent_calls = 0 if intent == "label" else 1
        low += intent_calls
        high += intent_calls + 4
        if "rules" in arms:
            low, high = low + 1, high + 1
        if "loop" in arms:
            low, high = low + 1, high + 3
        if judge and case.category in JUDGED:
            low, high = low + len(arms), high + len(arms)
    return low, high


def _judge_gate(summary: dict, results, arms) -> None:
    """评审分那一道门槛：B、C 类循环组不低于规则组，A、B、C 整体降幅不超过 0.02。"""
    def mean(arm: str, categories: tuple[str, ...]) -> float | None:
        scored = [r.grade.get("judge") for r in results
                  if r.arm == arm and r.category in categories and r.grade.get("judge") is not None]
        return round(sum(scored) / len(scored), 4) if scored else None

    for arm in arms:
        summary["arms"].setdefault(arm, {})["judge_mean"] = mean(arm, JUDGED)
    if set(arms) != set(ARMS) or "gates" not in summary:
        return
    bc = (mean("rules", ("B", "C")), mean("loop", ("B", "C")))
    overall = (mean("rules", JUDGED), mean("loop", JUDGED))
    passed = None
    if None not in bc and None not in overall:
        passed = bc[1] >= bc[0] and overall[1] - overall[0] >= -0.02
    summary["gates"][-1] = {"name": "评审分（B、C 不低于规则组，整体降幅 ≤ 0.02）",
                            "value": {"bc": bc, "overall": overall}, "threshold": "见名称", "passed": passed}


def _config() -> dict:
    try:
        cfg = resolve_llm_config()
    except RuntimeError as ex:
        sys.exit(str(ex))
    key_var = "OPENAI_API_KEY" if cfg["provider"] == "openai" else "ANTHROPIC_API_KEY"
    if len(cfg["api_key"]) < 20:
        sys.exit(f"{key_var} 看起来是占位符（长度 {len(cfg['api_key'])}），在 {_ROOT / '.env'} 里填真实 key。")
    return cfg


def _check_fixtures(cases, fixtures: ContractFixtures | None) -> list[str]:
    missing = []
    for case in cases:
        for stub in case.stubs:
            name = stub.get("fixture")
            if name and name != UNAVAILABLE and (fixtures is None or not fixtures.exists(name)):
                missing.append(f"{case.id}: {name}")
    return missing


async def _build(cfg: dict, documents: list[dict], workdir: pathlib.Path, intent: str):
    """和 run_eval.py 同一套对象，知识库换成临时目录里的 SQLite（内置文档 + 用例文件里的文档）。"""
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
        enable_fast_path=os.getenv("ENABLE_INTENT_FAST_PATH", "true").lower() != "false",
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL", "").strip() or None,
        embedding_api_key=os.getenv("EMBEDDING_API_KEY", "").strip() or None,
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip(),
    )
    skills = SkillManager(root_dir=str(_ROOT / "skills"), max_prompt_chars=5000)
    skills.load()
    orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
        skill_manager=skills, recognizer=recognizer, policy=AnswerPolicy(enabled=True), replicas=1,
    )
    kb = KnowledgeBase(store=LocalStore(db_path=str(workdir / "kb.db")), embedder=Embedder.from_env())
    await kb.ensure_seeded()
    if documents:
        await kb.import_documents(documents)
    await kb.backfill_lexical()
    tools = ToolManager(api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
                        shared_state=None, reranker=Reranker.from_env())
    tools.register(Tool(
        name="knowledge_search",
        description="搜索知识库（稠密向量 + 关键词双路混合检索，RRF 融合）",
        handler=kb.search_handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                "required": ["query"]},
        cache_ttl=300.0,
        supports_rerank=True,
    ))
    return recognizer, orchestrator, tools


async def main(args) -> int:
    cases, documents = load_cases(args.cases)
    categories = [c.strip().upper() for c in args.categories.split(",") if c.strip()]
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        sys.exit(f"不认识的类别：{unknown}，可选 {list(CATEGORIES)}")
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    if not set(arms) <= set(ARMS):
        sys.exit(f"--arms 只能是 {ARMS} 里的")

    root = contract_dir()
    fixtures = ContractFixtures(root) if root else None
    missing = _check_fixtures(cases, fixtures)
    plan = _plan(cases, categories, args.repeats, args.repeats_bc, args.pilot)
    low, high = _estimate(plan, arms, args.intent, args.judge)
    print(f"用例 {len(cases)} 条（{', '.join(f'{k}{sum(c.category == k for c in cases)}' for k in CATEGORIES)}），"
          f"本次计划 {len(plan)} 组成对样本，组别 {arms}，意图来自 {args.intent}")
    print(f"契约夹具：{root or '没找到'}" + (f"，缺 {len(missing)} 个：{missing}" if missing else ""))
    print(f"真实模型调用估算：{low}–{high} 次，全部串行")
    # 循环组的墙钟上限按能查库的专家算：查库工具的超时来自 SQL_AGENT_TIMEOUT_S（.env）。
    # 配得太小时模型发起的查库会被一律拒绝，B 类就测不出东西，所以跑之前先说出来。
    config = LoopConfig.from_env({**os.environ, "AI_HELPDESK_TOOL_LOOP": "on"})
    loop_tool_set = list(loop_tools(Prefetch(tool_manager=object(), sql_agent=StubSqlAgent())).values())
    deadline = (f"{deadline_for(config.budget, loop_tool_set):g} 秒（"
                f"{'显式配置' if config.budget.deadline_s is not None else '按慢工具的超时推算'}）")
    print(f"工具循环墙钟上限：{deadline}")
    for warning in deadline_warnings(config, loop_tool_set):
        print(f"提醒：{warning}")
    if args.dry_run:
        return 1 if missing or root is None else 0
    if missing or root is None:
        sys.exit("契约夹具不全，先设好 A2A_CONTRACT_DIR")
    if not args.yes:
        print("没有加 --yes：只打印计划，不调模型。确认调用量可以接受后加 --yes 再跑。")
        return 0

    cfg = _config()
    print(f"供应商: {cfg['provider']}  模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")
    out = pathlib.Path(args.out) if args.out else (
        _ROOT / "data" / "runtime" / "trajectory" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as workdir:
        recognizer, orchestrator, tools = await _build(cfg, documents, pathlib.Path(workdir), args.intent)
        harness = TrajectoryHarness(orchestrator=orchestrator, knowledge=tools, recognizer=recognizer,
                                    fixtures=fixtures, config=config, intent_source=args.intent)
        judge = LLMJudge(create_llm_client(cfg["api_key"], cfg.get("base_url")), cfg["model"]) \
            if args.judge else None
        results, stopped = [], ""
        for i, (case, repeat) in enumerate(plan, start=1):
            stopped = orchestrator.llm_unavailable_reason()
            if stopped:
                print(f"模型服务不可用（{stopped}），停在第 {i} 组之前。")
                break
            try:
                runs = await harness.run_case(case, arms, repeat)
            except Exception as ex:          # noqa: BLE001 —— 一条用例出错不拖垮整批
                print(f"[{i}/{len(plan)}] {case.id}#{repeat} 出错，跳过：{type(ex).__name__}: {ex}")
                continue
            if judge and case.category in JUDGED:
                context = "\n".join(f"{t['role']}: {t['content']}" for t in case.history) or None
                for run in runs:
                    scores = await judge.judge(case.message, run.answer, context=context)
                    run.grade["judge"] = None if scores.judge_failed else scores.overall
            results.extend(runs)
            line = "  ".join(f"{r.arm}: 到手 {r.grade['met']}/{r.grade['needs']} 调用 {r.grade['model_tool_calls']} "
                             f"模型 {r.usage.get('model_calls', 0)} 次 {r.latency_ms:.0f}ms" for r in runs)
            print(f"[{i}/{len(plan)}] {case.id}#{repeat}  {line}")

    summary = summarize(results, cases)
    if args.judge:
        _judge_gate(summary, results, arms)
    meta = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "供应商 / 模型": f"{cfg['provider']} / {cfg['model']}",
        "意图来源": args.intent,
        "组别": ", ".join(arms),
        "计划 / 完成的成对样本": f"{len(plan)} / {len({(r.case_id, r.repeat) for r in results})}",
        "中途停下的原因": stopped or "无",
        "工具循环墙钟上限": deadline,
        "契约夹具": str(root),
        "评审": "开" if args.judge else "关",
    }
    (out / "report.json").write_text(json.dumps(
        {"meta": meta, "summary": summary, "runs": [asdict(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(render_markdown(summary, meta), encoding="utf-8")
    print(f"\n报告：{out / 'report.json'}\n摘要：{out / 'summary.md'}")
    return 3 if stopped else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--categories", default=",".join(CATEGORIES), help="例如 B,C")
    ap.add_argument("--arms", default=",".join(ARMS), help="rules,loop 或其中之一")
    ap.add_argument("--repeats", type=int, default=1, help="A、D、E、F 类每条跑几次")
    ap.add_argument("--repeats-bc", type=int, default=3, help="B、C 类每条跑几次")
    ap.add_argument("--pilot", action="store_true", help="试跑：每类 1 条，各 1 次")
    ap.add_argument("--intent", choices=("model", "label"), default="model",
                    help="意图来自真实投票（默认），或直接用用例标注")
    ap.add_argument("--judge", action="store_true", help="用现有的 LLMJudge 给 A、B、C 类的回答打分")
    ap.add_argument("--out", default="", help="报告目录，默认 data/runtime/trajectory/<时间>/")
    ap.add_argument("--dry-run", action="store_true", help="只校验用例和契约夹具，不调模型")
    ap.add_argument("--yes", action="store_true", help="确认调用量后真的跑（会调真实模型）")
    sys.exit(asyncio.run(main(ap.parse_args())))
