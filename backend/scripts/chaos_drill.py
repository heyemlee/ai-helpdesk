"""
故障演练：一边提交 run，一边硬杀 worker、重启 Redis、让模型一阵子全回 429，跑完逐条对账。

演练的问题只有一个：**出了故障，run 会不会丢、会不会重复。** 所以不看日志、不看面板，
直接查库对账，每一条不变量都按 run 逐个核：

1. 每个提交的 run 都到了终态（没有一个卡在 queued / running）；
2. 每个会话最多一张工单；判了转人工的 run，工单号就是结果里报的那一张；
3. 审计：每个会话客户原话一条，助手回答最多一条，而且就是 run 的最终回答；
4. 工作记忆（Redis）：每个答完的会话正好两条，内容和最终回答一致；
5. 客户端看到的：从头读帧流（按 attempt 过滤之后）拿到的终结帧和库里的结果一致；
   演练中途断线、带 Last-Event-ID 续流的那几个客户端，拼出来的回答也一致。

再报几个数：被打断后由别的 worker 接手的 run 有多少、从被打断到跑完用了多久、排队和
总耗时的分位数、进了死信的有几个。

两种"环境"：

- `--driver compose`：deploy/drill/docker-compose.yml 那一套，用 docker 命令杀容器；
- `--driver k8s`：deploy/k8s 那一套（kind），用 kubectl 删 pod（--grace-period=0 --force）。

    docker compose -f deploy/drill/docker-compose.yml up -d --wait
    backend/.venv/bin/python backend/scripts/chaos_drill.py --runs 60 --kills 3 --restart-redis --storm 20

报告写到 --report（Markdown）和 --json。退出码：不变量全过是 0，有一条没过是 1。
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
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

MESSAGES = [
    "补件什么时候能到",
    "柜门关不严怎么办",
    "台面上有一道裂缝，需要怎么处理",
    "安装师傅什么时候上门",
    "报价单里有一项看不懂",
    "我想转人工客服",
    "抽屉滑轨有异响",
    "订单进度怎么样了",
]


# ── 故障的施加方式 ────────────────────────────────────────────────────────────

class ComposeDriver:
    """docker compose 那一套：worker 是 aihd-drill-worker-N 这些容器。"""

    def __init__(self, compose_file: str, project: str = "aihd-drill"):
        self.compose_file = compose_file
        self.project = project

    def _docker(self, *args: str) -> str:
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    def workers(self) -> list[str]:
        out = self._docker("ps", "--filter", f"label=com.docker.compose.project={self.project}",
                           "--filter", "label=com.docker.compose.service=worker",
                           "--format", "{{.Names}}")
        return sorted(name for name in out.splitlines() if name)

    def kill_worker(self, name: str) -> None:
        self._docker("kill", "--signal", "KILL", name)

    def start_worker(self, name: str) -> None:
        self._docker("start", name)

    def restart_redis(self) -> None:
        self._docker("restart", f"{self.project}-redis-1")

    def rollout(self) -> None:
        """发版：worker 收到 SIGTERM，排空（等手上的跑完，到时限的交还），再起新的。"""
        subprocess.run(["docker", "compose", "-f", self.compose_file, "restart", "--timeout", "60", "worker"],
                       check=True, capture_output=True, text=True)


class K8sDriver:
    """kind 那一套：worker 是 deploy/worker-staff 的 pod，删掉之后 Deployment 自己补一个。"""

    def __init__(self, namespace: str, context: str | None = None):
        self.namespace = namespace
        self.context = context

    def _kubectl(self, *args: str) -> str:
        cmd = ["kubectl", "-n", self.namespace, *(["--context", self.context] if self.context else []), *args]
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()

    def workers(self) -> list[str]:
        out = self._kubectl("get", "pods", "-l", "app.kubernetes.io/component=worker-staff",
                            "--field-selector=status.phase=Running", "-o", "name")
        return sorted(line.split("/", 1)[1] for line in out.splitlines() if line)

    def kill_worker(self, name: str) -> None:
        # 不给宽限期：等同于节点掉电、进程被 OOM 杀掉，worker 来不及交还
        self._kubectl("delete", "pod", name, "--grace-period=0", "--force", "--wait=false")

    def start_worker(self, name: str) -> None:
        return None                      # Deployment 自己补

    def restart_redis(self) -> None:
        self._kubectl("delete", "pod", "-l", "app.kubernetes.io/component=redis", "--wait=false")

    def rollout(self) -> None:
        """发版：滚动重启 worker。旧 pod 收到 SIGTERM 排空，新 pod 就绪后才删下一个。"""
        self._kubectl("rollout", "restart", "deploy/worker-staff")


# ── 演练 ──────────────────────────────────────────────────────────────────────

@dataclass
class Submitted:
    run_id: str
    conv_id: str
    user_id: str
    message: str
    submitted_at: float


# 时间线写成英文：它原样进报告，评测记录（docs/evaluation/）都是英文
@dataclass
class Drill:
    args: argparse.Namespace
    driver: Any
    submitted: list[Submitted] = field(default_factory=list)
    rejected: int = 0
    timeline: list[tuple[float, str]] = field(default_factory=list)
    readers: dict[str, dict[str, Any]] = field(default_factory=dict)
    started: float = field(default_factory=time.time)

    def note(self, what: str) -> None:
        offset = time.time() - self.started
        self.timeline.append((offset, what))
        print(f"[{offset:6.1f}s] {what}", flush=True)


async def submit_all(drill: Drill, client: httpx.AsyncClient) -> None:
    tag = uuid.uuid4().hex[:6]
    interval = 1.0 / drill.args.rate
    readers = []
    for i in range(drill.args.runs):
        conv_id = f"drill-{tag}-{i:03d}"
        user_id = f"drill-user-{tag}-{i % 10}"
        # 拼一个唯一柜号：绕开意图的进程内缓存，每一条都走完整的识别
        message = f"{random.choice(MESSAGES)}，柜号 W{3000 + i}-{i % 7 + 1}"
        key = f"drill-{tag}-{i}"
        for _ in range(20):
            resp = await client.post("/runs", json={"message": message, "user_id": user_id, "conv_id": conv_id},
                                     headers={"Idempotency-Key": key})
            if resp.status_code != 429:
                break
            drill.rejected += 1
            await asyncio.sleep(float(resp.headers.get("Retry-After", "2")))
        resp.raise_for_status()
        run_id = resp.json()["run_id"]
        drill.submitted.append(Submitted(run_id, conv_id, user_id, message, time.time()))
        # 一部分客户端模拟断线：读几帧就断，带着 Last-Event-ID 重连，直到读到终结帧
        if random.random() < drill.args.reader_ratio:
            readers.append(asyncio.create_task(flaky_reader(drill, client, run_id)))
        await asyncio.sleep(interval)
    drill.note(f"submitted {len(drill.submitted)} runs ({drill.rejected} submissions got 429 and were retried)")
    await asyncio.gather(*readers, return_exceptions=True)


def _fold(frames: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """按前端同一套规则把帧折成一条回复：attempt > 1 时清掉之前的，result 以它为准。"""
    text, result, attempts = "", None, 0
    for event, data in frames:
        if event == "attempt":
            attempts = max(attempts, data.get("attempt", 0))
            if data.get("attempt", 0) > 1:
                text, result = "", None
        elif event == "delta":
            text += data.get("text", "")
        elif event in ("result", "error"):
            result = (event, data)
    return {"text": text, "terminal": result, "attempts": attempts}


async def read_events(client: httpx.AsyncClient, run_id: str, last_id: str | None = None,
                      stop_after: int | None = None) -> tuple[list[tuple[str, dict[str, Any]]], str | None, bool]:
    """读一个 run 的帧流。stop_after 帧之后主动断开。返回 (帧, 最后一个 id, 读没读到终结帧)。"""
    headers = {"Last-Event-ID": last_id} if last_id else {}
    frames: list[tuple[str, dict[str, Any]]] = []
    async with client.stream("GET", f"/runs/{run_id}/events", headers=headers, timeout=None) as resp:
        resp.raise_for_status()
        block: dict[str, str] = {}
        async for line in resp.aiter_lines():
            if line.startswith(("id: ", "event: ", "data: ")):
                key, _, value = line.partition(": ")
                block[key] = value
                continue
            if line == "" and "event" in block:
                data = json.loads(block.get("data", "{}"))
                frames.append((block["event"], data))
                last_id = block.get("id", last_id)
                if block["event"] in ("result", "error"):
                    return frames, last_id, True
                block = {}
                if stop_after is not None and len(frames) >= stop_after:
                    return frames, last_id, False
            elif line == "":
                block = {}
    return frames, last_id, False


async def flaky_reader(drill: Drill, client: httpx.AsyncClient, run_id: str) -> None:
    frames: list[tuple[str, dict[str, Any]]] = []
    last_id, reconnects = None, 0
    for _ in range(200):
        try:
            got, last_id, done = await read_events(client, run_id, last_id, stop_after=random.randint(2, 5))
        except (httpx.HTTPError, OSError):
            done, got = False, []
            await asyncio.sleep(1.0)
        frames.extend(got)
        if done:
            break
        reconnects += 1
    drill.readers[run_id] = {**_fold(frames), "reconnects": reconnects}


async def chaos(drill: Drill, mock: httpx.AsyncClient) -> None:
    args = drill.args
    events: list[tuple[float, str]] = [(args.first_kill + n * args.kill_every, "kill") for n in range(args.kills)]
    if args.restart_redis:
        events.append((args.first_kill + args.kill_every * 0.5, "redis"))
    if args.storm:
        events.append((args.first_kill + args.kill_every * 1.5, "storm"))
    if args.rollout:
        events.append((args.first_kill + args.kill_every * max(args.kills, 1) + 2, "rollout"))
    revive: list[asyncio.Task] = []
    for at, kind in sorted(events):
        await asyncio.sleep(max(0.0, drill.started + at - time.time()))
        if kind == "kill":
            workers = drill.driver.workers()
            if not workers:
                drill.note("no running worker to kill, skipped")
                continue
            victim = random.choice(workers)
            drill.driver.kill_worker(victim)
            drill.note(f"SIGKILL worker {victim}")

            async def bring_back(name: str = victim) -> None:
                await asyncio.sleep(args.down_for)
                drill.driver.start_worker(name)
                drill.note(f"worker {name} back")

            revive.append(asyncio.create_task(bring_back()))
        elif kind == "redis":
            drill.driver.restart_redis()
            drill.note("Redis restarted (AOF on)")
        elif kind == "storm":
            await mock.post("/admin/config", json={"storm_s": args.storm})
            drill.note(f"model answers 429 to everything for {args.storm:g} s")
        elif kind == "rollout":
            drill.note("restarting the workers as a deploy would (SIGTERM, drain)")
            await asyncio.to_thread(drill.driver.rollout)
            drill.note("restart command returned")
    await asyncio.gather(*revive)


async def wait_terminal(drill: Drill, pg: asyncpg.Pool, timeout_s: float) -> list[asyncpg.Record]:
    ids = [s.run_id for s in drill.submitted]
    deadline = time.time() + timeout_s
    while True:
        rows = await pg.fetch("SELECT status, count(*) AS n FROM agent_runs WHERE run_id = ANY($1) GROUP BY status", ids)
        counts = {r["status"]: r["n"] for r in rows}
        open_runs = counts.get("queued", 0) + counts.get("running", 0)
        if open_runs == 0 or time.time() > deadline:
            drill.note(f"terminal: {counts}")
            return await pg.fetch("SELECT * FROM agent_runs WHERE run_id = ANY($1)", ids)
        await asyncio.sleep(2.0)


async def _retry(call, attempts: int = 30):
    """对账时连不上（Redis 刚重启完、k8s 的 port-forward 正在重连）：等一等再试，别把它算成不变量没过。"""
    for n in range(attempts):
        try:
            return await call()
        except (OSError, httpx.HTTPError, RedisConnectionError):
            if n == attempts - 1:
                raise
            await asyncio.sleep(1.0)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


async def verify(drill: Drill, pg: asyncpg.Pool, redis: aioredis.Redis, client: httpx.AsyncClient,
                 rows: list[asyncpg.Record]) -> dict[str, Any]:
    by_run = {r["run_id"]: r for r in rows}
    convs = [s.conv_id for s in drill.submitted]
    tickets = await pg.fetch("SELECT id, conv_id FROM tickets WHERE conv_id = ANY($1)", convs)
    audit = await pg.fetch("SELECT conv_id, role, content FROM conversation_log WHERE conv_id = ANY($1) ORDER BY seq",
                           convs)
    tickets_by_conv: dict[str, list[str]] = {}
    for t in tickets:
        tickets_by_conv.setdefault(t["conv_id"], []).append(t["id"])
    audit_by_conv: dict[str, list[tuple[str, str]]] = {}
    for a in audit:
        audit_by_conv.setdefault(a["conv_id"], []).append((a["role"], a["content"]))

    failures: dict[str, list[str]] = {k: [] for k in
                                      ("terminal", "tickets", "audit", "memory", "stream", "reconnect")}
    for s in drill.submitted:
        run = by_run.get(s.run_id)
        if run is None or run["status"] in ("queued", "running"):
            failures["terminal"].append(s.run_id)
            continue
        result = json.loads(run["result"]) if isinstance(run["result"], str) else run["result"]
        answer = (result or {}).get("response")

        own_tickets = tickets_by_conv.get(s.conv_id, [])
        expected_ticket = (result or {}).get("ticket_id")
        if len(own_tickets) > 1 or (expected_ticket and own_tickets != [expected_ticket]):
            failures["tickets"].append(f"{s.run_id}: {own_tickets} vs {expected_ticket}")

        rows_ = audit_by_conv.get(s.conv_id, [])
        users = [c for role, c in rows_ if role == "user"]
        assistants = [c for role, c in rows_ if role == "assistant"]
        if users != [s.message] or len(assistants) > 1 or (answer is not None and run["status"] == "succeeded"
                                                            and assistants != [answer]):
            failures["audit"].append(f"{s.run_id}: user×{len(users)} assistant×{len(assistants)}")

        if run["status"] == "succeeded" and not drill.args.flush_redis:
            stored = await _retry(lambda s=s: redis.lrange(f"wm:{s.user_id}:{s.conv_id}", 0, -1))
            contents = [json.loads(item).get("content") for item in stored]
            if contents != [answer, s.message]:
                failures["memory"].append(f"{s.run_id}: {len(contents)} entries")

        frames, _, done = await _retry(lambda s=s: read_events(client, s.run_id))
        folded = _fold(frames)
        terminal = folded["terminal"]
        if not done or terminal is None or (run["status"] == "succeeded" and terminal[1].get("response") != answer):
            failures["stream"].append(s.run_id)
        reader = drill.readers.get(s.run_id)
        if reader is not None:
            seen = reader["terminal"]
            if seen is None or (run["status"] == "succeeded" and seen[1].get("response") != answer):
                failures["reconnect"].append(s.run_id)

    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    attempts = [r["attempt"] for r in rows]
    interrupted = [r for r in rows if r["attempt"] > 1]
    history_outcomes: dict[str, int] = {}
    for r in rows:
        for item in json.loads(r["history"]) if isinstance(r["history"], str) else r["history"]:
            key = item.get("outcome") or "lost"
            history_outcomes[key] = history_outcomes.get(key, 0) + 1
    resumed = sum(1 for r in rows if r["status"] == "succeeded"
                  and (json.loads(r["result"]) if isinstance(r["result"], str) else r["result"] or {})
                  .get("run", {}).get("resumed_from"))
    queue_wait = [r["started_at"] - r["created_at"] for r in rows if r["started_at"]]
    total = [r["finished_at"] - r["created_at"] for r in rows if r["finished_at"]]
    recovered = [r["finished_at"] - r["created_at"] for r in interrupted if r["finished_at"]]
    unavailable = sum(1 for r in rows if r["status"] == "succeeded"
                      and (json.loads(r["result"]) if isinstance(r["result"], str) else r["result"] or {})
                      .get("service_unavailable"))
    return {
        "submitted": len(drill.submitted),
        "rejected_then_retried": drill.rejected,
        "statuses": statuses,
        "attempts": {str(n): attempts.count(n) for n in sorted(set(attempts))},
        "attempt_outcomes": history_outcomes,
        "interrupted_runs": len(interrupted),
        "resumed_from_commit_point": resumed,
        "answered_as_unavailable": unavailable,
        "tickets": len(tickets),
        "readers": len(drill.readers),
        "reader_reconnects": sum(r["reconnects"] for r in drill.readers.values()),
        "queue_wait_s": {"p50": _pct(queue_wait, 0.5), "p95": _pct(queue_wait, 0.95), "max": _pct(queue_wait, 1.0)},
        "total_s": {"p50": _pct(total, 0.5), "p95": _pct(total, 0.95), "max": _pct(total, 1.0)},
        "interrupted_total_s": {"p50": _pct(recovered, 0.5), "max": _pct(recovered, 1.0)},
        "failures": {k: v for k, v in failures.items()},
        "passed": all(not v for v in failures.values()),
    }


def report_markdown(drill: Drill, summary: dict[str, Any], mock_stats: dict[str, Any]) -> str:
    a = drill.args

    def secs(value):
        return f"{value:.1f}" if isinstance(value, int | float) else "—"

    checks = [
        ("Every run reached a terminal state", "terminal"),
        ("At most one ticket per conversation, and it is the one the result names", "tickets"),
        ("Audit: one customer line, at most one answer, equal to the final answer", "audit"),
        ("Working memory: exactly the two lines of the final answer", "memory"),
        ("A fresh read of the frame stream ends with the stored result", "stream"),
        ("Clients that dropped and resumed with Last-Event-ID saw the final answer", "reconnect"),
    ]
    faults = [
        f"{a.kills} worker SIGKILLs every {a.kill_every:g} s (each back after {a.down_for:g} s)" if a.kills else "",
        "Redis restarted once" if a.restart_redis else "",
        f"a {a.storm:g} s storm of 429s from the model" if a.storm else "",
        "the workers restarted as a deploy would (SIGTERM, drain)" if a.rollout else "",
        "Redis flushed (data loss) once" if a.flush_redis else "",
    ]
    lines = [
        f"### Drill {time.strftime('%Y-%m-%d %H:%M', time.localtime(drill.started))} ({a.driver})",
        "",
        f"{summary['submitted']} runs at {a.rate:g}/s; " + ("; ".join(f for f in faults if f) or "no faults injected") + ".",
        "",
        "| Invariant | Result |",
        "| --- | --- |",
    ]
    for label, key in checks:
        bad = summary["failures"][key]
        if key == "memory" and a.flush_redis:
            lines.append(f"| {label} | not checked (Redis was flushed) |")
            continue
        lines.append(f"| {label} | {'pass' if not bad else f'**{len(bad)} failed**'} |")
    lines += [
        "",
        "| Number | Value |",
        "| --- | --- |",
        f"| Terminal states | {', '.join(f'{k} {v}' for k, v in sorted(summary['statuses'].items()))} |",
        f"| Runs taken over after a worker was lost or a retry | {summary['interrupted_runs']} "
        f"(resumed from the commit point: {summary['resumed_from_commit_point']}) |",
        f"| Attempts per run | {', '.join(f'{k}: {v}' for k, v in summary['attempts'].items())} |",
        f"| Attempt outcomes | {', '.join(f'{k} {v}' for k, v in sorted(summary['attempt_outcomes'].items()))} |",
        f"| Answered \"model unavailable\" (no ticket) | {summary['answered_as_unavailable']} |",
        f"| Tickets | {summary['tickets']} |",
        f"| Queue wait p50 / p95 / max (s) | {secs(summary['queue_wait_s']['p50'])} / "
        f"{secs(summary['queue_wait_s']['p95'])} / {secs(summary['queue_wait_s']['max'])} |",
        f"| Submit → done p50 / p95 / max (s) | {secs(summary['total_s']['p50'])} / "
        f"{secs(summary['total_s']['p95'])} / {secs(summary['total_s']['max'])} |",
        f"| Interrupted runs, submit → done p50 / max (s) | {secs(summary['interrupted_total_s']['p50'])} / "
        f"{secs(summary['interrupted_total_s']['max'])} |",
        f"| Clients that dropped on purpose / reconnects | {summary['readers']} / {summary['reader_reconnects']} |",
        f"| Model requests by status | {mock_stats.get('statuses')} |",
        "",
        "Timeline:",
        "",
    ]
    lines += [f"- {offset:5.1f} s: {what}" for offset, what in drill.timeline]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description="故障演练：提交 run 的同时杀 worker、重启 Redis、制造 429，跑完逐条对账")
    parser.add_argument("--api", default="http://127.0.0.1:28080")
    parser.add_argument("--mock", default="http://127.0.0.1:29000")
    parser.add_argument("--pg", default="postgresql://drill:drill@127.0.0.1:25432/drill")
    parser.add_argument("--redis", default="redis://127.0.0.1:26379/0")
    parser.add_argument("--driver", choices=("compose", "k8s"), default="compose")
    parser.add_argument("--compose-file", default="deploy/drill/docker-compose.yml")
    parser.add_argument("--namespace", default="aihd")
    parser.add_argument("--context", default=None)
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--rate", type=float, default=2.0, help="每秒提交几个")
    parser.add_argument("--kills", type=int, default=3)
    parser.add_argument("--first-kill", type=float, default=6.0)
    parser.add_argument("--kill-every", type=float, default=8.0)
    parser.add_argument("--down-for", type=float, default=10.0, help="杀掉的 worker 多久之后重新起来")
    parser.add_argument("--restart-redis", action="store_true")
    parser.add_argument("--flush-redis", action="store_true", help="中途 FLUSHALL：Redis 整个丢了")
    parser.add_argument("--storm", type=float, default=0.0, help="模型连续回 429 多少秒")
    parser.add_argument("--rollout", action="store_true", help="中途滚动重启 worker（发版）：SIGTERM、排空")
    parser.add_argument("--slow", action="store_true", help="把模型调慢（作答 3 秒起），一轮更容易被拦腰杀掉")
    parser.add_argument("--reader-ratio", type=float, default=0.25, help="多大比例的 run 由一个会断线的客户端跟着读")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--report", default="")
    parser.add_argument("--json", default="")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    driver = ComposeDriver(args.compose_file) if args.driver == "compose" else K8sDriver(args.namespace, args.context)
    drill = Drill(args=args, driver=driver)
    pg = await asyncpg.create_pool(args.pg, min_size=1, max_size=4)
    redis = aioredis.from_url(args.redis, decode_responses=True)
    async with httpx.AsyncClient(base_url=args.api, timeout=30) as client, \
            httpx.AsyncClient(base_url=args.mock, timeout=10) as mock:
        (await client.get("/readyz")).raise_for_status()
        await mock.post("/admin/reset")
        if args.slow:
            await mock.post("/admin/config", json={"latency_ms": 3000, "structured_latency_ms": 800, "chunk_ms": 150})
        drill.note(f"start: workers {driver.workers()}")
        flush = None
        if args.flush_redis:
            async def do_flush():
                await asyncio.sleep(args.first_kill + args.kill_every)
                await redis.flushall()
                drill.note("FLUSHALL: dispatch queue, frame streams and working memory gone")
            flush = asyncio.create_task(do_flush())
        await asyncio.gather(submit_all(drill, client), chaos(drill, mock), *([flush] if flush else []))
        rows = await wait_terminal(drill, pg, args.timeout)
        summary = await verify(drill, pg, redis, client, rows)
        mock_stats = (await mock.get("/admin/stats")).json()
        if args.slow:
            await mock.post("/admin/config", json={"latency_ms": 800, "structured_latency_ms": 400, "chunk_ms": 60})
    await pg.close()
    await redis.aclose()

    summary["timeline"] = drill.timeline
    summary["mock"] = mock_stats
    print(json.dumps({k: v for k, v in summary.items() if k != "timeline"}, ensure_ascii=False, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
    if args.report:
        with open(args.report, "a", encoding="utf-8") as fh:
            fh.write(report_markdown(drill, summary, mock_stats) + "\n")
    print("不变量全部通过" if summary["passed"] else "有不变量没过，见 failures", file=sys.stderr)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(asyncio.run(main()))
