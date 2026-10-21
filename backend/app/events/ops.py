"""
事件日志的运维检查（scripts/turn_events.py 用，也给测试用）。

- check：一段时间里的每一轮，有没有终结事件、seq 有没有缺口、有没有没打用途的模型调用；
- forget：删除请求。按会话删掉本实例的事件；按用户时先找出他的会话：turn_started 的原文里
  有的，加上审计日志（conversation_log）里记在他名下的。审计日志本身删不删是另一个决定，这里不碰。
"""
from __future__ import annotations

import json
from typing import Any

from app.events.types import TERMINAL_TYPES, own_seq


async def check_turns(store, since: float, until: float | None = None, limit: int = 5000) -> dict[str, Any]:
    """
    上线后观察期每天看一次：没有终结事件、seq 有缺口的轮次都应该是 0。

    abandoned 单列、不算问题：跑那一轮的进程没了（排队模式下 worker 被杀），接手的那一遍或者
    死信替它补了 turn_abandoned。它们缺的那一截是真丢了，但去向是清楚的——run 换了一遍执行。
    缺口只看进程自己编号的那一段（types.RESERVED_SEQ 以下）。
    """
    turn_ids = await store.turn_ids_between(since, until, limit)
    by_turn = await store.events_of_turns(turn_ids)
    report: dict[str, Any] = {"turns": len(turn_ids), "complete": 0, "no_terminal": [], "seq_gaps": [],
                              "unknown_purpose": [], "abandoned": [], "events": 0}
    for turn_id in turn_ids:
        events = by_turn.get(turn_id, [])
        report["events"] += len(events)
        own = [e for e in events if own_seq(e.seq)]
        seqs = [e.seq for e in own]
        terminal = any(e.type in TERMINAL_TYPES for e in own)
        gap = seqs != list(range(len(seqs)))
        if not terminal and any(e.type == "turn_abandoned" for e in events):
            report["abandoned"].append(turn_id)
        elif not terminal:
            report["no_terminal"].append(turn_id)
        if gap:
            report["seq_gaps"].append(turn_id)
        # 从库里读回来的 jsonb 文本是 Postgres 重新排过版的：解开再看，不按字面匹配
        if any(e.type == "model_call" and json.loads(e.data).get("purpose") == "unknown" for e in events):
            report["unknown_purpose"].append(turn_id)
        if terminal and not gap:
            report["complete"] += 1
    return report


async def forget(store, *, conv_ids: list[str] | None = None, user_id: str = "",
                 audit=None, dry_run: bool = True) -> dict[str, Any]:
    """
    删除请求。dry_run 时只说要删什么。

    按用户找会话有两处：turn_started 的原文，和审计日志（audit，`conversation_log`）里这个人的
    会话。原文 30 天就清空，只靠它找，更早的轮次就找不到了；而审计表里"用户 → 会话"的对应一直
    都在，那些轮次剩下的结构行仍然能关联回这个人。审计只用来找，不删。found_by 说每个会话是
    从哪儿找到的：given（命令行给的）、turn_started、conversation_log。
    """
    found_by: dict[str, list[str]] = {}

    def found(conv_id: str, source: str) -> None:
        found_by.setdefault(conv_id, [])
        if source not in found_by[conv_id]:
            found_by[conv_id].append(source)

    for conv_id in conv_ids or []:
        found(conv_id, "given")
    if user_id:
        for conv_id in await store.conv_ids_of_user(user_id):
            found(conv_id, "turn_started")
        if audit is not None:
            for conv_id in await audit.conv_ids_of_user(user_id, store.instance):
                found(conv_id, "conversation_log")
    targets = list(found_by)
    deleted = {}
    for conv_id in targets:
        deleted[conv_id] = 0 if dry_run else await store.delete_conversation(conv_id)
    return {"instance": store.instance, "conversations": targets, "found_by": found_by,
            "dry_run": dry_run, "deleted": deleted}
