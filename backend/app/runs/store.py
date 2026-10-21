"""
run 的存储：Postgres（`agent_runs`，建表在 storage/pg.py）和进程内两种，接口一致。

**每个存储绑定一个实例。** 两个实例共用一张表，每条查询都带 instance：员工那边的 worker
连客户的 run 都读不到，更领不到——队列按实例分开是第一层，这是第二层。

**租约用数据库的钟。** "租约过期没有"是 worker A 写下、worker B 判断的，两台机器的时钟
差几秒，就会出现 A 还在续租、B 已经觉得它过期了。Postgres 里一律用
`clock_timestamp()`，所有判断对"现在"只有一个答案。进程内的存储只有一个进程，用本机钟。

**栅栏。** 续租、写结局、交还都带 (lease_owner, attempt)：run 被别人领走之后 attempt 加了一，
原来那个 worker 再来写，一行都改不到。改到几行就是结论，不用先读后写。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Any

from app.runs.records import QUEUED, RUNNING, RunRecord


class IdempotencyConflict(Exception):
    """同一个 Idempotency-Key 又来了一次，内容却不一样（会话或原话变了）。"""

    def __init__(self, existing: RunRecord):
        super().__init__(existing.run_id)
        self.existing = existing


def lost_attempts(run: RunRecord) -> int:
    """
    之前有几次执行是"失联"的：领走了，却没有正常收场（没有 ended_at）。

    死信看的是这个数，不是 attempt：worker 停机时主动交还的那一次（outcome=released）
    不算失联——不然连着发三次版，一个跑得久的 run 就进了死信。
    """
    return sum(1 for item in run.history[:-1] if "ended_at" not in item)


def same_request(a: RunRecord, b: RunRecord) -> bool:
    """同一个 Idempotency-Key 的两次提交是不是同一件事：会话、用户、原话都一样。"""
    return a.conv_id == b.conv_id and a.message == b.message and a.user_id == b.user_id


# ── 进程内 ────────────────────────────────────────────────────────────────────

class MemoryRunStore:
    """进程内的存储：单测和单进程联调用。语义和 PgRunStore 一致。"""

    backend = "memory"

    def __init__(self, instance: str, clock=time.time):
        self.instance = instance
        self._clock = clock
        self._runs: dict[str, RunRecord] = {}
        self._keys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _copy(self, run: RunRecord) -> RunRecord:
        return replace(run, history=[dict(h) for h in run.history],
                       result=json.loads(json.dumps(run.result)) if run.result is not None else None,
                       checkpoint=(json.loads(json.dumps(run.checkpoint))
                                   if run.checkpoint is not None else None))

    async def create(self, run: RunRecord) -> tuple[RunRecord, bool]:
        async with self._lock:
            existing_id = self._keys.get(run.idempotency_key)
            if existing_id is not None:
                existing = self._runs[existing_id]
                if not same_request(existing, run):
                    raise IdempotencyConflict(self._copy(existing))
                return self._copy(existing), False
            now = self._clock()
            stored = replace(run, instance=self.instance, status=QUEUED, created_at=now,
                             updated_at=now, dispatched_at=now, history=[])
            self._runs[stored.run_id] = stored
            self._keys[stored.idempotency_key] = stored.run_id
            return self._copy(stored), True

    async def get(self, run_id: str) -> RunRecord | None:
        run = self._runs.get(run_id)
        return self._copy(run) if run is not None else None

    async def get_by_key(self, idempotency_key: str) -> RunRecord | None:
        run_id = self._keys.get(idempotency_key)
        return await self.get(run_id) if run_id else None

    async def claim(self, run_id: str, owner: str, lease_s: float) -> RunRecord | None:
        async with self._lock:
            run = self._runs.get(run_id)
            now = self._clock()
            if run is None:
                return None
            if not (run.status == QUEUED or (run.status == RUNNING and run.lease_until < now)):
                return None
            reclaimed = run.status == RUNNING
            run.status, run.attempt = RUNNING, run.attempt + 1
            run.lease_owner, run.lease_until = owner, now + lease_s
            run.started_at = run.started_at if run.started_at is not None else now
            run.updated_at = now
            run.history.append({"attempt": run.attempt, "owner": owner, "started_at": now,
                                "reclaimed": reclaimed})
            return self._copy(run)

    def _fenced(self, run_id: str, owner: str, attempt: int) -> RunRecord | None:
        run = self._runs.get(run_id)
        if run is None or run.status != RUNNING or run.lease_owner != owner or run.attempt != attempt:
            return None
        return run

    async def heartbeat(self, run_id: str, owner: str, attempt: int, lease_s: float) -> bool:
        async with self._lock:
            run = self._fenced(run_id, owner, attempt)
            if run is None:
                return False
            now = self._clock()
            run.lease_until, run.updated_at = now + lease_s, now
            return True

    async def set_turn(self, run_id: str, owner: str, attempt: int, turn_id: str, degrade_level: int) -> bool:
        async with self._lock:
            run = self._fenced(run_id, owner, attempt)
            if run is None:
                return False
            run.turn_id, run.degrade_level, run.updated_at = turn_id, degrade_level, self._clock()
            if run.history:
                run.history[-1]["turn_id"] = turn_id
            return True

    async def checkpoint(self, run_id: str, owner: str, attempt: int, data: dict[str, Any]) -> bool:
        async with self._lock:
            run = self._fenced(run_id, owner, attempt)
            if run is None:
                return False
            run.checkpoint = json.loads(json.dumps(data, ensure_ascii=False))
            run.updated_at = self._clock()
            if run.history:
                run.history[-1]["committed"] = True
            return True

    async def finish(self, run_id: str, owner: str, attempt: int, status: str, *,
                     result: dict[str, Any] | None = None, error: str = "") -> RunRecord | None:
        async with self._lock:
            run = self._fenced(run_id, owner, attempt)
            if run is None:
                return None
            now = self._clock()
            run.status, run.result, run.error = status, result, error
            run.finished_at = run.updated_at = now
            run.lease_owner, run.lease_until = "", 0.0
            if run.history:
                run.history[-1].update(ended_at=now, outcome=status)
            return self._copy(run)

    async def release(self, run_id: str, owner: str, attempt: int) -> bool:
        async with self._lock:
            run = self._fenced(run_id, owner, attempt)
            if run is None:
                return False
            now = self._clock()
            run.status, run.lease_owner, run.lease_until = QUEUED, "", 0.0
            run.updated_at = run.dispatched_at = now
            if run.history:
                run.history[-1].update(ended_at=now, outcome="released")
            return True

    async def due_for_dispatch(self, *, idle_s: float, lease_grace_s: float, limit: int = 100) -> list[str]:
        now = self._clock()
        due = [
            r for r in self._runs.values()
            if r.dispatched_at < now - idle_s and (
                r.status == QUEUED or (r.status == RUNNING and r.lease_until < now - lease_grace_s))
        ]
        return [r.run_id for r in sorted(due, key=lambda r: r.created_at)[:limit]]

    async def mark_dispatched(self, run_ids: list[str]) -> None:
        now = self._clock()
        for run_id in run_ids:
            if run_id in self._runs:
                self._runs[run_id].dispatched_at = now

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for run in self._runs.values():
            out[run.status] = out.get(run.status, 0) + 1
        return out

    async def purge(self, older_than_s: float) -> int:
        cutoff = self._clock() - older_than_s
        gone = [r.run_id for r in self._runs.values()
                if r.finished_at is not None and r.finished_at < cutoff]
        for run_id in gone:
            run = self._runs.pop(run_id)
            self._keys.pop(run.idempotency_key, None)
        return len(gone)

    async def ping(self) -> bool:
        return True


# ── Postgres ──────────────────────────────────────────────────────────────────

_NOW = "extract(epoch from clock_timestamp())::float8"

_COLUMNS = (
    "run_id, instance, idempotency_key, conv_id, user_id, message, request_id, transport, status,"
    " attempt, lease_owner, lease_until, turn_id, degrade_level, checkpoint, result, error, history,"
    " created_at, updated_at, dispatched_at, started_at, finished_at"
)

# 这一次执行收场：history 的最后一条补上结束时间、结局和那一轮的 turn_id
_CLOSE_LAST = (
    "history = CASE WHEN jsonb_array_length(history) = 0 THEN history ELSE"
    " jsonb_set(history, ARRAY[(jsonb_array_length(history) - 1)::text],"
    " (history -> -1) || jsonb_build_object('ended_at', {now}, 'outcome', {outcome}::text)) END"
)


def _record(row: Any) -> RunRecord:
    result = row["result"]
    history = row["history"]
    checkpoint = row["checkpoint"]
    return RunRecord(
        run_id=row["run_id"], instance=row["instance"], idempotency_key=row["idempotency_key"],
        conv_id=row["conv_id"], user_id=row["user_id"], message=row["message"],
        request_id=row["request_id"], transport=row["transport"], status=row["status"],
        attempt=row["attempt"], lease_owner=row["lease_owner"], lease_until=row["lease_until"],
        turn_id=row["turn_id"], degrade_level=row["degrade_level"],
        checkpoint=json.loads(checkpoint) if isinstance(checkpoint, str) else checkpoint,
        result=json.loads(result) if isinstance(result, str) else result,
        error=row["error"],
        history=json.loads(history) if isinstance(history, str) else (history or []),
        created_at=row["created_at"], updated_at=row["updated_at"],
        dispatched_at=row["dispatched_at"], started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class PgRunStore:
    """Postgres 的 `agent_runs`。所有判断在一条 SQL 里做完，改到几行就是结论。"""

    backend = "postgres"

    def __init__(self, db, instance: str):
        self._db = db
        self.instance = instance

    async def create(self, run: RunRecord) -> tuple[RunRecord, bool]:
        row = await self._db.fetchrow(
            f"INSERT INTO agent_runs (run_id, instance, idempotency_key, conv_id, user_id, message,"
            f" request_id, transport, status, created_at, updated_at, dispatched_at)"
            f" VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'queued', {_NOW}, {_NOW}, {_NOW})"
            f" ON CONFLICT (instance, idempotency_key) DO NOTHING RETURNING {_COLUMNS}",
            run.run_id, self.instance, run.idempotency_key, run.conv_id, run.user_id,
            run.message, run.request_id, run.transport,
        )
        if row is not None:
            return _record(row), True
        row = await self._db.fetchrow(
            f"SELECT {_COLUMNS} FROM agent_runs WHERE instance = $1 AND idempotency_key = $2",
            self.instance, run.idempotency_key,
        )
        existing = _record(row)
        if not same_request(existing, run):
            raise IdempotencyConflict(existing)
        return existing, False

    async def get(self, run_id: str) -> RunRecord | None:
        row = await self._db.fetchrow(
            f"SELECT {_COLUMNS} FROM agent_runs WHERE run_id = $1 AND instance = $2",
            run_id, self.instance,
        )
        return _record(row) if row is not None else None

    async def get_by_key(self, idempotency_key: str) -> RunRecord | None:
        row = await self._db.fetchrow(
            f"SELECT {_COLUMNS} FROM agent_runs WHERE instance = $1 AND idempotency_key = $2",
            self.instance, idempotency_key,
        )
        return _record(row) if row is not None else None

    async def claim(self, run_id: str, owner: str, lease_s: float) -> RunRecord | None:
        # SET 里引用的列都是改之前的值：attempt + 1 用的是旧 attempt，reclaimed 看的是旧 status
        row = await self._db.fetchrow(
            f"UPDATE agent_runs SET status = 'running', attempt = attempt + 1, lease_owner = $2,"
            f" lease_until = {_NOW} + $3, started_at = COALESCE(started_at, {_NOW}), updated_at = {_NOW},"
            f" history = history || jsonb_build_array(jsonb_build_object('attempt', attempt + 1,"
            f" 'owner', $2::text, 'started_at', {_NOW}, 'reclaimed', status = 'running'))"
            f" WHERE run_id = $1 AND instance = $4"
            f" AND (status = 'queued' OR (status = 'running' AND lease_until < {_NOW}))"
            f" RETURNING {_COLUMNS}",
            run_id, owner, float(lease_s), self.instance,
        )
        return _record(row) if row is not None else None

    async def heartbeat(self, run_id: str, owner: str, attempt: int, lease_s: float) -> bool:
        status = await self._db.execute(
            f"UPDATE agent_runs SET lease_until = {_NOW} + $4, updated_at = {_NOW}"
            f" WHERE run_id = $1 AND instance = $5 AND lease_owner = $2 AND attempt = $3"
            f" AND status = 'running'",
            run_id, owner, attempt, float(lease_s), self.instance,
        )
        return status.endswith(" 1")

    async def set_turn(self, run_id: str, owner: str, attempt: int, turn_id: str, degrade_level: int) -> bool:
        status = await self._db.execute(
            f"UPDATE agent_runs SET turn_id = $4, degrade_level = $5, updated_at = {_NOW},"
            f" history = CASE WHEN jsonb_array_length(history) = 0 THEN history ELSE"
            f" jsonb_set(history, ARRAY[(jsonb_array_length(history) - 1)::text],"
            f" (history -> -1) || jsonb_build_object('turn_id', $4::text)) END"
            f" WHERE run_id = $1 AND instance = $6 AND lease_owner = $2 AND attempt = $3"
            f" AND status = 'running'",
            run_id, owner, attempt, turn_id, degrade_level, self.instance,
        )
        return status.endswith(" 1")

    async def checkpoint(self, run_id: str, owner: str, attempt: int, data: dict[str, Any]) -> bool:
        """写下提交点（带栅栏）。写不进去说明租约已经不在手上：调用方不能再落任何副作用。"""
        status = await self._db.execute(
            f"UPDATE agent_runs SET checkpoint = $4::jsonb, updated_at = {_NOW},"
            f" history = CASE WHEN jsonb_array_length(history) = 0 THEN history ELSE"
            f" jsonb_set(history, ARRAY[(jsonb_array_length(history) - 1)::text],"
            f" (history -> -1) || jsonb_build_object('committed', true)) END"
            f" WHERE run_id = $1 AND instance = $5 AND lease_owner = $2 AND attempt = $3"
            f" AND status = 'running'",
            run_id, owner, attempt, json.dumps(data, ensure_ascii=False), self.instance,
        )
        return status.endswith(" 1")

    async def finish(self, run_id: str, owner: str, attempt: int, status: str, *,
                     result: dict[str, Any] | None = None, error: str = "") -> RunRecord | None:
        row = await self._db.fetchrow(
            f"UPDATE agent_runs SET status = $4, result = $5::jsonb, error = $6,"
            f" finished_at = {_NOW}, updated_at = {_NOW}, lease_owner = '', lease_until = 0,"
            f" {_CLOSE_LAST.format(now=_NOW, outcome='$4')}"
            f" WHERE run_id = $1 AND instance = $7 AND lease_owner = $2 AND attempt = $3"
            f" AND status = 'running' RETURNING {_COLUMNS}",
            run_id, owner, attempt, status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            error[:2000], self.instance,
        )
        return _record(row) if row is not None else None

    async def release(self, run_id: str, owner: str, attempt: int) -> bool:
        status = await self._db.execute(
            f"UPDATE agent_runs SET status = 'queued', lease_owner = '', lease_until = 0,"
            f" updated_at = {_NOW}, dispatched_at = {_NOW},"
            f" {_CLOSE_LAST.format(now=_NOW, outcome=repr('released'))}"
            f" WHERE run_id = $1 AND instance = $4 AND lease_owner = $2 AND attempt = $3"
            f" AND status = 'running'",
            run_id, owner, attempt, self.instance,
        )
        return status.endswith(" 1")

    async def due_for_dispatch(self, *, idle_s: float, lease_grace_s: float, limit: int = 100) -> list[str]:
        rows = await self._db.fetch(
            f"SELECT run_id FROM agent_runs WHERE instance = $1 AND dispatched_at < {_NOW} - $2"
            f" AND (status = 'queued' OR (status = 'running' AND lease_until < {_NOW} - $3))"
            f" ORDER BY created_at LIMIT $4",
            self.instance, float(idle_s), float(lease_grace_s), limit,
        )
        return [row["run_id"] for row in rows]

    async def mark_dispatched(self, run_ids: list[str]) -> None:
        if run_ids:
            await self._db.execute(
                f"UPDATE agent_runs SET dispatched_at = {_NOW} WHERE instance = $1 AND run_id = ANY($2::text[])",
                self.instance, run_ids,
            )

    async def counts(self) -> dict[str, int]:
        rows = await self._db.fetch(
            "SELECT status, count(*) AS n FROM agent_runs WHERE instance = $1 GROUP BY status",
            self.instance,
        )
        return {row["status"]: int(row["n"]) for row in rows}

    async def purge(self, older_than_s: float) -> int:
        status = await self._db.execute(
            f"DELETE FROM agent_runs WHERE instance = $1 AND finished_at IS NOT NULL"
            f" AND finished_at < {_NOW} - $2",
            self.instance, float(older_than_s),
        )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def ping(self) -> bool:
        await self._db.fetchrow("SELECT 1")
        return True

