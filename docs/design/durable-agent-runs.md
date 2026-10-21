# Durable agent runs

[README](../../README.md) · [Chaos drill](../evaluation/chaos-drill.md) · [Load test](../evaluation/load-test.md)

`AI_HELPDESK_RUN_MODE=queued` takes a chat turn off the HTTP connection that asked for it. The API records the turn as a *run*, a worker executes it, and the client reads the turn's progress from a stream it can reconnect to. The default, `inline`, is unchanged.

## Why

In inline mode a whole turn (memory, intent vote, retrieval, a 17–40 s delegation to the SQL agent, the specialist answer, the answer guard, the ticket) runs inside the generator that writes the SSE response. Four things follow from that:

- A customer who refreshes the page, or whose connection drops, aborts the turn. The answer is lost.
- A deploy or a container restart drops every turn in flight.
- Long tasks are pinned to connections, so capacity can only be added by connection, not by work.
- Model rate limits are only handled after the fact: every replica sends, every replica gets the 429, every replica backs off. The quota is global; the reaction is not.

## Decision

A turn becomes a run with one source of truth and two disposable streams.

```
 client ──POST /chat/stream or /runs──▶ API ── INSERT agent_runs (queued) ─▶ Postgres   ← the only state
                                        │  └─ XADD run_id ─▶ Redis stream ai-helpdesk:runs:{instance}
                                        │                          │ XREADGROUP (group "workers")
                                        │                          ▼
 client ◀─GET /runs/{id}/events────── API ◀─ XREAD ─ ai-helpdesk:run:{id}:frames ◀─ XADD ─ worker
          (Last-Event-ID resumes)                                                        │
                                              claim (conditional UPDATE, attempt+1) ─────┤
                                              heartbeat every lease/3 ───────────────────┤
                                              commit point (answer recorded) ────────────┤
                                              ticket, memory, audit (idempotent) ────────┤
                                              finish (fenced) → result frame → XACK ─────┘
```

- **Postgres holds all run state** (`agent_runs`): status, attempt, lease owner and expiry, the commit point, the result and a per-attempt history. Leases and timestamps use the database clock, so workers on different machines agree on "now".
- **Redis Streams carry dispatch and frames**, and both can be rebuilt. A sweep re-dispatches runs that sat queued too long or whose lease expired long ago, so a lost dispatch message (Redis restarted without persistence, the API died between INSERT and XADD) delays a run but never loses it. A lost frame stream is completed from the stored result.
- **Workers are the same image as the API** (`AI_HELPDESK_ROLE=api|worker|all`) and run the existing turn pipeline unchanged: the inline endpoint and the worker consume the same generator of SSE frames.

## How a run survives failures

Execution is **at least once**; every effect is **applied once**; and once an answer is decided, **it is never re-decided**.

| Failure | Detected by | Recovery | What the customer sees |
| --- | --- | --- | --- |
| Worker killed before the answer was decided | Lease not renewed; message idle in the pending list | Another worker takes it over with XAUTOCLAIM and a conditional UPDATE (`attempt+1`), answers again; successful tool results are replayed from the per-run memo, so the 40 s delegation is not repeated | An `attempt` frame clears the half answer; the new answer streams in |
| Worker killed after the answer was decided | Same | The commit point (`checkpoint`) is in the row: the new attempt skips the model entirely and finishes the follow-up steps with the recorded answer | The same answer, sent again in one piece |
| Worker stalls (event loop blocked, DB unreachable) | Its heartbeat matches zero rows, or cannot be confirmed for a whole lease | The attempt is cancelled; its later writes are fenced out by `(lease_owner, attempt)`; its late frames carry the old attempt number and the relay drops them | Nothing from the stale attempt |
| Infrastructure error mid-turn (Redis or Postgres connection) or any error after the commit point | Exception classified as transient / post-commit | Run handed back with outcome `retry`; the sweep re-dispatches it after `AI_HELPDESK_RUN_REDISPATCH_S` | A short delay |
| A run keeps failing like that | `max_attempts` lost or retried attempts | Dead-letter stream, status `dead`, a handoff ticket, the customer's words in the audit | "This needs a person" with a ticket |
| Redis restarted (AOF on) | Consumers and publishers get connection errors | Consume loops back off; frames published during the gap are lost, the stored result fills them in | Possibly a gap in the progress, never in the answer |
| Redis lost all data | `NOGROUP`, empty streams | Groups are recreated; the sweep re-dispatches from Postgres; working memory is gone (it only lives in Redis) | Delays; the next turn has no short-term memory |
| API pod dies with streams open | Connection drops | The client reconnects to another API pod with `Last-Event-ID` | Nothing |
| Deploy / scale-down (SIGTERM) | — | `/readyz` turns 503, the worker stops taking runs, waits for the ones it has (`AI_HELPDESK_RUN_DRAIN_S`) and hands back any that did not finish | Nothing |
| Upstream 429s | Model guard | Process-wide backoff, answered as "model service unavailable", no ticket (the existing outage path); the token bucket below exists so the system does not cause this itself | "Can't answer right now, try again" |
| Overload | Backlog | 429 + `Retry-After` at the door above `AI_HELPDESK_RUN_QUEUE_MAX`; the degrade ladder below it | Retry later, or a leaner answer |

### Effects are keyed by the run

| Effect | Key | Mechanism |
| --- | --- | --- |
| Handoff ticket | `tk_` + hash of instance and run | Deterministic id; `INSERT … ON CONFLICT (id) DO NOTHING`, then return the existing ticket. Its webhook outbox is keyed by ticket id already |
| Audit (customer line, answer) | `run:{instance}:{run}:audit:{role}` | Dedup row inserted in the same transaction as the audit row (side table, so old images reading `SELECT *` are unaffected) |
| Working memory | `run:{instance}:{run}:memory` | One Lua script: `SET NX` the marker and `LPUSH` both lines, or do nothing |
| Tool results | sha256 of tool and normalised arguments | Per-run hash in Redis, read only on attempts after the first; failures are not recorded |
| Event log | per attempt | Each attempt is its own turn; `turn_started.run` names the run and attempt, `resumed_from` points at the turn that answered. A lost attempt never writes its own terminal event, so the attempt that takes over (or the dead-letter path) closes that turn with `turn_abandoned` on a reserved seq: written twice, stored once |

Exactly-once *execution* is not on offer from any component here; the design makes duplicate execution harmless instead. The commit point closes the one gap idempotency alone leaves: a worker that dies after writing the audit but before recording the result would otherwise make the retry answer again, so the customer and the audit would see different answers.

### Streams and resume

Every frame is a Redis stream entry; its id is the SSE `id:`. `/chat/stream` (and `GET /runs/{id}/events`) first sends a `run` frame with the run id, then the frames of the run. A client that loses the connection reconnects with `Last-Event-ID` and gets exactly the frames after it. The console does this automatically, five times with backoff. A new attempt starts with an `attempt` frame; the console clears what the previous attempt had shown.

## Load: admission, degradation, backpressure

- **Token bucket in Redis**, shared by every replica, in front of every model call (`GuardedLLMClient` is the one place all calls pass). RPM and TPM (`AI_HELPDESK_LLM_RPM`, `…_TPM`); TPM charges an estimate and settles with the real usage afterwards. The bucket holds ten seconds of quota, not a minute, because providers enforce per-minute limits over shorter windows. A call that would wait longer than `AI_HELPDESK_LLM_ADMISSION_WAIT_S` fails locally as `overloaded` and never leaves the process.
- **Degrade ladder** chosen per run from the backlog when a worker claims it (`AI_HELPDESK_DEGRADE_BACKLOG`, default 20/40/80/160): no tool loop → no rewrite and rerank → no delegation → no model call. The last rung answers like an outage (no ticket), not with a handoff as first planned: during overload a ticket per question would bury the tickets that need a person, for the same reason the outage path opens none.
- **Backpressure at the door**: above `AI_HELPDESK_RUN_QUEUE_MAX` the API answers 429 with `Retry-After`. A replay of the same `Idempotency-Key` is not new load and is never refused.

Capacity is set by the model quota, not by workers: with quota *R* calls/min and *c* calls per turn the system finishes at most *R/c* turns a minute. A worker running 4 turns of *d* seconds finishes 4×60/*d* a minute, so workers beyond ⌈(*R/c*) ÷ (4×60/*d*)⌉ only queue at the bucket. That number is KEDA's `maxReplicaCount`; the [load test](../evaluation/load-test.md) measures it.

## Kubernetes

[deploy/k8s](../../deploy/k8s) (Kustomize, verified on kind with `scripts/k8s-up.sh`): Postgres and Redis StatefulSets, the mock model, and an `api` and a `worker` Deployment per instance.

- API pods: startup and liveness on `/livez` (process only), readiness on `/readyz` (Redis and Postgres answer within a second, not draining), a 5 s preStop sleep so endpoints are removed before SIGTERM, and a 15 s uvicorn graceful-shutdown limit after which open streams are closed and clients resume elsewhere.
- Worker pods: a 120 s termination grace for the 90 s drain, and an init container that waits for Postgres and Redis.
- KEDA scales each worker Deployment with two `redis-streams` triggers on its instance's stream, pending (running) and lag (waiting), 4 per replica, between 1 and the quota-derived maximum. Lag alone would scale running workers away the moment every run was claimed.
- Instances stay apart: separate streams, per-instance stores (every query filters by instance) and separate worker Deployments, so the staff worker, which can query orders, never runs a customer's question.

## The event log on Kafka

`AI_HELPDESK_EVENT_SINK=kafka` (default `postgres`) sends the turn event log through Kafka instead of writing it straight into `turn_events`. It is the one part of the system shaped like a log: append-only, read by more than one consumer, worth replaying. Dispatch and frames stay on Redis (see the Kafka bullet under alternatives).

```
 worker / API ── EventWriter ──▶ topic ai-helpdesk.turn-events.<instance>   (key conv_id, 6 partitions, 7 days)
                                      │                         │
                       projector (consumer group)      sampler (consumer group per rules version)
                       → turn_events (Postgres)        → eval_candidates (structure only)
                         reads, retention, forget         export via replay_turns.py --mode export
```

- **One topic per instance**, keyed by `conv_id`: every attempt of a run is in one conversation, so its events and the `turn_abandoned` that closes a lost attempt arrive in order on one partition. Customer text and staff order data never share a topic.
- **Producer**: idempotent (`acks=all`), batched by the existing writer, connected lazily. Kafka being down never blocks startup or a turn; batches are retried with backoff, then dropped and counted (`kafka_error`), the same at-most-once contract the Postgres writer has. A message bigger than the broker allows is dropped alone.
- **Closing a turn is not left to chance.** The writer sends a batch as soon as it holds a turn-ending event, and a worker waits (at most 1 s) for the log to deliver a turn before it marks the run finished. Before that, a worker killed a fraction of a second after a successful run left its turn open forever; the [chaos drill](../evaluation/chaos-drill.md) found it.
- **Projector**: writes `turn_events`, committing offsets only after the write (at least once, plus the `(turn_id, seq)` primary key). Undecodable messages, the wrong instance, and rows Postgres rejects for their data go to a dead-letter topic with their origin; outages are retried, never skipped. Reads, the console and retention keep using Postgres.
- **Sampler**: stateful. It groups events by turn and, when a turn closes, records it as an evaluation candidate if it was handed off, unavailable, failed, aborted, abandoned or taken over, degraded, guard-replaced, a policy violation, low confidence, a fallback, or in a stable 5% sample. It commits each partition only up to the first message of its oldest open turn, so a restart rebuilds unfinished turns; only `turn_started` opens a turn, because after a restart it reads the tails of turns it already handled (the drill found that too). New rules get a new label and a new consumer group, which replays the 7-day window into a second set of candidates beside the first.
- **Privacy.** The topic keeps 7 days, shorter than the shortest body retention (customer: 14), so a replay never writes back text Postgres already cleared; the projector also strips text past retention. A deletion request records the conversation in `forgotten_conversations` before deleting, and the projector's insert skips it in the same statement, so resetting offsets cannot bring a forgotten conversation back. Kafka itself cannot delete one conversation: its copy goes when retention does. Crypto-shredding (a key per conversation, deleted on request) would remove it at once; not built.
- **Scaling.** KEDA scales the projector on consumer-group lag, at most to the partition count: a partition belongs to one consumer of a group at a time, which is exactly why dispatch is not on Kafka. The sampler runs one replica.
- **Not used**: Kafka transactions and exactly-once (idempotent writes are enough), a schema registry (the envelope carries a version, fields are only added; renaming bumps `v`), Kafka Streams.

## Alternatives considered

- **Kafka.** A log, not a work queue: consumers commit offsets per partition, so one 40-second turn holds up the partition behind it, parallelism is capped by the partition count, and processing longer than `max.poll.interval.ms` triggers a rebalance that re-delivers work (Kafka's share groups, KIP-932, exist to address this). The volume here is tens of turns a minute, and Redis was already deployed. Kafka fits the *event log* (turn events fanned out to several consumers that replay them), and that is where it is used (above), not for dispatch.
- **Postgres as the queue** (`SELECT … FOR UPDATE SKIP LOCKED`, `LISTEN/NOTIFY`). One system and no dual write, and a real option at this scale. Not chosen because claiming would be polling or notification plumbing on the database that also serves retrieval, and because Redis Streams give consumer groups, a pending list with idle-time reclaim and a KEDA scaler as is. The dual write is covered by the sweep.
- **Temporal or another durable-execution engine.** It would replay every step, model calls included, from its history. The cost is a new stateful service and rewriting the pipeline as workflow and activities. The commit point plus tool memoization is the narrow version of the same idea, and it keeps the pipeline as it is.
- **Celery, RQ, Arq.** They provide delivery and retries but not the two things a streamed agent turn needs: frames a client can resume from, and fencing so a worker that lost its lease cannot write a result.

## What it does not do

- Model calls are not replayed. A turn interrupted before its commit point is answered again, possibly with a different intent or wording; tools are replayed, the model is not.
- Working memory lives only in Redis; if Redis loses its data, conversations lose their short-term memory (runs and answers are not lost).
- Frames are best effort; the result is not.
- There is no cancel. Stopping the stream in the console stops reading; the run still finishes and is audited, which is the point of decoupling it from the connection.
- No priorities or per-tenant fairness in the queue; the system is single-tenant by design ([deployment boundaries](../deployment-boundaries.md)).
- The Kubernetes manifests run single-replica Postgres and Redis on kind. Production would use managed or replicated stores; nothing in the code assumes otherwise.
- Ticket webhooks are delivered at least once with an `Idempotency-Key`; the receiver has to honour it.

## Operating it

- Switch on: `docker compose -f docker-compose.yml -f docker-compose.queued.yml up -d --build`, or `scripts/k8s-up.sh`. Queued mode refuses to start without Postgres, Redis and `AI_HELPDESK_INSTANCE`.
- Watch: the *ai-helpdesk · 排队模式* Grafana dashboard (backlog, queue wait, first-sentence latency by mode, outcomes, takeovers and lost leases, degrade levels, token-bucket waits) and the `ai-helpdesk-runs` alert group (dead letters, backlog, queue wait, lost leases, sustained degradation, local overload).
- A dead-lettered run already has a handoff ticket; its per-attempt history is in `GET /runs/{run_id}` and in `agent_runs.history`.
- Practise: `backend/scripts/chaos_drill.py` against the compose drill stack, or `scripts/k8s-drill.sh` against kind; `loadtest/run.sh` for capacity.
