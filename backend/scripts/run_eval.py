#!/usr/bin/env python
"""
独立跑一次内置评测并落盘基线，不需要起 Docker / Redis / Postgres。

与 /eval/run 走的是同一套对象（同样的 SkillManager、AnswerPolicy、IntentRecognizer、
AgentOrchestrator、EndToEndEvaluator），只是绕开了 FastAPI 生命周期和外部依赖。

用法：
    ANTHROPIC_API_KEY=<真实 key> backend/.venv/bin/python scripts/run_eval.py
    ...                                                   scripts/run_eval.py --promote

默认 promote=False。仓库里当前没有基线文件，所以首跑会自动落盘第一份作为起点；
之后每次跑都会和它对比，只有你确认这批分数可接受时才用 --promote 提升基线。
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 配置优先级：命令行前置的环境变量 > .env > shell 里的既有默认值。
#
# 为什么不能简单地 load_dotenv(override=False)：Claude Code / 部分 shell 会预置
# ANTHROPIC_BASE_URL=https://api.anthropic.com，不覆盖的话请求会打到官方端点而不是
# .env 里配的第三方网关。
# 为什么也不能简单地 override=True：那样会把命令行前置的 key 用 .env 里的占位符盖回去。
# 所以先记下命令行显式传进来的值，override 之后再把它们放回去。
try:
    from dotenv import load_dotenv

    _EXPLICIT = {
        k: v for k in ("LLM_PROVIDER",
                       "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                       "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                       "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
        if (v := os.environ.get(k, "").strip())
    }
    # shell 预置的官方端点不算"显式指定"，让 .env 里的网关配置赢
    if _EXPLICIT.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com":
        _EXPLICIT.pop("ANTHROPIC_BASE_URL")
    load_dotenv(_ROOT / ".env", override=True)
    os.environ.update(_EXPLICIT)
except ImportError:
    pass

from app.agents.agent_orchestrator import AgentOrchestrator  # noqa: E402
from app.core.answer_policy import AnswerPolicy  # noqa: E402
from app.core.embedder import Embedder  # noqa: E402
from app.core.intent_recognizer import IntentRecognizer  # noqa: E402
from app.core.llm_utils import resolve_llm_config  # noqa: E402
from app.core.reranker import Reranker  # noqa: E402
from app.core.skill_loader import SkillManager  # noqa: E402
from app.evaluation.evaluator import (  # noqa: E402
    DEFAULT_DIALOG_CASES,
    DEFAULT_INTENT_CASES,
    EndToEndEvaluator,
)
from app.storage.vector_store import LocalStore  # noqa: E402
from app.tools.knowledge_base import KnowledgeBase, should_use_knowledge  # noqa: E402
from app.tools.tool_manager import Tool, ToolManager  # noqa: E402

BASELINE = _ROOT / "data" / "eval" / "baseline.json"


def _config() -> dict:
    try:
        cfg = resolve_llm_config()
    except RuntimeError as ex:
        sys.exit(str(ex))
    key_var = "OPENAI_API_KEY" if cfg["provider"] == "openai" else "ANTHROPIC_API_KEY"
    if len(cfg["api_key"]) < 20:
        sys.exit(
            f"{key_var} 看起来是占位符（长度 {len(cfg['api_key'])}）。\n"
            f"请在 {_ROOT / '.env'} 里填真实 key，或在命令行前置：\n"
            f"    {key_var}=<真实 key> {sys.executable} {__file__}"
        )
    return cfg


async def main(promote: bool, force: bool, concurrency: int | None = None) -> None:
    cfg = _config()
    print(f"供应商: {cfg['provider']}  模型: {cfg['model']}  "
          f"base_url: {cfg.get('base_url', '(官方)')}")

    recognizer = IntentRecognizer(
        api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
        enable_fast_path=os.getenv("ENABLE_INTENT_FAST_PATH", "true").lower() != "false",
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL", "").strip() or None,
        embedding_api_key=os.getenv("EMBEDDING_API_KEY", "").strip() or None,
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip(),
    )
    print(f"意图 Embedding 后端: {recognizer.embedding_backend}")

    skills = SkillManager(root_dir=str(_ROOT / "skills"), max_prompt_chars=5000)
    skills.load()

    orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
        skill_manager=skills, recognizer=recognizer,
        policy=AnswerPolicy(enabled=True),
        replicas=1,   # 评测不需要多实例；Monitor 反馈闭环不在评测范围内
    )

    # ── RAG：SQLite 兜底后端，不依赖 Redis / Postgres ────────────────────────
    embedder = Embedder.from_env()
    store = LocalStore(db_path=str(_ROOT / "data" / "vector_store.db"))
    kb = KnowledgeBase(store=store, embedder=embedder)
    await kb.ensure_seeded()
    await kb.backfill_lexical()   # 与线上一致：老片段补关键词索引后再评测
    print(f"知识库: {await kb.doc_count_async()} 个片段（后端 {kb.backend}，向量 {embedder.backend} {embedder.dim} 维）")

    tools = ToolManager(
        api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
        shared_state=None,   # 熔断器退化为进程内，评测不需要跨实例共享
        reranker=Reranker.from_env(),
    )
    tools.register(Tool(
        name="knowledge_search",
        description="搜索知识库（稠密向量 + 关键词双路混合检索，RRF 融合）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
    ))

    async def grounding(message: str, intent_result) -> str:
        """复刻 /chat 的出处构建：按意图门控，命中规则通道时走单次直检。"""
        if not should_use_knowledge(message, intent=getattr(intent_result, "intent", None)):
            return ""
        try:
            if getattr(intent_result, "fast_path", False):
                result = await tools.call("knowledge_search", {"query": message, "top_k": 3})
            else:
                result = await tools.search_with_rewrite("knowledge_search", message, top_k=3)
        except Exception as ex:                      # noqa: BLE001
            print(f"  [warn] 检索失败，本轮无出处: {ex}")
            return ""
        if not result.success or not isinstance(result.data, list) or not result.data:
            return ""
        parts = ["[知识库检索结果]"]
        for i, item in enumerate(result.data[:3], start=1):
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                continue
            parts.append(
                f"{i}. 标题: {item.get('title', '未命名文档')}\n"
                f"   相关度: {item.get('score', '')}\n"
                f"   内容: {str(item['content'])[:600]}"
            )
        return "\n".join(parts) if len(parts) > 1 else ""

    evaluator = EndToEndEvaluator(
        orchestrator=orchestrator, recognizer=recognizer,
        api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
        grounding_provider=grounding,
        baseline_path=str(BASELINE),
        release=os.getenv("AI_HELPDESK_RELEASE", "cabinet-domain"),
        concurrency=concurrency,
    )
    had_baseline = BASELINE.exists()
    print(f"基线文件: {BASELINE}  ({'已存在，将做回归对比' if had_baseline else '不存在，本次自动建档'})")
    print(f"用例: {len(DEFAULT_INTENT_CASES)} 个意图 + {len(DEFAULT_DIALOG_CASES)} 个对话"
          f"（并发 {evaluator.concurrency}）\n")

    # 注意这里恒传 promote=False：是否提升要等回归检测跑完才能判断，
    # 直接传 promote=True 会在"这次比基线差 13%"的情况下照样把它写成新基线——
    # 那正是 promote 门控本该拦住的事。真正的提升在下面显式调用。
    report = await evaluator.run(
        intent_cases=DEFAULT_INTENT_CASES,
        dialog_cases=DEFAULT_DIALOG_CASES,
        promote=False,
    )

    print("=" * 62)
    print(f"通过 {report.passed}/{report.total}   pass_rate {report.pass_rate:.1%}")
    for k, v in report.avg_scores.items():
        print(f"  {k:<16} {v}")
    print("回归:", report.regressions or "无")
    for r in report.recommendations:
        print("建议:", r)
    print("=" * 62)
    for r in report.results:
        flag = "PASS" if r.passed else "FAIL"
        print(f"[{flag}] {r.test_id:<20} {r.detail[:70]}")

    # 模型服务不可用、评测中途中止：上面打印的只是中止前完成的那几项。不往下走——
    # 下面的分支会报"基线已写入""已提升为新基线"，而这一轮既没写基线也提升不了
    # （evaluator 对没跑完的报告两样都不做）。
    if report.incomplete:
        print(f"\n评测未完成：模型服务不可用（{report.incomplete_reason}），已中止。"
              f"计划 {report.planned} 项，完成 {report.total} 项。")
        if evaluator.incomplete_path is not None:
            print(f"已完成的结果保存在 {evaluator.incomplete_path}。基线没有改动，本次结果不能提升。")
        print("处理好凭证或额度后重新运行；脚本不会自己重试。")
        sys.exit(3)

    # 跑挂了就不许留基线。evaluator.run() 在没有基线文件时会自动落盘第一份，
    # 但如果这一跑本身是坏的（key 失效、网关不通、判官全挂），那份基线全是
    # judge 的失败默认值 0.5——它比"没有基线"更糟：后续每次跑都会拿真实分数
    # 去和一堆 0.5 比，回归检测永远报"大幅改善"，等于把闸门焊死在打开状态。
    judge_failed = sum(1 for r in report.results if r.metadata.get("judge_failed"))
    # Agent 返回空回复也是坏跑：judge 会老实打 0 分，看起来像"模型不稳定"，
    # 实际是推理把 token 预算吃光后正文块不存在。混进基线会污染整条曲线。
    empty_answers = [
        r.test_id for r in report.results
        if "response" in r.metadata and not str(r.metadata.get("response", "")).strip()
    ]
    if judge_failed or empty_answers:
        if judge_failed:
            print(f"\n本次评测有 {judge_failed} 个用例的 LLM Judge 调用失败，分数不可信。")
        if empty_answers:
            print(f"\n本次评测有 {len(empty_answers)} 个用例的 Agent 回复为空：{empty_answers}")
            print("这通常是 AI_HELPDESK_ANSWER_MAX_TOKENS 太小，推理模型没留出正文预算。")
        if not had_baseline and BASELINE.exists():
            BASELINE.unlink()
            print(f"已删除自动落盘的基线 {BASELINE}，避免把坏分数固化成回归基准。")
        sys.exit(1)

    if BASELINE.exists():
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        print(f"\n基线已写入 {BASELINE}（timestamp={data.get('timestamp')}）")
    if promote and had_baseline:
        if report.regressions and not force:
            print("\n检测到相对基线的回归，已拒绝提升：")
            for r in report.regressions:
                print("  -", r)
            print("确认这是可接受的口径变化（例如评测 harness 本身改了）再加 --force。")
            sys.exit(2)
        evaluator.promote_baseline(report)
        print(f"\n已提升为新基线（release={report.release}）。")
    elif not promote and had_baseline:
        print("未提升基线（promote=False）。确认这批分数可接受后加 --promote 再跑一次。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", action="store_true", help="把本次结果写为新基线")
    ap.add_argument("--force", action="store_true",
                    help="即使检测到回归也提升基线（仅用于 harness 口径本身变更）")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="同时在跑的用例数，默认取 EVAL_CONCURRENCY（4）")
    _a = ap.parse_args()
    asyncio.run(main(_a.promote, _a.force, _a.concurrency))
