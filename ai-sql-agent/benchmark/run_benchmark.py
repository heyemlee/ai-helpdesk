from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_sql_agent.core import get_settings
from benchmark.runner import (
    ABORT_MODEL_ACCESS,
    ABORT_MODEL_UNAVAILABLE,
    ABORT_SUMMARY,
    EXIT_INCOMPLETE,
    BenchmarkRunner,
    describe_degraded,
    save_incomplete,
)

# 离线冒烟子集：Mock 模型可以正确处理的样例（单库 + 跨库各一条）。
# 完整 150 条评测需要配置模型凭证（OPENAI_API_KEY / ANTHROPIC_API_KEY）走真实模型。
SMOKE_IDS = ["q003", "q010", "q046", "q101"]
"""离线冒烟子集：Mock 模型能正确处理的样例，覆盖单表 / 多表 join / 跨库三类。"""


def _report_incomplete(report, *, output, completed_rounds=None, rounds_planned=1) -> int:
    """
    评测中途停下（模型凭证不可用，或者某一题的模型调用失败）：把已完成的部分存下来，
    说明没有结论，返回非零。

    不打印准确率汇总——只跑了一部分的数字摆在那里，早晚会被人当成结果引用。
    """
    summary = ABORT_SUMMARY.get(report.abort_reason, ABORT_SUMMARY[ABORT_MODEL_ACCESS])
    payload = {
        "abort_reason": report.abort_reason,
        "abort_detail": report.abort_detail,
        "rounds_planned": rounds_planned,
        "rounds_completed": len(completed_rounds or []),
        "completed_rounds": completed_rounds or [],
        "partial_round": report.to_dict(),
    }
    target = save_incomplete(payload, output, "benchmark")
    print("=" * 60, file=sys.stderr)
    print(
        f"[未完成] {summary}，评测已停止：这一轮跑完 {report.total}/{report.planned_total} 条"
        + (f"，此前完整跑完 {len(completed_rounds)}/{rounds_planned} 轮" if rounds_planned > 1 else "")
        + "。",
        file=sys.stderr,
    )
    for block in report.abort_detail:
        if block.get("scope") == "item":
            print(f"[未完成] {describe_degraded(block)}。", file=sys.stderr)
            continue
        print(
            f"[未完成] 已知故障：{block.get('reason')}（范围 {block.get('scope')}"
            + (f"，模型 {block.get('model')}" if block.get("model") else "")
            + f"，自 {block.get('since')}）",
            file=sys.stderr,
        )
    print(f"[未完成] 已完成的部分已写入 {target}（标注 incomplete，不能当作评测结果引用）。", file=sys.stderr)
    fix = "确认模型服务恢复后" if report.abort_reason == ABORT_MODEL_UNAVAILABLE else "修复凭证后"
    print(f"[未完成] 没有补跑，也不会自动重试。{fix}请重新安排完整的一轮。", file=sys.stderr)
    return EXIT_INCOMPLETE


def _run_repeated(runner, args) -> int:
    """跑 N 轮，报告均值与区间，让波动显式可见。"""
    from statistics import mean

    accuracies, recalls, silent, false_alarm, grouped = [], [], [], [], {}
    completed_rounds = []
    for trial in range(1, args.repeat + 1):
        report = runner.run(
            categories=args.category,
            ids=SMOKE_IDS if args.smoke else None,
            limit=args.limit,
        )
        if report.incomplete:
            return _report_incomplete(
                report,
                output=args.output,
                completed_rounds=completed_rounds,
                rounds_planned=args.repeat,
            )
        completed_rounds.append(
            {
                "round": trial,
                "accuracy": report.accuracy,
                "column_recall": report.column_recall,
                "silent_error_rate": report.silent_error_rate,
                "false_alarm_rate": report.false_alarm_rate,
                "grouped_accuracy": report.grouped_accuracy(),
            }
        )
        accuracies.append(report.accuracy)
        recalls.append(report.column_recall)
        silent.append(report.silent_error_rate)
        false_alarm.append(report.false_alarm_rate)
        for group, value in report.grouped_accuracy().items():
            grouped.setdefault(group, []).append(value)
        print(
            f"[第 {trial}/{args.repeat} 轮] "
            f"执行准确率={report.accuracy:.2%} 字段召回率={report.column_recall:.2%} "
            f"静默错误率={report.silent_error_rate:.2%}",
            flush=True,
        )

    def summarize(name, values):
        print(
            f"  {name}: 均值 {mean(values):.2%}  "
            f"区间 [{min(values):.2%}, {max(values):.2%}]  "
            f"跨度 {max(values) - min(values):.1%}"
        )

    print("=" * 60)
    print(f"{args.repeat} 轮汇总")
    summarize("执行准确率", accuracies)
    summarize("字段召回率", recalls)
    for group, values in sorted(grouped.items()):
        summarize(group, values)
    summarize("静默错误率（越低越好）", silent)
    summarize("风险提示误报率", false_alarm)
    print("\n注：区间跨度反映模型输出的不确定性，不是代码变更造成的。")
    print("对比两次改动的效果时，跨度小于该区间的差异不能认定为有效。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI SQL Agent 150 条执行准确率评测")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="只跑离线冒烟子集（无 API Key 时使用）",
    )
    parser.add_argument(
        "--category",
        action="append",
        help="只评测指定类别（single_table / multi_join / aggregation / having / cross_db / time_range）",
    )
    parser.add_argument("--limit", type=int, help="限制评测条数")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "重复跑 N 轮并报告均值与区间。"
            "模型输出并非完全确定，单次跑分不可靠——实测同一份代码、同一批 25 条"
            "跨库题，准确率在 16%%～36%% 之间波动。要引用的数字建议至少跑 3 轮。"
        ),
    )
    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="关闭回溯回路。与默认跑一次对比即得「回溯增益」",
    )
    parser.add_argument("--output", help="报告 JSON 输出路径")
    parser.add_argument(
        "--dataset",
        help=(
            "评测集 JSON 路径，默认 benchmark/dataset.json。"
            "中英文业务用例在 benchmark/bilingual_cases.json（4 个场景各一中一英）。"
        ),
    )
    args = parser.parse_args()

    # 用 Settings 判断而不是 os.getenv：凭证可能配在 .env 里，
    # os.getenv 读不到会误报「未检测到 Key」并让门禁悄悄降级成冒烟模式。
    if not args.smoke and not get_settings().has_model_credentials:
        print(
            "[warn] 未检测到模型凭证（OPENAI_API_KEY / ANTHROPIC_API_KEY），Mock 模型只能覆盖冒烟样例；"
            "完整评测请配置 API Key，或使用 --smoke 跑离线子集。",
            file=sys.stderr,
        )

    runner = BenchmarkRunner(enable_repair=not args.no_repair, dataset_path=args.dataset)

    if args.repeat > 1:
        return _run_repeated(runner, args)

    report = runner.run(
        categories=args.category,
        ids=SMOKE_IDS if args.smoke else None,
        limit=args.limit,
    )
    if report.incomplete:
        return _report_incomplete(report, output=args.output)

    print("=" * 60)
    print(f"模式：{'关闭回溯' if args.no_repair else '开启回溯'}")
    print(f"总计：{report.correct}/{report.total}  执行准确率={report.accuracy:.2%}")
    print(
        f"字段召回率：{report.column_recall:.2%}"
        f"（平均进图 {report.avg_retrieved_columns:.1f}/{report.total_columns} "
        f"= {report.avg_column_ratio:.1%}）"
    )
    # 静默错误率单列：执行失败、空结果告警、降级答复用户都看得见，
    # 只有「跑通了、有结果、没提示、但是错的」这一类需要单独盯。
    silent_items = [item for item in report.items if item.silent_error]
    print(
        f"静默错误率：{report.silent_error_rate:.2%}"
        f"（{len(silent_items)}/{report.total}）  "
        f"风险提示误报率：{report.false_alarm_rate:.2%}"
    )
    for group, accuracy in report.grouped_accuracy().items():
        print(f"  {group}: {accuracy:.2%}")
    for category in sorted(report.category_totals):
        print(
            f"    {category}: "
            f"{report.category_correct.get(category, 0)}"
            f"/{report.category_totals[category]} "
            f"({report.category_accuracy(category):.2%})"
        )

    if silent_items:
        print(f"\n静默错误样例（前 5 条 / 共 {len(silent_items)} 条）：")
        for item in silent_items[:5]:
            print(f"  [{item.item_id}] {item.query}")
            print(f"      sql: {item.predicted_sql}")

    failures = [item for item in report.items if not item.correct]
    if failures:
        print(f"\n失败样例（前 10 条 / 共 {len(failures)} 条）：")
        for item in failures[:10]:
            print(f"  [{item.item_id}] {item.query}")
            if item.error:
                print(f"      error: {item.error}")

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告已写入 {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
