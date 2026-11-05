"""
反馈回路命令行工具。

    python -m ai_sql_agent.feedback stats                      # 线上准确率与静默错误率
    python -m ai_sql_agent.feedback wrong                      # 待处理的答错清单
    python -m ai_sql_agent.feedback export-dataset out.json    # 把答错的问题导成评测集草稿
    python -m ai_sql_agent.feedback resolve <trace_id>         # 标记某条已处理
    python -m ai_sql_agent.feedback certified --user u1        # 看某人的认证范例

export-dataset 导出的是**草稿**：gold_sql 需要人来补。
模型答错的题，正确答案只能由懂业务的人给——这一步没有捷径，
但把「问题原文 + 模型答错的 SQL + 命中的库表」都摆好，补一条通常只要一两分钟。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

from .objects import Verdict
from .store import create_feedback_store

TODO = "TODO: 由业务同事补上正确 SQL"


def _stats(args: argparse.Namespace) -> int:
    store = create_feedback_store()
    stats = store.stats()

    if not stats.total:
        print(
            "还没有任何反馈。\n"
            "在查询页面点「对 / 错」即可积累——这是让工具越用越准的唯一数据来源。"
        )
        return 0

    print(f"反馈总数：{stats.total}（对 {stats.correct} · 错 {stats.wrong} · 说不准 {stats.unsure}）")
    print()
    print(f"线上准确率：      {stats.accuracy:.1%}   （有明确对错判定的样本）")
    print(f"线上静默错误率：  {stats.silent_error_rate:.1%}   （答错且当时没有任何风险提示）")
    print()
    print(f"答错但被提示过：  {stats.wrong_with_risk}  ← 提示起作用了，用户自己发现了")
    print(f"答错且毫无提示：  {stats.wrong_without_risk}  ← 最贵的一类，错数字会被当成事实")
    print(f"待处理的答错：    {stats.unresolved_wrong}")
    print()
    print(
        "这两个数与 benchmark 的口径一致，但题目来自真实用户。\n"
        "两边差得远时以这里为准——评测集只代表我们自己想到的问法。"
    )
    return 0


def _wrong(args: argparse.Namespace) -> int:
    store = create_feedback_store()
    rows = [
        row
        for row in store.recent(limit=args.limit, verdict=Verdict.WRONG.value)
        if args.all or not row.get("resolved")
    ]
    if not rows:
        print("没有待处理的答错记录。")
        return 0

    for row in rows:
        flag = "有提示" if row.get("had_risk") else "无提示"
        print(f"[{row.get('trace_id')}] {flag}  {row.get('query')}")
        if row.get("comment"):
            print(f"    用户说明：{row['comment']}")
        for step in row.get("sqls") or []:
            print(f"    {step.get('database')}: {step.get('sql')}")
        print()

    print(f"共 {len(rows)} 条。补完元数据或加进评测集后，用 resolve 标记处理完毕。")
    return 0


def _export_dataset(args: argparse.Namespace) -> int:
    store = create_feedback_store()
    rows = [
        row
        for row in store.recent(limit=args.limit, verdict=Verdict.WRONG.value)
        if args.all or not row.get("resolved")
    ]
    if not rows:
        print("没有可导出的答错记录。")
        return 0

    items: List[dict] = []
    for index, row in enumerate(rows, start=1):
        steps = row.get("sqls") or []
        databases = row.get("databases") or []
        items.append(
            {
                "id": f"fb{index:03d}",
                "category": "cross_db" if len(databases) > 1 else "from_feedback",
                "query": row.get("query", ""),
                "gold_sql": TODO,
                "gold_columns": [],
                "_source_trace_id": row.get("trace_id"),
                "_user_comment": row.get("comment", ""),
                "_model_answer_was": [
                    {"database": step.get("database"), "sql": step.get("sql")}
                    for step in steps
                    if isinstance(step, dict)
                ],
                "_had_risk_warning": bool(row.get("had_risk")),
            }
        )

    target = Path(args.output)
    if target.exists() and not args.overwrite:
        print(f"{target} 已存在；加 --overwrite 才会覆盖", file=sys.stderr)
        return 2
    target.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"已导出 {len(items)} 条到 {target}")
    print()
    print("下一步：")
    print(f"  1. 打开 {target}，为每条补上 gold_sql（下划线开头的字段是给人看的线索）；")
    print("  2. 补 gold_columns，用于算字段召回率（可选，但缺了这一项该条不计入召回）；")
    print("  3. 把补完的条目并进 benchmark/dataset.json，删掉下划线开头的字段；")
    print("  4. 重跑 python -m benchmark.gate --repeat 3。")
    print()
    print(
        "真实用户答错的题进了评测集，下次改动就再也不能悄悄退化到这里——\n"
        "这是评测集从「我们出的模板题」长成「用户真会问的问题」的唯一途径。"
    )
    return 0


def _resolve(args: argparse.Namespace) -> int:
    store = create_feedback_store()
    if store.mark_resolved(args.trace_id):
        print(f"已标记 {args.trace_id} 为处理完毕。")
        return 0
    print(f"没有找到 trace_id={args.trace_id} 的反馈。", file=sys.stderr)
    return 1


def _certified(args: argparse.Namespace) -> int:
    store = create_feedback_store()
    rows = store.certified(user_id=args.user, limit=args.limit)
    if not rows:
        print("还没有认证范例。用户点「对」之后，这条查询就会出现在这里。")
        return 0
    for item in rows:
        print(f"[{item.database}] {item.query}")
        print(f"    {item.sql}")
    print(f"\n共 {len(rows)} 条，会作为写法参考注入同类问题的生成 Prompt。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_sql_agent.feedback", description="用户反馈回路工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stats = sub.add_parser("stats", help="线上准确率与静默错误率")
    stats.set_defaults(func=_stats)

    wrong = sub.add_parser("wrong", help="待处理的答错清单")
    wrong.add_argument("--limit", type=int, default=50)
    wrong.add_argument("--all", action="store_true", help="含已处理的")
    wrong.set_defaults(func=_wrong)

    export = sub.add_parser("export-dataset", help="把答错的问题导成评测集草稿")
    export.add_argument("output", help="输出 JSON 路径")
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--all", action="store_true", help="含已处理的")
    export.add_argument("--overwrite", action="store_true")
    export.set_defaults(func=_export_dataset)

    resolve = sub.add_parser("resolve", help="标记一条答错已处理")
    resolve.add_argument("trace_id")
    resolve.set_defaults(func=_resolve)

    certified = sub.add_parser("certified", help="查看认证范例")
    certified.add_argument("--user", default="", help="只看某个用户的")
    certified.add_argument("--limit", type=int, default=50)
    certified.set_defaults(func=_certified)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
