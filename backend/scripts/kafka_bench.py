"""
投影消费者的吞吐：往一个单独的 topic 里灌一批真实形状的事件，看几个投影消费者多快把它写进库。

事件拿库里已经记下的轮次当模板（真实的大小：结构加原文，平均一千多字节），复制成很多份，每份
换新的 turn_id 和会话，顺序和分区键照旧。投影消费者跑在演练栈的网络里，和线上是同一个镜像、
同一个入口（`python -m app.events project`），从宿主机每半秒读一次消费组的提交点：

- 稳态吞吐：提交点从 10% 走到 90% 之间的斜率，条/秒；
- 追平用时：从消费者起来到积压归零。

跑完核对库里这批事件一条不少。演练栈要用 kafka 那一档起（deploy/drill/docker-compose.yml）。

    backend/.venv/bin/python backend/scripts/kafka_bench.py --copies 75 --consumers 1
    backend/.venv/bin/python backend/scripts/kafka_bench.py --copies 75 --consumers 3
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
import uuid

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.events.kafka import KafkaSettings, encode, ensure_topics  # noqa: E402
from app.events.recorder import new_turn_id  # noqa: E402
from app.events.types import event_from_row  # noqa: E402


async def committed(settings: KafkaSettings, group: str, topic: str) -> tuple[int, int]:
    """消费组提交到哪了、topic 一共多少条（各分区加起来）。"""
    from aiokafka import AIOKafkaConsumer
    from aiokafka.admin import AIOKafkaAdminClient
    from aiokafka.structs import TopicPartition

    consumer = AIOKafkaConsumer(bootstrap_servers=settings.bootstrap, group_id=None)
    await consumer.start()
    try:
        await consumer._client.force_metadata_update()
        partitions = [TopicPartition(topic, p) for p in sorted(consumer.partitions_for_topic(topic) or [])]
        ends = await consumer.end_offsets(partitions)
    finally:
        await consumer.stop()
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.bootstrap)
    await admin.start()
    try:
        offsets = await admin.list_consumer_group_offsets(group)
    finally:
        await admin.close()
    return sum(offsets[tp].offset for tp in partitions if tp in offsets), sum(ends.values())


async def produce(settings: KafkaSettings, topic: str, events) -> float:
    from aiokafka import AIOKafkaProducer

    producer = AIOKafkaProducer(bootstrap_servers=settings.bootstrap, acks="all", enable_idempotence=True,
                                linger_ms=20, compression_type="gzip", max_batch_size=262144)
    await producer.start()
    started = time.monotonic()
    try:
        pending = []
        for event in events:
            key, value = encode(event)
            pending.append(await producer.send(topic, value=value, key=key))
            if len(pending) >= 5000:
                await asyncio.gather(*pending)
                pending = []
        await asyncio.gather(*pending)
    finally:
        await producer.stop()
    return time.monotonic() - started


def _at(samples: list[tuple[float, int]], target: float) -> float | None:
    """提交点第一次到达 target 的时刻（相邻两个样本之间线性插值）。"""
    for (t0, c0), (t1, c1) in zip(samples, samples[1:], strict=False):
        if c0 < target <= c1:
            return t0 + (t1 - t0) * (target - c0) / max(c1 - c0, 1)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description="投影消费者的吞吐")
    parser.add_argument("--bootstrap", default="127.0.0.1:29092")
    parser.add_argument("--pg", default="postgresql://drill:drill@127.0.0.1:25432/drill")
    parser.add_argument("--template-like", default="drill-%", help="拿哪些会话的事件当模板（conv_id LIKE）")
    parser.add_argument("--copies", type=int, default=75)
    parser.add_argument("--consumers", type=int, default=1)
    parser.add_argument("--image", default="aihd-drill/backend:dev")
    parser.add_argument("--network", default="aihd-drill_default")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    tag = uuid.uuid4().hex[:6]
    settings = KafkaSettings(bootstrap=args.bootstrap, topic_prefix=f"bench-{tag}.turn-events")
    topic = settings.topic("staff")
    group = f"{settings.topic_prefix}.projector.staff"
    await ensure_topics(settings, ["staff"], partitions=6, retention_days=1)

    pg = await asyncpg.create_pool(args.pg, min_size=1, max_size=2)
    rows = await pg.fetch("SELECT * FROM turn_events WHERE instance = 'staff' AND conv_id LIKE $1"
                          " ORDER BY turn_id, seq", args.template_like)
    templates: dict[str, list] = {}
    for row in rows:
        event = event_from_row(row)
        templates.setdefault(event.turn_id, []).append(event)
    events = []
    for copy in range(args.copies):
        for turn in templates.values():
            turn_id = new_turn_id()
            conv_id = f"bench-{tag}-{copy}-{turn[0].conv_id}"[:190]
            events += [dataclasses.replace(e, turn_id=turn_id, conv_id=conv_id) for e in turn]
    payload = sum(len(e.data) + len(e.body or "") for e in events)
    print(f"{len(events)} events from {len(templates)} template turns × {args.copies}, "
          f"{payload / 1e6:.1f} MB of data and text", flush=True)

    produce_s = await produce(settings, topic, events)
    print(f"produced in {produce_s:.1f} s ({len(events) / produce_s:.0f} events/s)", flush=True)

    names = [f"bench-projector-{tag}-{n}" for n in range(args.consumers)]
    started = time.monotonic()
    for name in names:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", args.network,
                        "-e", "AI_HELPDESK_KAFKA_BOOTSTRAP=kafka:9092",
                        "-e", f"AI_HELPDESK_KAFKA_TOPIC_PREFIX={settings.topic_prefix}",
                        "-e", "POSTGRES_DSN=postgresql://drill:drill@postgres:5432/drill",
                        "-e", "AI_HELPDESK_CONSUMER_METRICS_PORT=0", "-e", "LOG_LEVEL=WARNING",
                        args.image, "python", "-m", "app.events", "project", "--instance", "staff"],
                       check=True, capture_output=True)
    samples: list[tuple[float, int]] = [(0.0, 0)]
    total = len(events)
    try:
        while time.monotonic() - started < 600:
            await asyncio.sleep(0.5)
            try:
                done, end = await committed(settings, group, topic)
            except Exception:
                continue
            samples.append((time.monotonic() - started, done))
            if done >= end >= total:
                break
    finally:
        for name in names:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    t10, t90 = _at(samples, 0.1 * total), _at(samples, 0.9 * total)
    first = next((t for t, c in samples if c > 0), None)
    drained = samples[-1][0] if samples[-1][1] >= total else None
    written = await pg.fetchval("SELECT count(*) FROM turn_events WHERE conv_id LIKE $1", f"bench-{tag}-%")
    await pg.close()
    result = {
        "consumers": args.consumers,
        "events": total,
        "megabytes": round(payload / 1e6, 1),
        "produce_events_per_s": round(total / produce_s),
        "steady_events_per_s": round(0.8 * total / (t90 - t10)) if t10 and t90 else None,
        "steady_mb_per_s": round(0.8 * payload / 1e6 / (t90 - t10), 1) if t10 and t90 else None,
        "first_commit_s": round(first, 1) if first is not None else None,
        "drained_s": round(drained, 1) if drained is not None else None,
        "rows_in_postgres": written,
        "topic": topic,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({**result, "samples": samples}, fh, ensure_ascii=False, indent=2)
    return 0 if written == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
