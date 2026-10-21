"""
断线和重启的对照演练：同一组问题、同样的故障，分别打 inline 模式和排队模式，量出各丢了多少轮对话。

故障两种，都是控制台和客户实际会碰到的：

- 客户端断线：一部分客户端读到第几帧就断开（刷新页面、网络抖一下），然后照控制台的做法想办法接着读；
- API 重启：提交到一半时重启一次 API，像发版一样给 10 秒宽限（docker restart -t 10：先 SIGTERM，
  10 秒后 SIGKILL）。正在读的连接这时候一起断，宽限期里跑得完的那几轮能跑完。

断了以后客户端怎么恢复，照控制台（frontend/src/hooks/useChatStream.ts）的做法：

- 排队模式：拿第一帧里的 run_id，带 Last-Event-ID 续读；API 还没起来就每秒重试，最多 2 分钟。
  第一帧还没到就断了的，带同一个 Idempotency-Key 重新提交（拿回的是同一个 run，不会多跑一遍）；
- inline 模式：没有能续的东西。这一轮就丢了——要答案只能重新问一遍，那是新的一轮、新的模型调用；
- 两种模式都一样：连接 45 秒没有任何字节就当断了（服务端每 15 秒发一条心跳）；API 连不上时提交
  每秒重试，最多 2 分钟——提交没成功不算丢，只算晚。

一轮"丢了"= 客户端最后没拿到这一轮的回答（result 帧）。服务端那一侧另外从事件日志对一遍：每个
会话的轮次是 turn_completed（答完了）还是 turn_aborted（跑到一半放弃了）。

    docker compose -f deploy/drill/docker-compose.yml --profile compare up -d --wait
    backend/.venv/bin/python backend/scripts/disconnect_drill.py --mode inline --api http://127.0.0.1:28081 --service api-inline
    backend/.venv/bin/python backend/scripts/disconnect_drill.py --mode queued --api http://127.0.0.1:28080 --service api

报告写到 --report（Markdown）和 --json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import asyncpg
import httpx
from chaos_drill import MESSAGES

IDLE_S = 45.0          # 和控制台的 STREAM_IDLE_MS 一样
RECOVER_S = 120.0


class Retry(Exception):
    """API 还没准备好（502 / 503 / 504，或者连不上）：等一秒再来。"""


@dataclass
class Turn:
    index: int
    conv_id: str
    message: str
    key: str
    drop_after: int | None
    run_id: str = ""
    last_id: str | None = None
    frames: int = 0
    cuts: list[str] = field(default_factory=list)
    answer: str | None = None
    error: str | None = None
    submit_retries: int = 0
    submitted_at: float = 0.0
    first_cut_at: float | None = None
    done_at: float | None = None

    @property
    def delivered(self) -> bool:
        return self.answer is not None


async def read_stream(resp: httpx.Response, turn: Turn, drop: bool) -> str:
    """读一条 SSE 流。返回 done（读到 result / error）、dropped（按计划主动断开）或 ended（服务端关了）。"""
    block: dict[str, str] = {}
    async for line in resp.aiter_lines():
        if line.startswith(":"):
            continue                                   # 心跳
        if line.startswith(("id: ", "event: ", "data: ")):
            key, _, value = line.partition(": ")
            block[key] = value
            continue
        if line != "" or "event" not in block:
            block = {}
            continue
        event, data = block["event"], json.loads(block.get("data") or "{}")
        turn.last_id = block.get("id", turn.last_id)
        turn.frames += 1
        block = {}
        if event == "run":
            turn.run_id = data.get("run_id", "")
        elif event == "result":
            turn.answer = data.get("response") or ""
            return "done"
        elif event == "error":
            turn.error = str(data.get("detail") or "error")
            return "done"
        if drop and turn.drop_after is not None and not turn.cuts and turn.frames >= turn.drop_after:
            return "dropped"
    return "ended"


def _cut_reason(ex: BaseException) -> str:
    return "idle" if isinstance(ex, httpx.ReadTimeout) else "connection"


async def submit(client: httpx.AsyncClient, turn: Turn, drop: bool) -> str:
    """提交并读这一轮。API 连不上就每秒重试（不算丢）；连上之后流断了返回断的原因。"""
    body = {"message": turn.message, "user_id": f"cut-user-{turn.index % 10}", "conv_id": turn.conv_id}
    deadline = time.time() + RECOVER_S
    while True:
        frames_before = turn.frames
        try:
            async with client.stream("POST", "/chat/stream", json=body,
                                     headers={"Idempotency-Key": turn.key}) as resp:
                if resp.status_code in (502, 503, 504):
                    raise Retry()
                resp.raise_for_status()
                return await read_stream(resp, turn, drop)
        except (httpx.ConnectError, Retry):
            if turn.frames > frames_before:
                return "connection"
            if time.time() > deadline:
                turn.error = "API 2 分钟没起来，没提交成"
                return "done"
            turn.submit_retries += 1
            await asyncio.sleep(1.0)
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.WriteError) as ex:
            return _cut_reason(ex)


async def resume(client: httpx.AsyncClient, turn: Turn) -> None:
    """排队模式：断了之后按 run_id 续读，直到拿到 result 或者 2 分钟过去。"""
    deadline = time.time() + RECOVER_S
    while time.time() < deadline:
        if not turn.run_id:
            # 第一帧还没到就断了：带同一个 Idempotency-Key 重新提交，拿回的是同一个 run
            outcome = await submit(client, turn, drop=False)
        else:
            headers = {"Last-Event-ID": turn.last_id} if turn.last_id else {}
            try:
                async with client.stream("GET", f"/runs/{turn.run_id}/events", headers=headers) as resp:
                    if resp.status_code in (502, 503, 504):
                        raise Retry()
                    resp.raise_for_status()
                    outcome = await read_stream(resp, turn, drop=False)
            except (httpx.ConnectError, Retry):
                await asyncio.sleep(1.0)
                continue
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.WriteError) as ex:
                outcome = _cut_reason(ex)
        if outcome == "done":
            return
        turn.cuts.append(outcome)
        await asyncio.sleep(1.0)
    turn.error = turn.error or "2 分钟内没续上"


async def run_turn(client: httpx.AsyncClient, turn: Turn, args, started: float, notes) -> None:
    await asyncio.sleep(max(0.0, started + turn.index / args.rate - time.time()))
    turn.submitted_at = time.time()
    outcome = await submit(client, turn, drop=turn.drop_after is not None)
    if outcome != "done":
        turn.cuts.append(outcome)
        turn.first_cut_at = time.time()
        if args.mode == "queued":
            await resume(client, turn)
    if turn.delivered:
        turn.done_at = time.time()


async def restart_api(args, started: float, note) -> None:
    await asyncio.sleep(max(0.0, started + args.restart_at - time.time()))
    note(f"restarting {args.service} (docker restart -t {args.grace:g}: SIGTERM, {args.grace:g} s grace, then SIGKILL)")
    await asyncio.to_thread(subprocess.run, ["docker", "restart", "-t", f"{args.grace:g}",
                                             f"{args.project}-{args.service}-1"],
                            check=True, capture_output=True)
    note(f"{args.service} restart command returned")


async def server_side(pg: asyncpg.Pool, turns: list[Turn]) -> dict[str, int]:
    """事件日志里每一轮怎么收的场：turn_completed / turn_aborted / turn_failed / 没有记录。"""
    rows = await pg.fetch(
        "SELECT conv_id, turn_id, array_agg(type) AS types FROM turn_events"
        " WHERE conv_id = ANY($1) AND type IN ('turn_completed', 'turn_aborted', 'turn_failed', 'turn_abandoned')"
        " GROUP BY conv_id, turn_id", [t.conv_id for t in turns])
    out: dict[str, int] = {}
    for row in rows:
        kinds = set(row["types"])
        kind = next((k for k in ("turn_completed", "turn_failed", "turn_aborted", "turn_abandoned") if k in kinds),
                    "unknown")
        out[kind] = out.get(kind, 0) + 1
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarize(args, turns: list[Turn], server: dict[str, int], mock: dict[str, Any], timeline) -> dict[str, Any]:
    dropped = [t for t in turns if t.cuts and t.cuts[0] == "dropped"]
    restarted = [t for t in turns if t.cuts and t.cuts[0] != "dropped"]
    clean = [t for t in turns if not t.cuts]
    recovery = [t.done_at - t.first_cut_at for t in turns if t.cuts and t.done_at and t.first_cut_at]
    delivered = sum(1 for t in turns if t.delivered)
    requests = sum((mock.get("statuses") or {}).values())
    return {
        "mode": args.mode,
        "turns": len(turns),
        "cut_by_client": len(dropped),
        "cut_by_client_delivered": sum(1 for t in dropped if t.delivered),
        "cut_by_restart": len(restarted),
        "cut_by_restart_delivered": sum(1 for t in restarted if t.delivered),
        "not_cut": len(clean),
        "not_cut_delivered": sum(1 for t in clean if t.delivered),
        "delivered": delivered,
        "lost": len(turns) - delivered,
        "errors": sum(1 for t in turns if t.error),
        "submit_retries": sum(t.submit_retries for t in turns),
        "recovery_s": {"p50": _pct(recovery, 0.5), "max": _pct(recovery, 1.0)},
        "server": server,
        "model_requests": requests,
        "model_requests_per_delivered": round(requests / delivered, 2) if delivered else None,
        "timeline": timeline,
        "turn_detail": [asdict(t) for t in turns],
    }


def report(summary: dict[str, Any], args) -> str:
    s = summary

    def secs(v):
        return f"{v:.1f}" if isinstance(v, int | float) else "—"

    lines = [
        f"### {args.mode} · {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"{s['turns']} turns at {args.rate:g}/s; {round(args.drop_ratio * 100)}% of clients drop after 2–5 frames; "
        f"{args.service} restarted at {args.restart_at:g} s with {args.grace:g} s grace.",
        "",
        "| | Turns | Got the answer |",
        "| --- | ---: | ---: |",
        f"| Client dropped the stream | {s['cut_by_client']} | {s['cut_by_client_delivered']} |",
        f"| Stream cut by the API restart | {s['cut_by_restart']} | {s['cut_by_restart_delivered']} |",
        f"| Not cut | {s['not_cut']} | {s['not_cut_delivered']} |",
        f"| **All** | **{s['turns']}** | **{s['delivered']}** (lost {s['lost']}) |",
        "",
        f"Server side (event log): {s['server']}. Model requests: {s['model_requests']} "
        f"({s['model_requests_per_delivered']} per delivered answer). Submissions retried while the API was down: "
        f"{s['submit_retries']}. Cut streams recovered after p50 {secs(s['recovery_s']['p50'])} s, "
        f"max {secs(s['recovery_s']['max'])} s.",
        "",
        "Timeline:",
        "",
    ]
    lines += [f"- {offset:5.1f} s: {what}" for offset, what in s["timeline"]]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description="断线和重启的对照演练：inline 和排队模式各丢多少轮对话")
    parser.add_argument("--mode", choices=("inline", "queued"), required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--service", required=True, help="要重启的 compose 服务：api-inline 或 api")
    parser.add_argument("--project", default="aihd-drill")
    parser.add_argument("--mock", default="http://127.0.0.1:29000")
    parser.add_argument("--pg", default="postgresql://drill:drill@127.0.0.1:25432/drill")
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument("--drop-ratio", type=float, default=0.3)
    parser.add_argument("--restart-at", type=float, default=12.0)
    parser.add_argument("--grace", type=float, default=10.0)
    parser.add_argument("--slow", action="store_true", help="把模型调慢（作答 3 秒起），一轮更容易被拦腰断开")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--report", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    tag = uuid.uuid4().hex[:6]
    turns = [Turn(index=i, conv_id=f"cut-{args.mode}-{tag}-{i:03d}",
                  message=f"{rng.choice(MESSAGES)}，柜号 W{5000 + i}-{i % 7 + 1}",
                  key=f"cut-{tag}-{i}",
                  drop_after=rng.randint(2, 5) if rng.random() < args.drop_ratio else None)
             for i in range(args.runs)]
    started = time.time()
    timeline: list[tuple[float, str]] = []

    def note(what: str) -> None:
        offset = time.time() - started
        timeline.append((offset, what))
        print(f"[{offset:6.1f}s] {what}", flush=True)

    timeout = httpx.Timeout(10.0, read=IDLE_S)
    async with httpx.AsyncClient(base_url=args.api, timeout=timeout) as client, \
            httpx.AsyncClient(base_url=args.mock, timeout=10) as mock:
        await mock.post("/admin/reset")
        if args.slow:
            await mock.post("/admin/config", json={"latency_ms": 3000, "structured_latency_ms": 800, "chunk_ms": 150})
        note(f"start: {args.mode} mode against {args.api}")
        await asyncio.gather(*(run_turn(client, t, args, started, note) for t in turns),
                             restart_api(args, started, note))
        note(f"all clients finished: {sum(1 for t in turns if t.delivered)} of {len(turns)} got the answer")
        stats = (await mock.get("/admin/stats")).json()
        if args.slow:
            await mock.post("/admin/config", json={"latency_ms": 800, "structured_latency_ms": 400, "chunk_ms": 60})
    await asyncio.sleep(3.0)                          # 审计和事件日志是后台写的
    pg = await asyncpg.create_pool(args.pg, min_size=1, max_size=2)
    server = await server_side(pg, turns)
    await pg.close()

    summary = summarize(args, turns, server, stats, timeline)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("timeline", "turn_detail")},
                     ensure_ascii=False, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
    if args.report:
        with open(args.report, "a", encoding="utf-8") as fh:
            fh.write(report(summary, args) + "\n")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(asyncio.run(main()))
