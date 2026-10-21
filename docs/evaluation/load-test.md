# Load test: what limits queued mode

[README](../../README.md#durable-runs) · [Design](../design/durable-agent-runs.md) · [Chaos drill](chaos-drill.md)

Queued mode lets workers be added by load. This record measures where adding them stops helping, what the token bucket and the degrade ladder change, and what the queue costs a single turn.

## Conditions

- Stack: [deploy/drill](../../deploy/drill/docker-compose.yml) on one laptop (Docker Desktop, 12 CPUs, 7.7 GB): Postgres, Redis, one API, *N* workers running four runs each.
- Model: the [mock model](../../backend/scripts/mock_llm.py) with a quota of 300 requests a minute (it answers 429 with `retry-after` above that), answers that start after 3 s and stream for about 1 s, structured calls (intent vote, rewrite, rerank, profile) of 0.8 s. No real model, so the numbers say nothing about any provider's latency; they show how the system behaves against a fixed quota.
- Load: [loadtest/runs.js](../../loadtest/runs.js) on k6 2.3.0, arrivals ramping over five one-minute stages, every run polled to its terminal state. The light profile goes 30 → 60 → 90 → 120 → 120 runs a minute, the heavy one 60 → 120 → 180 → 240 → 240. Messages carry a unique cabinet number so the intent cache never answers; across the load runs, 1,924 of 3,837 turns were classified by the rule channel without the intent vote. Timings are the server's (`created_at`, `started_at`, `finished_at`).
- Queue cap 200; the token bucket and the degrade ladder per scenario, below.

## Reproduce

```bash
docker compose -f deploy/drill/docker-compose.yml build api
export MOCK_RPM=300 MOCK_LATENCY_MS=3000 MOCK_STRUCTURED_MS=800 QUEUE_MAX=200
PROFILE=30,60,90,120,120 WORKERS=1 LLM_RPM=270 DEGRADE=off loadtest/run.sh a1-one-worker
PROFILE=30,60,90,120,120 WORKERS=2 LLM_RPM=270 DEGRADE=off loadtest/run.sh a2-two-workers
export PROFILE=60,120,180,240,240
WORKERS=2 LLM_RPM=270 DEGRADE=off loadtest/run.sh h0-bucket-at-90pct
WORKERS=2 LLM_RPM=255 DEGRADE=off loadtest/run.sh h1-two-workers
WORKERS=4 LLM_RPM=255 DEGRADE=off loadtest/run.sh h2-four-workers
WORKERS=4 LLM_RPM=0   DEGRADE=off loadtest/run.sh h3-no-admission
WORKERS=4 LLM_RPM=255 DEGRADE=20,40,80,160 loadtest/run.sh h4-degrade
python3 loadtest/report.py loadtest/results/*

docker compose -f deploy/drill/docker-compose.yml --profile compare up -d api-inline
backend/.venv/bin/python loadtest/ttfe_probe.py --n 30
```

## Results

Two profiles. The light one (arrivals up to 120 runs a minute) finds the worker limit; the heavy one (up to 240) goes past the model quota.

| Scenario | Workers | Token bucket (RPM) | Degrade | Accepted / refused at the door | Finished per minute at the peak | Queue wait p50 / p95 (s) | Submit → done p50 / p95 (s) | Answered "unavailable" | Model calls per finished run | 429s from the model |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Light, 1 worker | 1 | 270 | off | 362 / 0 | 79 | 5.9 / 40.3 | 9.4 / 42.8 | 0 | 2.62 | 0 |
| Light, 2 workers | 2 | 270 | off | 374 / 0 | 112 | 0.0 / 0.0 | 3.1 / 7.5 | 0 | 2.40 | 0 |
| Heavy, 2 workers | 2 | 255 | off | 485 / 264 | 97 | 4.3 / 58.9 | 9.3 / 62.6 | 0 | 2.62 | 0 |
| Heavy, 4 workers | 4 | 255 | off | 496 / 253 | 103.5 | 0.0 / 56.5 | 8.3 / 64.5 | 2 | 2.48 | 0 |
| Heavy, 4 workers, no token bucket | 4 | off | off | 749 / 0 | 221.5 | 0.0 / 0.0 | 2.1 / 6.0 | **201** | 1.71 | **718** |
| Heavy, 4 workers, degrade on | 4 | 255 | 20,40,80,160 | 749 / 0 | 213.5 | 6.1 / 22.6 | 10.6 / 30.7 | 200 | 1.75 | 0 |
| Heavy, 2 workers, bucket at 90% | 2 | 270 | off | 592 / 100 | 117.5 | 0.0 / 50.5 | 4.2 / 55.6 | 29 | 2.27 | 72 |

"Finished per minute at the peak" is the mean of minutes 3 and 4, the two top arrival rates, counted from the server's `finished_at`; "Answered unavailable" are runs whose answer was the "can't answer right now" text, with no ticket. Per-minute series and the raw k6 summaries are produced by `loadtest/report.py` from `loadtest/results/`.

### One worker is the limit until the quota is

On the light profile one worker (four runs at a time) tops out at 79 runs a minute; the backlog climbs from the third minute and the queue wait p95 reaches 40 s. A second worker keeps up with all 120 a minute with no queueing at all.

### Past the quota, more workers change nothing

On the heavy profile two workers finish 97 runs a minute and four finish 103.5. Both are the token bucket's ceiling, 255 calls a minute divided by the calls each run makes (255 / 2.62 ≈ 97, 255 / 2.48 ≈ 103). Doubling the workers moved the waiting from the queue (p50 4.3 s → 0 s) to the bucket (submit to done p50 9.3 s → 8.3 s, p95 62.6 s → 64.5 s): no turn finished sooner. This is the number KEDA's ceiling should come from, ⌈(*R/c*) ÷ (per-worker throughput)⌉ = ⌈97 ÷ 79⌉ = 2 for this quota, not from the backlog.

Above the ceiling the queue does its job: it fills to `AI_HELPDESK_RUN_QUEUE_MAX` (200), and the door then refuses new runs with 429 and `Retry-After` (264 and 253 refusals) instead of letting waits grow without bound.

### Without the token bucket the numbers look better and the answers are worse

With no bucket, the same four workers "finish" 221.5 runs a minute with a p95 of 6 s and refuse nobody. The model refused 718 calls, the model guard paused calls after each 429, and 201 of 749 runs (27%) were answered "the assistant can't answer right now". Failing fast is why throughput and latency looked healthy; with the bucket, the same overload became queueing and honest 429s at the door, with 2 local overload answers and no 429 from the model. A dashboard of throughput and latency alone would have called the worse configuration better, which is why the alert rules watch overload answers and dead letters, not just latency.

### The bucket has to sit below the quota by its burst

At 270 RPM (90% of the quota) the bucket still let 72 429s through and 29 runs were answered "unavailable": the bucket holds ten seconds of quota, so a 60 s window can see 270 + 45 = 315 calls, more than the model's 300. At 255 RPM (85%) there were none. The design doc's guidance, the quota times about 0.85, comes from this run.

### What degradation buys

With the ladder on, the backlog peaked at 158 and every rung was used. Model calls per run, from the event log, by the rung a run was given:

| Rung | Runs | Model calls per run | Answered "unavailable" |
| --- | ---: | ---: | ---: |
| 0 (none) | 245 | 2.63 | 0 |
| 1 (no tool loop) | 50 | 2.56 | 0 |
| 2 (+ no rewrite and rerank) | 159 | 2.09 | 1 |
| 3 (+ no delegation) | 97 | 2.10 | 1 |
| 4 (no model call) | 198 | 0 | 198 |

The second rung is the one that bought capacity here: about 20% fewer calls a run, which lifts the ceiling from ~97 to ~121 answered runs a minute on the same quota; minutes 3 and 4 answered 110 and 117. Rungs 1 and 3 saved nothing because this stack runs the tool loop off and has no SQL agent; on the staff instance the loop nearly doubles input tokens per turn (2,692 → 4,915 in the [tool-loop A/B](tool-loop-ab.md)) and a delegation costs 17–40 s, so they matter there. The last rung absorbed the rest: instead of refusing 253 runs at the door, it answered 198 of them at once with "can't answer right now, try again" and no ticket. Which of the two a desk prefers is a product decision; placing the last threshold at or above the queue cap makes the door refuse first.

### What the queue costs one turn

At low load, the same backend in inline and queued mode (30 alternating pairs, [ttfe_probe.py](../../loadtest/ttfe_probe.py)):

| Mode | First sentence p50 / p95 (s) | Whole turn p50 / p95 (s) |
| --- | ---: | ---: |
| inline | 1.006 / 1.366 | 1.347 / 1.569 |
| queued | 1.005 / 1.378 | 1.352 / 1.567 |

The write to Postgres, the dispatch, the claim and the extra hop of every frame through Redis are below what this probe resolves.

## The event log on Kafka: latency and throughput

With `AI_HELPDESK_EVENT_SINK=kafka` a turn event goes writer → Kafka → projector → `turn_events` instead of writer → `turn_events`. Two questions: what the detour costs an event, and how much one projector can take.

### Latency

With Postgres's `track_commit_timestamp` on, every row carries the time it was committed; minus the event's own timestamp, taken when it was emitted (both clocks are the same Docker host's), that is the time from emitting an event to its being readable. Same load for both sinks: 60 runs at 2 a second with the default mock model, no faults.

| Sink | Events | Batched events p50 / p95 | Turn-ending events p50 / p95 | All events p99 / max |
| --- | ---: | ---: | ---: | ---: |
| Kafka → projector → Postgres | 687 | 250 / 281 ms | 72 / 79 ms | 298 / 392 ms |
| Postgres directly | 683 | 244 / 254 ms | 48 / 51 ms | 285 / 288 ms |

Most events wait for the writer's 250 ms batch window, which sets the median either way. A turn-ending event is sent at once, so its latency is the path itself: Kafka adds about 24 ms (72 against 48 ms), close to the producer's 20 ms linger. The 48 ms of a direct write comes from the writer checking its queue every 50 ms while it collects a batch (`EventWriter._next_batch`), not from Postgres.

### Throughput

[kafka_bench.py](../../backend/scripts/kafka_bench.py) clones the 120 turns these drills recorded (1,370 events with their data and text) into 411,000 events on a separate 6-partition topic, starts projectors on the drill network with the image and entry point that are deployed, and reads the consumer group's committed offsets every half second.

| Projectors | Steady rate (10% → 90% of the backlog) | Drained | Rows in `turn_events` |
| --- | ---: | ---: | ---: |
| 1 | 21,348 events/s (14.4 MB/s) | 19.8 s | 411,000 of 411,000 |
| 3 | 52,743 events/s (35.5 MB/s) | 10.9 s, the group joining included | 411,000 of 411,000 |

The heaviest load test above produced about 42 events a second (3.7 turns a second, 11.4 events each), so one projector has about 500 times that in hand, which is why KEDA never had a reason to add a second one in the drills. Three projectors on six partitions did 2.5 times one, not 3; all three write to the one Postgres on the same laptop.

```bash
DRILL_EVENT_SINK=kafka docker compose -f deploy/drill/docker-compose.yml --profile kafka up -d --wait
docker exec aihd-drill-postgres-1 psql -U drill -d drill -c "ALTER SYSTEM SET track_commit_timestamp = on"
docker restart aihd-drill-postgres-1
backend/.venv/bin/python backend/scripts/chaos_drill.py --runs 60 --rate 2 --kills 0 --reader-ratio 0
#   then: percentiles of extract(epoch FROM pg_xact_commit_timestamp(xmin)) - ts over that drill's rows;
#   again with DRILL_EVENT_SINK=postgres for the direct write
backend/.venv/bin/python backend/scripts/kafka_bench.py --copies 300 --consumers 1
backend/.venv/bin/python backend/scripts/kafka_bench.py --copies 300 --consumers 3
```

## Limits of this record

- One laptop, one run per scenario, a mock model with fixed latencies. The shape (where throughput stops, what the bucket and the ladder change) carries over; the absolute numbers do not.
- The Kafka figures come from one broker and one Postgres on the same laptop, and the throughput test replays cloned turns rather than live traffic; the latency comparison and the ratio between one and three projectors carry over better than the absolute rates.
- Load came in at a ramping rate; k6 could not start 57 iterations in the first heavy run (the 90% one) for lack of VUs, which were raised for the others (0 dropped).
- Messages are drawn from eight templates, and half the turns were classified by the rule channel without the intent vote; traffic with more open-ended questions makes more calls per run, and the ceiling moves with it.

