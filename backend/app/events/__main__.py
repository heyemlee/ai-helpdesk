"""
事件日志在 Kafka 那一侧的进程入口（和服务同一个镜像）：

    python -m app.events topics  [--instance staff --instance customer] [--partitions 6] [--retention-days 7]
    python -m app.events project --instance staff
    python -m app.events sample  --instance staff

- topics：建各实例的事件 topic 和死信 topic，已有的把保留期改成这里的值（部署时跑一次）；
- project：投影消费者，写 `turn_events`；
- sample：采样消费者，写 `eval_candidates`。

环境变量：AI_HELPDESK_KAFKA_BOOTSTRAP、AI_HELPDESK_KAFKA_TOPIC_PREFIX、POSTGRES_DSN；
AI_HELPDESK_EVENT_LOG_BODY_DAYS / AI_HELPDESK_EVENT_LOG_DAYS 和这个实例的服务配成一样；
AI_HELPDESK_SAMPLER_RULES / _RANDOM_RATE / _MIN_CONFIDENCE；指标端口 AI_HELPDESK_CONSUMER_METRICS_PORT
（默认 9102，0 不开）。两个消费者都不建表：表由服务启动时建，没建好之前它们退避重试。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from app.core.request_context import setup_logging
from app.events.consumers import ConsumerSettings, Projector, Sampler, SamplingRules
from app.events.kafka import DEFAULT_PARTITIONS, DEFAULT_RETENTION_DAYS, INSTANCES, KafkaSettings, ensure_topics

logger = logging.getLogger("app.events")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.events", description="事件日志的 Kafka 消费者和 topic")
    sub = parser.add_subparsers(dest="command", required=True)
    topics = sub.add_parser("topics", help="建 topic，已有的改保留期")
    topics.add_argument("--instance", action="append", choices=INSTANCES, default=None)
    topics.add_argument("--partitions", type=int, default=DEFAULT_PARTITIONS)
    topics.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    topics.add_argument("--replication", type=int, default=1)
    for name in ("project", "sample"):
        command = sub.add_parser(name, help="投影消费者" if name == "project" else "采样消费者")
        command.add_argument("--instance", required=True, choices=INSTANCES)
    return parser


def _metrics() -> None:
    raw = os.getenv("AI_HELPDESK_CONSUMER_METRICS_PORT", "9102").strip()
    port = int(raw) if raw.isdigit() else 9102
    if port:
        from prometheus_client import start_http_server

        start_http_server(port)
        logger.info("指标在 :%d/metrics", port)


async def _consume(args) -> int:
    from app.events.store import PgEventStore
    from app.storage.pg import Database

    dsn = os.getenv("POSTGRES_DSN", "").strip()
    settings = ConsumerSettings.from_env(args.instance)
    if not dsn or not settings.kafka.bootstrap:
        logger.error("要设 POSTGRES_DSN 和 AI_HELPDESK_KAFKA_BOOTSTRAP")
        return 2
    db = Database(dsn, max_size=4)
    while True:
        try:
            await db.connect_pool()
            break
        except Exception as ex:
            logger.warning("连不上 Postgres（%s），5 秒后再试", type(ex).__name__)
            await asyncio.sleep(5)
    if args.command == "project":
        loop = Projector(settings, PgEventStore(db, args.instance))
    else:
        loop = Sampler(settings, db, SamplingRules.from_env())
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    _metrics()
    try:
        await loop.run(stop)
    finally:
        await db.close()
    logger.info("%s 停了", loop.consumer_name)
    return 0


async def _topics(args) -> int:
    settings = KafkaSettings.from_env()
    if not settings.bootstrap:
        logger.error("要设 AI_HELPDESK_KAFKA_BOOTSTRAP")
        return 2
    delay = 1.0
    for _ in range(30):
        try:
            out = await ensure_topics(settings, args.instance or list(INSTANCES), partitions=args.partitions,
                                      retention_days=args.retention_days, replication=args.replication)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        except ValueError as ex:
            logger.error("%s", ex)
            return 2
        except Exception as ex:
            logger.warning("建 topic 失败（%s），%.0f 秒后再试", type(ex).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
    return 1


def main(argv: list[str]) -> int:
    setup_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FORMAT", "text"))
    args = _parser().parse_args(argv)
    return asyncio.run(_topics(args) if args.command == "topics" else _consume(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
