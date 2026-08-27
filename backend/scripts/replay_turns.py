#!/usr/bin/env python3
"""
拿真实流量重放（事件日志里记下的轮次）。在本机跑，连 POSTGRES_DSN（或 --dsn）那个库。

  python scripts/replay_turns.py --instance staff --mode route  [--hours 24 | --turn ID …] [--outcome handoff]
  python scripts/replay_turns.py --instance staff --mode guard  [同上]
  python scripts/replay_turns.py --instance staff --mode export --turn ID … [--category B] [--out cases.json]
  python scripts/replay_turns.py --instance staff --mode answer [同上] [--yes]

| 方式 | 做什么 | 调模型 |
|---|---|---|
| route | 用记下的意图、实体和原话，拿当前代码重算两道门控和路由，报出会变的轮次 | 否 |
| guard | 把每一步的原始输出喂给当前的答案守卫，和记录的替换比 | 否 |
| export | 导出成 data/eval/trajectory_cases.json 的用例骨架，needs 和期望要人来填 | 否 |
| answer | 从事件重建请求，用当前代码和模型重新作答（规则组：专家单次生成，不带工具），新旧回答并排 | **是** |

answer 不带 --yes 只打印计划和调用量；带了才跑——跑之前要有人安排，这和评测脚本是同一个规矩。
重建出来的请求和当时的对不上（脚手架、SOP 改过）时，报告里会标出来：那是"用当前代码重放旧输入"，
不是复现。原文已经按保留期清空的轮次重建不了，跳过并计数。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
from typing import Any

_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from app.events.projection import summarize_turn  # noqa: E402
from app.events.replay import (  # noqa: E402
    ReplayError,
    export_case,
    rebuild_request,
    replay_guard,
    replay_route,
)


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拿事件日志里的真实轮次重放：route / guard / export / answer")
    parser.add_argument("--instance", required=True, choices=["staff", "customer"])
    parser.add_argument("--mode", required=True, choices=["route", "guard", "export", "answer"])
    parser.add_argument("--dsn", default="", help="默认读 POSTGRES_DSN")
    parser.add_argument("--turn", action="append", default=[], help="只看这几轮，可以给多个")
    parser.add_argument("--hours", type=float, default=24.0, help="没给 --turn 时看最近多少小时开始的轮次")
    parser.add_argument("--outcome", default="", help="只看这种结局的：answered / handoff / clarification / unavailable")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--category", default="B", help="export：导出的用例算哪一类（A–F）")
    parser.add_argument("--out", default="", help="export：写到这个文件，默认打到标准输出")
    parser.add_argument("--yes", action="store_true", help="answer：真的调模型重新作答")
    return parser.parse_args(argv)


async def select_turns(args: argparse.Namespace, store) -> dict[str, list]:
    """挑轮次：给了 --turn 就是这几轮，否则按时间段；再按结局筛。返回 turn_id → 事件。"""
    turn_ids = list(dict.fromkeys(args.turn)) or await store.turn_ids_between(
        time.time() - args.hours * 3600, limit=args.limit)
    by_turn = await store.events_of_turns(turn_ids[:args.limit])
    if args.outcome:
        by_turn = {t: evs for t, evs in by_turn.items() if summarize_turn(evs)["outcome"] == args.outcome}
    return by_turn


def _route(by_turn: dict[str, list]) -> dict[str, Any]:
    from app.agents.agent_orchestrator import AgentOrchestrator

    # 只用它的路由规则和专家池，不发请求：客户端造出来不连网
    orchestrator = AgentOrchestrator(api_key="replay-no-calls")
    report: dict[str, Any] = {"compared": 0, "changed": [], "skipped": 0, "not_rebuilt": 0}
    for turn_id, events in by_turn.items():
        try:
            replay = replay_route(events, orchestrator)
        except ReplayError:
            report["not_rebuilt"] += 1
            continue
        if replay is None:
            report["skipped"] += 1
            continue
        report["compared"] += 1
        if replay.differences:
            report["changed"].append({"turn_id": turn_id, "fields": replay.differences,
                                      "recorded": replay.recorded, "replayed": replay.replayed})
    return report


def _guard(by_turn: dict[str, list]) -> dict[str, Any]:
    report: dict[str, Any] = {"compared": 0, "changed": [], "not_rebuilt": 0, "not_reproduced": []}
    for turn_id, events in by_turn.items():
        try:
            rebuilt = rebuild_request(events)
            replays = replay_guard(events)
        except ReplayError:
            report["not_rebuilt"] += 1
            continue
        if rebuilt.verified is False:
            report["not_reproduced"].append(turn_id)
        for replay in replays:
            report["compared"] += 1
            if not replay.same:
                report["changed"].append({"turn_id": turn_id, "agent": replay.agent,
                                          "recorded": replay.recorded, "replayed": replay.replayed})
    return report


def _export(by_turn: dict[str, list], category: str) -> dict[str, Any]:
    cases, skipped = [], 0
    for turn_id, events in by_turn.items():
        try:
            cases.append(export_case(events, case_id=f"X-{turn_id[-8:]}", category=category))
        except ReplayError:
            skipped += 1
    return {"cases": cases, "skipped": skipped}


async def _answer(by_turn: dict[str, list], yes: bool) -> dict[str, Any]:
    plan = {"turns": len(by_turn), "estimated_model_calls": sum(
        max(1, sum(1 for e in events if e.type == "agent_started")) for events in by_turn.values())}
    if not yes:
        return {"plan": plan, "note": "这是计划：加 --yes 才真的调模型（要先有人安排）"}

    from app.agents.agent_orchestrator import AgentOrchestrator
    from app.core import model_settings
    from app.core.answer_policy import AnswerPolicy
    from app.core.llm_utils import create_llm_client
    from app.core.model_runtime import chat_config_from
    from app.core.skill_loader import SkillManager

    cfg = chat_config_from(model_settings.load_store(model_settings.settings_path()))
    skills = SkillManager(root_dir=os.getenv("AI_HELPDESK_SKILLS_DIR", str(_ROOT / "skills")))
    skills.load()
    orchestrator = AgentOrchestrator(api_key=cfg["api_key"], base_url=cfg.get("base_url"), model=cfg["model"],
                                     skill_manager=skills, policy=AnswerPolicy())
    orchestrator.reconfigure_llm(create_llm_client(cfg["api_key"], cfg.get("base_url") or None,
                                                   provider=cfg["provider"]), cfg["model"])
    rows = []
    for turn_id, events in by_turn.items():
        try:
            rebuilt = rebuild_request(events)
        except ReplayError as ex:
            rows.append({"turn_id": turn_id, "skipped": str(ex)})
            continue
        recorded = next((e.to_dict()["body"] or {} for e in events if e.type == "turn_completed"), {})
        result = await orchestrator.run(rebuilt.request)
        rows.append({"turn_id": turn_id, "reproduced": rebuilt.verified, "notes": rebuilt.notes,
                     "recorded": recorded.get("response", ""), "replayed": result.response,
                     "agent": result.agent_type.value, "handoff": result.handoff})
    return {"plan": plan, "model": cfg["model"], "turns": rows}


async def run(args: argparse.Namespace, store) -> int:
    """命令本体。store 是本实例的事件存储（PgEventStore），测试里换成进程内的。"""
    by_turn = await select_turns(args, store)
    if args.mode == "route":
        report = _route(by_turn)
    elif args.mode == "guard":
        report = _guard(by_turn)
    elif args.mode == "export":
        report = _export(by_turn, args.category)
        if args.out:
            pathlib.Path(args.out).write_text(
                json.dumps({"cases": report["cases"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report = {"written": args.out, "cases": len(report["cases"]), "skipped": report["skipped"]}
    else:
        report = await _answer(by_turn, args.yes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


async def _main(argv: list[str]) -> int:
    from app.events.store import PgEventStore
    from app.storage.pg import Database

    args = parse(argv)
    dsn = args.dsn or os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        print("需要 POSTGRES_DSN（或 --dsn）：事件日志只存在 Postgres 里", file=sys.stderr)
        return 2
    db = Database(dsn)
    await db.connect_pool()
    try:
        return await run(args, PgEventStore(db, args.instance))
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
