#!/usr/bin/env python3
"""
事件日志的运维脚本。在本机跑，连 POSTGRES_DSN（或 --dsn）那个库，只看、只删指定实例的行。

  python scripts/turn_events.py show   --instance staff --turn <turn_id> [--raw]
  python scripts/turn_events.py check  --instance staff [--hours 24]
  python scripts/turn_events.py forget --instance customer --conv <conv_id> [--conv …] [--yes]
  python scripts/turn_events.py forget --instance staff --user <user_id> [--yes]

- show：打印这一轮的经过（和 GET /turns/{turn_id} 一样的投影），--raw 打原始事件。客户实例的
  读接口是关着的，查它的轮次用这个；
- check：列出一段时间里没有终结事件、seq 有缺口、有没打用途的模型调用的轮次。上线观察期
  每天看一次，这几项都应该是 0。有问题时退出码是 2；
- forget：删除请求。不带 --yes 只打印要删什么。按用户找会话有两处：turn_started 的原文，和
  审计日志（conversation_log）里记在这个人名下的会话——原文 30 天就清空，更早的轮次靠审计
  才找得到。审计日志本身删不删是另一个决定，这里只读不删。

Docker 部署时库只对容器网络开放，在应用容器里跑：
  docker exec ai-helpdesk-app python scripts/turn_events.py check --instance staff
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from app.events.ops import check_turns, forget  # noqa: E402
from app.events.projection import project_turn  # noqa: E402


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="事件日志的运维脚本：show / check / forget")
    parser.add_argument("command", choices=["show", "check", "forget"])
    parser.add_argument("--instance", required=True, choices=["staff", "customer"])
    parser.add_argument("--dsn", default="", help="默认读 POSTGRES_DSN")
    parser.add_argument("--turn", default="", help="show：哪一轮")
    parser.add_argument("--raw", action="store_true", help="show：打印原始事件，不做投影")
    parser.add_argument("--hours", type=float, default=24.0, help="check：看最近多少小时开始的轮次")
    parser.add_argument("--conv", action="append", default=[], help="forget：要删的会话，可以给多个")
    parser.add_argument("--user", default="", help="forget：按用户找出他的会话一起删")
    parser.add_argument("--yes", action="store_true", help="forget：真的删；不给只打印计划")
    args = parser.parse_args(argv)
    if args.command == "show" and not args.turn:
        parser.error("show 要给 --turn")
    if args.command == "forget" and not (args.conv or args.user):
        parser.error("forget 要给 --conv 或 --user")
    return args


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def run(args: argparse.Namespace, store, audit=None) -> int:
    """
    命令本体。store 是本实例的事件存储（PgEventStore），audit 是审计日志（PgConversationLog），
    测试里换成进程内的和 SQLite 的。
    """
    if args.command == "show":
        events = await store.events_of(args.turn)
        if not events:
            print(f"没有这一轮：不存在、不是 {args.instance} 实例的，或已超出保留期", file=sys.stderr)
            return 1
        _print([e.to_dict() for e in events] if args.raw else project_turn(events))
        return 0
    if args.command == "check":
        report = await check_turns(store, since=time.time() - args.hours * 3600)
        _print(report)
        return 2 if report["no_terminal"] or report["seq_gaps"] or report["unknown_purpose"] else 0
    result = await forget(store, conv_ids=args.conv, user_id=args.user, audit=audit, dry_run=not args.yes)
    _print(result)
    if result["dry_run"] and result["conversations"]:
        print("这是计划：加 --yes 才真的删。", file=sys.stderr)
    return 0


async def _main(argv: list[str]) -> int:
    from app.events.store import PgEventStore
    from app.storage.conversation_log import PgConversationLog
    from app.storage.pg import Database

    args = parse(argv)
    dsn = args.dsn or os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        print("需要 POSTGRES_DSN（或 --dsn）：事件日志只存在 Postgres 里", file=sys.stderr)
        return 2
    db = Database(dsn)
    await db.connect_pool()
    try:
        return await run(args, PgEventStore(db, args.instance), PgConversationLog(db))
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
