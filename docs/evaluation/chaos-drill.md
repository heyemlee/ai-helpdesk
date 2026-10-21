# Chaos drill: queued runs under failure

[README](../../README.md#durable-runs) · [Design](../design/durable-agent-runs.md) · [Load test](load-test.md)

Queued mode claims that a run survives the loss of the worker running it, of Redis, and of the model, with every effect applied once. This record tests that claim the only way that counts: submit runs, break things while they run, then reconcile every run against the database.

## What is checked

[chaos_drill.py](../../backend/scripts/chaos_drill.py) submits runs through `POST /runs` at a fixed rate, each in its own conversation with a unique cabinet number (so no intent cache helps). A quarter of them are followed by a client that reads a few frames, drops the connection on purpose and resumes with `Last-Event-ID` until the end. While that goes on it SIGKILLs workers (and brings each back ten seconds later), restarts or flushes Redis, makes the model answer 429 to everything for a while, or restarts the workers as a deploy would. When every run has reached a terminal state, each run is checked:

1. it reached a terminal state;
2. its conversation has at most one ticket, and it is the one the result names;
3. the audit has the customer's line once and at most one answer, equal to the final answer;
4. working memory holds exactly the customer's line and the final answer (not checked when Redis was flushed);
5. a fresh read of the frame stream, folded the way the console folds it, ends with the stored result;
6. every client that dropped and resumed saw the stored result.

Failures are listed by run; the script exits non-zero if any check fails.

## Conditions

- Mock model ([mock_llm.py](../../backend/scripts/mock_llm.py)) slowed down with `--slow`: answers start after 3 s, structured calls take 0.8 s, so a turn lasts long enough to be killed in the middle. 30% of answers end with `[[handoff:yes]]`, so tickets get created.
- Compose: [deploy/drill](../../deploy/drill/docker-compose.yml), one API and three workers (four runs each), lease 10 s (takeover after ~15 s idle), sweep after 15 s. A killed worker is `docker kill --signal KILL`.
- Kubernetes: [deploy/k8s](../../deploy/k8s) on kind, two API pods and a KEDA-scaled worker Deployment (1–4 pods), lease 15 s. A killed worker is `kubectl delete pod --grace-period=0 --force`; Redis is restarted by deleting its pod.

## Reproduce

```bash
docker compose -f deploy/drill/docker-compose.yml build api
docker compose -f deploy/drill/docker-compose.yml up -d --wait
backend/.venv/bin/python backend/scripts/chaos_drill.py --runs 60 --rate 2 --kills 3 --restart-redis --storm 20 --slow
backend/.venv/bin/python backend/scripts/chaos_drill.py --runs 60 --rate 2 --kills 2 --kill-every 10 --flush-redis --slow
backend/.venv/bin/python backend/scripts/chaos_drill.py --runs 60 --rate 2 --kills 0 --rollout --slow

scripts/k8s-up.sh
scripts/k8s-drill.sh --runs 80 --rate 3 --kills 3 --restart-redis --slow
scripts/k8s-drill.sh --runs 80 --rate 3 --kills 0 --rollout --slow
```

## Results

Five drills, three on Compose and two on kind, each run once. The kind image was built at commit 9e0a62a; the Compose image a few minutes earlier, when schema setup still ran the whole script under the lock on every start. Nothing committed after them changes what the drills exercise. Every check passed in all five: 340 runs, all `succeeded`; 24 of them were taken over after their worker was killed, each on its second attempt; 92 clients dropped their connection on purpose and reconnected 170 times between them.

| Drill | Faults | Runs | Taken over | Answered "unavailable" | Tickets | Queue wait p95 / max (s) | Submit → done p50 / p95 / max (s) | Checks |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Compose 1 | 3 worker SIGKILLs, Redis restarted, 20 s of 429s from the model | 60 | 9 | 23 | 11 | 3.8 / 21.9 | 4.6 / 28.6 / 29.4 | 6 of 6 |
| Compose 2 | 2 worker SIGKILLs, Redis flushed | 60 | 6 | 0 | 18 | 0.3 / 1.2 | 3.9 / 38.6 / 42.1 | 5 of 5, memory not checked |
| Compose 3 | All workers restarted (SIGTERM, drain) | 60 | 0 | 0 | 25 | 2.6 / 4.1 | 4.0 / 8.5 / 12.5 | 6 of 6 |
| kind 1 | 3 worker pods force-deleted, Redis pod deleted | 80 | 9 | 0 | 20 | 8.2 / 24.7 | 6.2 / 34.7 / 41.3 | 6 of 6 |
| kind 2 | `kubectl rollout restart` of the workers | 80 | 0 | 0 | 28 | 0.0 / 0.3 | 3.5 / 8.4 / 9.0 | 6 of 6 |

### A lost worker costs its runs about half a minute

Every run on a killed worker was taken over and finished on its second attempt, without a second ticket, audit line or memory entry. Those runs finished a median of 27 s after submission on Compose with a 10 s lease and 35 s on kind with a 15 s lease, 42 s at the slowest: the lease has to lapse and the message has to sit idle before a worker may claim it again, and the turn is then answered again. The lease length is the trade-off: a shorter one takes over sooner and costs more heartbeats.

### Redis can restart, or lose everything

The Redis restart in the first drill caught one submission between its two writes: the run was stored, the dispatch failed, the API accepted the run anyway, and the sweep dispatched it 22 s later (that drill's 21.9 s queue-wait maximum). Streams being relayed at that moment paused, and carried on once Redis was back.

Flushing Redis erased the dispatch stream, its consumer group, every frame stream and all working memory. The groups were recreated, and the killed workers' runs, whose pending entries were gone, could not be taken over from Redis: the sweep found them in Postgres about 22 and 30 s after the flush, which is why that drill's interrupted runs took longest (p50 38.6 s). Every client that resumed still ended with the final answer; where its frames were gone, the relay finished the stream from the stored result. The working-memory check is skipped for this drill because working memory lives only in Redis.

### A 429 storm is answered, not hidden

While the model refused everything for 20 s, 23 runs were answered "can't answer right now" without a ticket, the existing outage path; the model saw 41 refused and 85 successful requests. Those runs count as `succeeded` because that is the answer the system is meant to give during an outage. The token bucket keeps the system from causing such a storm itself (see the [load test](load-test.md)); it cannot stop a provider's.

### Deploys lose nothing

Neither restart took a run over. On Compose all three workers got SIGTERM at once, finished the seven runs they held (none was handed back) and were back five seconds later; the slowest run took 12.5 s against a 4.0 s median. The rolling restart on kind kept the slowest at 9.0 s.

### KEDA while pods are being killed

In the first kind drill the worker Deployment started with one pod, and the first kill took that pod out. KEDA asked for three pods 15 s in and had four ready at 36 s, while pods kept being killed. The drill's queue waits (p95 8.2 s, max 24.7 s) are the highest of the five; it is also the only one that began with a single worker and lost it within ten seconds.

### What the drills did not reach

- No run was interrupted after its commit point; every report says 0. Between recording the answer and finishing the run there are only a few writes, and kills every 8 to 10 s never landed there. That path is covered by `test_a_worker_dying_after_the_commit_point_does_not_answer_twice` and `test_a_redis_blip_after_the_commit_point_is_retried_from_the_checkpoint` in [test_runs_api.py](../../backend/tests/test_runs_api.py).
- No run reached the dead letter, since none needed a third attempt; that path is covered by `test_a_failure_that_does_not_go_away_ends_in_the_dead_letter` and `test_a_dead_run_is_handed_to_a_person` in the same file.
- Each drill ran once, on one laptop. A pass shows the recovery paths work under these faults; it says nothing about how rare a failure is.

<details>
<summary>The five reports as printed by <code>chaos_drill.py</code></summary>

### Drill 2026-10-21 01:01 (compose)

60 runs at 2/s; 3 worker SIGKILLs every 8 s (each back after 10 s); Redis restarted once; a 20 s storm of 429s from the model.

| Invariant | Result |
| --- | --- |
| Every run reached a terminal state | pass |
| At most one ticket per conversation, and it is the one the result names | pass |
| Audit: one customer line, at most one answer, equal to the final answer | pass |
| Working memory: exactly the two lines of the final answer | pass |
| A fresh read of the frame stream ends with the stored result | pass |
| Clients that dropped and resumed with Last-Event-ID saw the final answer | pass |

| Number | Value |
| --- | --- |
| Terminal states | succeeded 60 |
| Runs taken over after a worker was lost or a retry | 9 (resumed from the commit point: 0) |
| Attempts per run | 1: 51, 2: 9 |
| Attempt outcomes | lost 9, succeeded 60 |
| Answered "model unavailable" (no ticket) | 23 |
| Tickets | 11 |
| Queue wait p50 / p95 / max (s) | 0.0 / 3.8 / 21.9 |
| Submit → done p50 / p95 / max (s) | 4.6 / 28.6 / 29.4 |
| Interrupted runs, submit → done p50 / max (s) | 27.1 / 29.4 |
| Clients that dropped on purpose / reconnects | 16 / 27 |
| Model requests by status | {'200': 85, '429': 41} |

Timeline:

-   0.1 s: start: workers ['aihd-drill-worker-1', 'aihd-drill-worker-2', 'aihd-drill-worker-3']
-   6.2 s: SIGKILL worker aihd-drill-worker-1
-  10.3 s: Redis restarted (AOF on)
-  14.2 s: SIGKILL worker aihd-drill-worker-3
-  16.3 s: worker aihd-drill-worker-1 back
-  18.0 s: model answers 429 to everything for 20 s
-  22.2 s: SIGKILL worker aihd-drill-worker-1
-  24.3 s: worker aihd-drill-worker-3 back
-  30.9 s: submitted 60 runs (0 submissions got 429 and were retried)
-  32.3 s: worker aihd-drill-worker-1 back
-  48.4 s: terminal: {'succeeded': 60}

### Drill 2026-10-21 01:02 (compose)

60 runs at 2/s; 2 worker SIGKILLs every 10 s (each back after 10 s); Redis flushed (data loss) once.

| Invariant | Result |
| --- | --- |
| Every run reached a terminal state | pass |
| At most one ticket per conversation, and it is the one the result names | pass |
| Audit: one customer line, at most one answer, equal to the final answer | pass |
| Working memory: exactly the two lines of the final answer | not checked (Redis was flushed) |
| A fresh read of the frame stream ends with the stored result | pass |
| Clients that dropped and resumed with Last-Event-ID saw the final answer | pass |

| Number | Value |
| --- | --- |
| Terminal states | succeeded 60 |
| Runs taken over after a worker was lost or a retry | 6 (resumed from the commit point: 0) |
| Attempts per run | 1: 54, 2: 6 |
| Attempt outcomes | lost 6, succeeded 60 |
| Answered "model unavailable" (no ticket) | 0 |
| Tickets | 18 |
| Queue wait p50 / p95 / max (s) | 0.0 / 0.3 / 1.2 |
| Submit → done p50 / p95 / max (s) | 3.9 / 38.6 / 42.1 |
| Interrupted runs, submit → done p50 / max (s) | 38.6 / 42.1 |
| Clients that dropped on purpose / reconnects | 21 / 45 |
| Model requests by status | {'200': 168} |

Timeline:

-   0.1 s: start: workers ['aihd-drill-worker-1', 'aihd-drill-worker-2', 'aihd-drill-worker-3']
-   6.2 s: SIGKILL worker aihd-drill-worker-2
-  16.1 s: SIGKILL worker aihd-drill-worker-1
-  16.1 s: FLUSHALL: dispatch queue, frame streams and working memory gone
-  16.3 s: worker aihd-drill-worker-2 back
-  26.2 s: worker aihd-drill-worker-1 back
-  30.7 s: submitted 60 runs (0 submissions got 429 and were retried)
-  55.7 s: terminal: {'succeeded': 60}

### Drill 2026-10-21 01:09 (compose)

60 runs at 2/s; the workers restarted as a deploy would (SIGTERM, drain).

| Invariant | Result |
| --- | --- |
| Every run reached a terminal state | pass |
| At most one ticket per conversation, and it is the one the result names | pass |
| Audit: one customer line, at most one answer, equal to the final answer | pass |
| Working memory: exactly the two lines of the final answer | pass |
| A fresh read of the frame stream ends with the stored result | pass |
| Clients that dropped and resumed with Last-Event-ID saw the final answer | pass |

| Number | Value |
| --- | --- |
| Terminal states | succeeded 60 |
| Runs taken over after a worker was lost or a retry | 0 (resumed from the commit point: 0) |
| Attempts per run | 1: 60 |
| Attempt outcomes | succeeded 60 |
| Answered "model unavailable" (no ticket) | 0 |
| Tickets | 25 |
| Queue wait p50 / p95 / max (s) | 0.0 / 2.6 / 4.1 |
| Submit → done p50 / p95 / max (s) | 4.0 / 8.5 / 12.5 |
| Interrupted runs, submit → done p50 / max (s) | — / — |
| Clients that dropped on purpose / reconnects | 17 / 30 |
| Model requests by status | {'200': 148} |

Timeline:

-   0.1 s: start: workers ['aihd-drill-worker-1', 'aihd-drill-worker-2', 'aihd-drill-worker-3']
-  13.0 s: restarting the workers as a deploy would (SIGTERM, drain)
-  18.3 s: restart command returned
-  30.7 s: submitted 60 runs (0 submissions got 429 and were retried)
-  36.7 s: terminal: {'succeeded': 60}

### Drill 2026-10-21 01:18 (k8s)

80 runs at 3/s; 3 worker SIGKILLs every 8 s (each back after 10 s); Redis restarted once.

| Invariant | Result |
| --- | --- |
| Every run reached a terminal state | pass |
| At most one ticket per conversation, and it is the one the result names | pass |
| Audit: one customer line, at most one answer, equal to the final answer | pass |
| Working memory: exactly the two lines of the final answer | pass |
| A fresh read of the frame stream ends with the stored result | pass |
| Clients that dropped and resumed with Last-Event-ID saw the final answer | pass |

| Number | Value |
| --- | --- |
| Terminal states | succeeded 80 |
| Runs taken over after a worker was lost or a retry | 9 (resumed from the commit point: 0) |
| Attempts per run | 1: 71, 2: 9 |
| Attempt outcomes | lost 9, succeeded 80 |
| Answered "model unavailable" (no ticket) | 0 |
| Tickets | 20 |
| Queue wait p50 / p95 / max (s) | 2.4 / 8.2 / 24.7 |
| Submit → done p50 / p95 / max (s) | 6.2 / 34.7 / 41.3 |
| Interrupted runs, submit → done p50 / max (s) | 34.7 / 41.3 |
| Clients that dropped on purpose / reconnects | 20 / 38 |
| Model requests by status | {'200': 230} |

Timeline:

-   0.1 s: start: workers ['worker-staff-cbc97d6bb-r55fz']
-   8.1 s: SIGKILL worker worker-staff-cbc97d6bb-r55fz
-  12.1 s: Redis restarted (AOF on)
-  16.1 s: SIGKILL worker worker-staff-cbc97d6bb-fx2j8
-  18.1 s: worker worker-staff-cbc97d6bb-r55fz back
-  24.1 s: SIGKILL worker worker-staff-cbc97d6bb-2xkkl
-  26.1 s: worker worker-staff-cbc97d6bb-fx2j8 back
-  29.6 s: submitted 80 runs (0 submissions got 429 and were retried)
-  34.1 s: worker worker-staff-cbc97d6bb-2xkkl back
-  55.1 s: terminal: {'succeeded': 80}

### Drill 2026-10-21 01:20 (k8s)

80 runs at 3/s; the workers restarted as a deploy would (SIGTERM, drain).

| Invariant | Result |
| --- | --- |
| Every run reached a terminal state | pass |
| At most one ticket per conversation, and it is the one the result names | pass |
| Audit: one customer line, at most one answer, equal to the final answer | pass |
| Working memory: exactly the two lines of the final answer | pass |
| A fresh read of the frame stream ends with the stored result | pass |
| Clients that dropped and resumed with Last-Event-ID saw the final answer | pass |

| Number | Value |
| --- | --- |
| Terminal states | succeeded 80 |
| Runs taken over after a worker was lost or a retry | 0 (resumed from the commit point: 0) |
| Attempts per run | 1: 80 |
| Attempt outcomes | succeeded 80 |
| Answered "model unavailable" (no ticket) | 0 |
| Tickets | 28 |
| Queue wait p50 / p95 / max (s) | 0.0 / 0.0 / 0.3 |
| Submit → done p50 / p95 / max (s) | 3.5 / 8.4 / 9.0 |
| Interrupted runs, submit → done p50 / max (s) | — / — |
| Clients that dropped on purpose / reconnects | 18 / 30 |
| Model requests by status | {'200': 187} |

Timeline:

-   0.1 s: start: workers ['worker-staff-cbc97d6bb-m4kdc', 'worker-staff-cbc97d6bb-vbrxm', 'worker-staff-cbc97d6bb-w9dzh']
-  13.0 s: restarting the workers as a deploy would (SIGTERM, drain)
-  13.1 s: restart command returned
-  27.5 s: submitted 80 runs (0 submissions got 429 and were retried)
-  36.2 s: terminal: {'succeeded': 80}

</details>

## The main stack with the real model

The drills use the mock model, the drill stack and kind. Afterwards queued mode ran once on the main Compose stack (`docker-compose.yml` plus `docker-compose.queued.yml`, both instances, nginx in front of the customer instance, ai-sql-agent on the host) with the real model, at commit 79bcac2. Five turns, each reconciled against Postgres and Redis afterwards like a drill run:

| Turn | Entry | Outcome |
| --- | --- | --- |
| Staff, hinge adjustment | console proxy | answered in 6.4 s |
| Staff, order status | console proxy | two delegations to ai-sql-agent (prefetch and tool loop), answered in 57.3 s, one handoff ticket |
| Staff, order date and amount; worker SIGKILLed after the prefetch delegation | console proxy | taken over on attempt 2, answered in 83.8 s on the same connection |
| Customer, warranty | nginx | answered in 14.7 s |
| Customer, door gap; client dropped after four frames | nginx | resumed through `GET /runs/{id}/events` with `Last-Event-ID`; the frames after the cursor added up to the stored answer |

The killed worker was started again 10 s later; the run was taken over 41 s after the kill, mostly the 30 s lease. In attempt 2 the event log records the prefetch delegation at 1.1 ms and the knowledge search at 2.7 ms, against 12.7 s and 15.3 s for real calls in the same session: both came from the per-run memo, and ai-sql-agent was not asked again. Every turn had at most one ticket (the one its result names), one customer line and one answer in the audit equal to the result, and one turn in working memory. Each instance's runs were executed by its own worker, and the customer entry point answered 404 for a staff run id.

## What the drills found

Deploying to Kubernetes and running the drill there found four real defects before any run failed a check, each fixed in the same branch:

- **Schema creation raced.** Five or six pods starting at once against a fresh database all ran `CREATE EXTENSION / TABLE IF NOT EXISTS`, which is not safe under concurrency; the losers crashed on `pg_extension_name_index`. Schema setup now takes a Postgres advisory lock. A test creates the schema from six connections on a new database.
- **Requests hung on a Redis pod that was gone.** Without socket timeouts, connections to the deleted Redis pod received neither data nor a reset, so `POST /runs` hung for the client's whole 30 s timeout. Redis clients now have connect, read and keepalive timeouts; a submission fails open when the backlog cannot be read (Postgres has the run and the sweep dispatches it); the stream relay blocks at most 5 s per read.
- **Pods crash-looped on the first deploy** because they started before Postgres accepted connections. An init container now waits for Postgres and Redis.
- **Starting pods deadlocked on the schema.** Even under the lock, a pod that started while others were serving still ran `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, which takes an exclusive table lock when there is nothing to add, and deadlocked against their queries. Startup now checks the catalog and runs the schema script only when something is missing.

One more gap was found while preparing the first drill rather than by it: a Redis blip during the follow-up steps (writing working memory) would have failed a turn for good, although its answer was already decided. Runs that hit an infrastructure error, or fail after their commit point, are now handed back and retried; errors in the turn itself are still final.

Checking the console by hand against the drill stack found the last one: when the API container restarted in the middle of an answer, the dev proxy and Docker Desktop's port forwarding kept the browser's connection open without sending anything, so the console waited forever instead of resuming. It now treats 45 s without a byte, three missed heartbeats, as a dropped connection and resumes from `Last-Event-ID`. The drill never restarts the API, so it could not have caught this.

Switching the main stack back to inline mode found one more, older than this branch: nginx resolved `ai-helpdesk-customer` once at startup, so when the customer container was recreated with a new IP it kept connecting to the old one and answered 502 until reloaded. The upstream now re-resolves through Docker's DNS (`resolve`, valid 10 s); with throwaway containers, a backend recreated on a new IP was served again within 6 s, where the old configuration kept timing out.
