# ai_sql_agent.runs

One request is one *run*. A run has:

- a record with its status, entry, usage and outcome;
- an event log numbered from 1;
- a cancel token.

The pipeline itself does not know about runs. The layers it already passes through emit the events and check the token:
`trace.stage()`, the model transport and the SQL router. Library calls (benchmark, examples, scripts) have no run, and
every hook is then a no-op.

## Layout

```text
src/ai_sql_agent/core/run_context.py   RunContext, CancelToken, RunCancelled, checkpoints (used by every layer)
src/ai_sql_agent/runs/
├── events.py       Event types, envelope, size caps
├── store.py        SQLite / PostgreSQL / null stores (ai_sql_runs, ai_sql_run_events)
├── manager.py      Start, finish, cancel, orphan sweep, retention
└── disconnect.py   Watch for a caller that went away (behind AI_SQL_CANCEL_ON_DISCONNECT)
src/ai_sql_agent/api/runs.py           GET /runs/{id}, GET /runs/{id}/events, POST /runs/{id}/cancel
```

## Events

| Type | Emitted by | Attributes |
| --- | --- | --- |
| `run.started` | entry | entry (`http_query` / `a2a_send` / `a2a_stream`), caller, user, session, question length |
| `stage.started` | `trace.stage()` on entry | the stage's entry fields (step, database, dialect …) |
| `stage.finished` | `trace.stage()` on exit | status `ok` / `error` / `cancelled`, duration, the stage's recorded fields |
| `model.call` | model transport, per attempt | component, model, attempt, status, duration, tokens, error category |
| `tool.sql` | SQL router, per execution | database, status, rows, truncated, duration, SQL (≤ 8 KiB), error |
| `run.cancel_requested` | `RunManager.cancel` | reason: `api`, `client_disconnected`, `deadline`, `shutdown` |
| `run.finished` | entry | status `completed` / `failed` / `cancelled` / `interrupted`, usage, outcome |

- **Position.** Each event carries the innermost stage it happened in and that stage's step number. This is how a
  `tool.sql` event shows whether it was the first execution, a repair or a degraded replay.
- **What is never stored.** Prompts, model output and result rows. Strings are cut at 1 KiB (SQL at 8 KiB), and one
  event's attributes at 16 KiB (`AI_SQL_RUN_EVENT_ATTR_MAX_BYTES`).
- **When events are written.** They are buffered and written when a stage finishes, on a cancel request and when the
  run finishes. A crash loses at most the current stage's events.
- **When the store fails.** That run stops persisting and `ai_sql_run_events_dropped_total` counts the loss. The
  query is not affected.

A2A progress frames do not come from this log. They still come from the `trace.stage()` sink, so the contract's wire
format is unchanged.

## Cancellation

`RunCancelled` derives from `Exception` directly. It must not be a `RuntimeError` (routing would fall back to rules)
or a `ModelCallError` (the pipeline would degrade and replay history).

| Checkpoint | Where | Latency |
| --- | --- | --- |
| Stage entry | `trace.stage()` | when the current stage ends |
| Before each model attempt; retry backoff | `core/transport.py` | immediate |
| Model call already in flight | — | not interruptible: at most the HTTP timeout, result discarded |
| SQL running | SQLite progress handler; PostgreSQL `cancel_safe()` | immediate |
| Each repair attempt; each degraded replay step | `repair/repairer.py`; `_replay_history` | immediate |

Three places used to swallow exceptions with side effects, and now let a cancel through:

- the keyword extractor (one failure switched it to rules for the life of the process);
- the remote embedding client (a failure starts a 30 s cooldown);
- the semantic self-check (it would report "not run" and hand the answer out).

Where a cancel comes from:

- the console's stop button: it takes the task id from the first stream frame and calls `POST /runs/{id}/cancel`
  before it aborts the request;
- `POST /runs/{id}/cancel` itself;
- a caller disconnecting (`AI_SQL_CANCEL_ON_DISCONNECT`, on by default since A2A contract v1.13);
- the run deadline (`AI_SQL_RUN_DEADLINE_SECONDS`, off by default);
- shutdown.

A cancelled request is still audited, with status 499, and still counts toward the daily quota (A2A contract §5.5).
On A2A, a run cancelled while the caller is still connected ends in the `canceled` state, on the stream's final
frame and on the task that `message/send` returns (contract §4.4).

## Verifying

- `tests/test_runs.py` and `tests/test_run_cancel.py` cover the store, the manager, the event order, the HTTP surface,
  the checkpoints and the swallow sites.
- `tests/test_prompt_snapshot.py` shows that every model call on a first run receives exactly what it received before
  this layer existed.
- `tests/test_a2a_wire_snapshot.py` shows the A2A frames, the `message/send` task and the `/query` data part keep their
  shape.
- The PostgreSQL store and cancel path run only where a DSN is configured. CI configures one (a PostgreSQL 16 +
  pgvector service), so they run there.
- Cancel latency under a real model was measured once, on 2026-10-16, on a local instance with demo data: six cancels
  sent 0.5–11 s into an A2A stream ended their runs 0.07–2.7 s later, each 3–16 ms after the model or embedding call
  already in flight returned. No call started after a cancel.
