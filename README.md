<div align="center">

# AI Helpdesk

**Multi-agent customer support: routed specialist agents, delegation to a peer SQL agent over A2A, a guard on every sentence, and a person when confidence runs out.**

A FastAPI agent service: a router votes on each question's intent, tools run concurrently, specialist agents answer (in parallel when a question spans domains) and look up what is still missing in a bounded tool loop, and every sentence is checked before it is sent. A React console shows each agent decision as it happens.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/requirements.txt)
[![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](docker-compose.yml)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

**English** · [简体中文](README.zh-CN.md)

[Highlights](#highlights) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Features](#features) · [Develop](#develop)

</div>

<p align="center">
  <img src="docs/screenshots/chat.webp" width="960" alt="Three questions in the AI Helpdesk console with Learning mode on: a cracked-countertop question is answered by two specialist agents in parallel after a corrective search, with an unsupported date promise replaced; an order-status question is delegated to the SQL agent over A2A, whose table search, planning, queries and repair stream into the console; and for a split cabinet door, where the rules fetch no order data, the specialist calls tools itself in the bounded loop, querying the business systems for the delivery date and searching the knowledge base again, and Learning mode lists both lookups" />
</p>

<p align="center"><sub>A made-up company with made-up orders and documents, served by a mocked API.</sub></p>

## Highlights

- **A router and specialist agents.** An LLM, embeddings and rules vote (0.7 / 0.2 / 0.1) on 19 intents, 77 of 90 labelled messages right with gpt-6-luna. Domain scores pick one of three specialists, run as replicas a monitor re-weights; a second joins in parallel when it clears 0.45 and 55% of the first.
- **A bounded tool loop, switched on by an A/B.** When the prefetched material falls short, a specialist calls the same search and data lookup itself through function calling, at most three model calls and one delegation a turn. In 72 paired runs, questions whose data the rules miss got it in 26 of 30 runs instead of none, with no forbidden calls or invented figures, so the staff instance runs it.
- **Agent-to-agent delegation.** Data questions go to a separate SQL agent that plans, writes, runs and repairs its own queries. Over A2A it streams each stage back, with Agent Card discovery, fallbacks, timeouts and contract tests pinned at `v1.13`.
- **Trust between agents fails closed.** A delegated result is evidence only with a success or partial-success status and no risk or degraded flag; a missing field counts against it. Only evidence, retrieved passages and SOPs can ground a price or a date.
- **Retrieval with a corrective loop.** Three sub-queries over pgvector and a keyword index, a cross-encoder or LLM rerank with a check on whether the evidence is enough, and one more round for what is missing. The fused search alone finds the right document in the top three for 101 of 108 test questions.
- **Guardrails and a human in the loop.** Every sentence is checked before it is sent, and unsupported price or date promises are replaced. Specialists declare when a person is needed, low confidence opens a ticket, and a model outage pauses calls instead of opening tickets.
- **Turns that outlive their worker.** In queued mode a turn is a run in Postgres, executed by workers that hold a fenced lease and record the answer before any side effect; tickets, audit and memory are keyed by the run, and clients resume the stream with `Last-Event-ID`. In five chaos drills, 340 runs went through worker kills, Redis restarts and a Redis flush with no second ticket, audit line or answer; put to the same dropped connections and API restart, the old inline mode lost 53% of turns and queued mode none.
- **An event log on Kafka, if you want one.** Turn events can go through Kafka to a projector and an evaluation sampler, each with its own offsets. The projection rebuilds from the topic, deletion requests hold through replays, and drills that kill both consumers and restart the broker match every event in Kafka to a row in Postgres. One projector writes about 21,000 events a second, and the detour adds about 24 ms to an event.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" width="844" alt="Agent architecture: one question runs through five stages in the agent runtime, posted by the React 18 console with the answer streamed back over SSE. 1 Understand: an intent vote typed with Pydantic 2, with working memory in Redis 7. 2 Gather: tools picked by rules and run together with asyncio.gather, a search in PostgreSQL 16 with pgvector and tsvector and a delegation over A2A to the peer agent ai-sql-agent, which runs its own loop; thin evidence loops back for one corrective round. 3 Route: domain scores pick the specialists, and a monitor re-weights their replicas every 10 seconds. 4 Answer: three specialists on the Anthropic or OpenAI chat models, falling back to a generalist; in a bounded tool loop a specialist can search or query again through function calling. 5 Check: a guard and three confidence gates; a failed gate hands off to a person with a ticket. In queued mode, opt-in on Docker Compose and on by default in the Kubernetes manifests, the run waits on Redis Streams for workers that hold a fenced lease on it; when worker w1 is lost after the delegation, w2 takes over as attempt 2 and the console resumes the SSE stream with Last-Event-ID. Every step is also appended, in order, to a turn event log (intent, prefetch, routing, model calls, tool calls, guard, answer and handoff), written to PostgreSQL in batches off the answer path, or, with the same defaults, through Kafka to a projector and a sampler. Vectors and reranking come from an embedding API, and the platform is FastAPI, Python 3.12, httpx, Docker Compose, Kubernetes, KEDA, Nginx, OpenTelemetry and Grafana; pulses travel each connection in that order" />
</p>

A question reaches the staff instance from the console, or the customer instance through Nginx on port 80, and runs through five stages. **Understand** loads memory and votes on the intent. **Gather** runs the tools the rules pick for it, at the same time: a hybrid search in PostgreSQL and, on the staff instance, a lookup delegated to [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent) over A2A or its HTTP `/query`. **Route** scores the specialists and picks a replica, **Answer** runs them with their own SOPs and, on the staff instance, lets them look up what is still missing, and **Check** tests each sentence on the way out, or opens a ticket and queues its webhook in an outbox. In queued mode the same five stages run on a worker that holds a fenced lease on the run: if it dies, another worker takes over as attempt 2 and the console resumes the stream with `Last-Event-ID`; the event log can also go through Kafka ([Durable runs](#durable-runs)).

- **Rules fetch first; specialists fill the gaps.** Search and data lookups are chosen from the voted intent and its entities before any agent runs, and both run at once. With the tool loop switched on, a specialist that finds the material lacking calls the same tools itself, within a budget; with it off, a turn costs a predictable number of model calls. The open-ended loop, planning and repairing SQL, lives in the SQL agent behind a pinned contract.
- **Two entrances, one image.** The customer instance never delegates, never runs the tool loop and keeps no cross-session memory, since its user IDs come from the request body; only Nginx on port 80 accepts outside connections.

## Tech stack

| Layer                | Technology                                                                       | Role                                                                                                                                                                                                                                           |
| -------------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent runtime        | Python 3.12 · FastAPI · asyncio                                                  | FastAPI serves chat over SSE; asyncio runs the prefetch, parallel specialists and each tool-loop step's calls with `gather`, keeps blocking store calls off the event loop, and pumps progress into the stream                                 |
| Model calls          | Anthropic SDK · OpenAI SDK · Pydantic 2                                          | One interface over both providers, switchable at runtime, with function calling translated both ways; every model decision (intent vote, query rewrite, rerank verdict, user profile, judge scores) is a Pydantic schema, validated before use |
| Agent protocol       | A2A JSON-RPC (`message/stream`, `message/send`) · Agent Card · httpx             | Delegation to the SQL agent with streamed stages, fallbacks and timeouts; plain HTTP `/query` as the default transport                                                                                                                         |
| Embeddings, rerank   | OpenAI-compatible `/embeddings` · local n-gram vectors · cross-encoder `/rerank` | One embedder for knowledge, episodic memory and the intent vote, with local vectors when no API is set; a cross-encoder rerank with the LLM as fallback                                                                                        |
| Retrieval store      | PostgreSQL 16 · pgvector · `tsvector` · asyncpg · SQLite                         | Dense and lexical legs fused by rank; SQLite with BM25 as a local fallback                                                                                                                                                                     |
| Memory, shared state | Redis 7                                                                          | 24-hour working memory and its summarisation lock, per-user rate limits, and circuit breakers and routing penalties shared across instances; in queued mode, run dispatch and resumable frames (Streams) and the model token bucket (Lua)     |
| Event streaming      | Apache Kafka 4.3 (KRaft) · aiokafka                                              | Optional transport for the turn event log: a topic per instance keyed by conversation, a projector and an evaluation sampler as separate consumer groups, a dead-letter topic, a 7-day replay window |
| Guardrails, identity | Rule-based answer policy · confidence gates · HMAC user tokens                   | Sentence-level checks, handoff decisions and signed user identities                                                                                                                                                                            |
| Console              | React 18 · TypeScript · Vite 6 · TanStack Query 5 · Tailwind CSS 4               | SSE read from a `fetch` stream (EventSource can't POST), streamed Markdown and Learning mode, in English and Chinese                                                                                                                           |
| Observability        | OpenTelemetry · Prometheus · Grafana · Tempo · Loki                              | Traces per request, metrics, logs and alert rules, next to the monitor's Z-score anomaly checks                                                                                                                                                |
| Delivery, quality    | Docker Compose · Nginx · Kubernetes · KEDA · GitHub Actions · pytest · Ruff · Vitest · k6 | 12 services in one deploy, or Kustomize manifests with KEDA-scaled workers; CI runs the tests against pgvector, Redis and Kafka, the A2A contract fixtures and both image builds; k6 and a chaos drill for queued mode                     |

## Features

### Agent orchestration

A streamed turn arrives as `progress → meta → routing → delta → result`; the console shows each stage live, and Learning mode explains every decision.

- **Routing.** A rule anchor fixes the intent without a model call and skips rewriting and reranking; conflicting anchors fall back to the full vote, and unclear questions get a clarifying reply.
- **Replicas and feedback.** Each specialist type runs two replicas. A monitor tracks success rate and latency, flags Z-score anomalies, and penalises a replica by up to 0.9 when success drops below 90% or it slows, steering traffic to the healthier one.
- **Collaboration and fallback.** One specialist's answer streams sentence by sentence; parallel answers are merged, each marked with its role. A failing specialist is replaced by the generalist, except mid-stream, where the turn is reported as not answered.
- **Tool loop.** With `AI_HELPDESK_TOOL_LOOP=on`, as on the staff instance here, a specialist calls `knowledge_search` and `order_data_query` through function calling: at most three model calls and one delegation per specialist and turn, identity injected by the server, no data lookups for quotes, repeats of the prefetch served from the turn's cache, a wall clock sized to the slowest tool, and no second delegation once one has timed out. Failures go back to the model as tool results; calls stream as progress, and Learning mode lists each one with its outcome and time.
- **Turn event log.** With `AI_HELPDESK_EVENT_LOG=full`, each turn is appended to Postgres as ordered events: intent, prefetch, routing, every model call by purpose with its tokens, every tool call, guard replacements, handoff and the answer. Writes are batched off the answer path, text expires before structure, and the two instances stay apart. After a refresh the console rebuilds a turn's inspector and Learning mode from it, and offline scripts replay routing and the answer guard on recorded traffic.
- **Self-declared handoff.** Each specialist ends with a marker saying whether a person is needed, stripped from the stream; declaring one drops its confidence below the 0.70 gate.
- **Memory and skills.** Working memory lives in Redis for 24 hours and is summarised at 15 messages; past sessions and a user profile return to the context. Each specialist loads its own SOPs from reloadable `SKILL.md` files.

| One run with gpt-6-luna             | Result                                                                            |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| Intent vote on 90 labelled messages | 77 correct (85.6%), macro-F1 0.798                                                |
| LLM-judged dialog turns             | 14 of the 15 that completed pass; 1 of 16 was cut by an upstream connection error |

The judge is the model under test and the cases are in Chinese ([evaluation record](docs/evaluation/end-to-end.md)). The bundled intents, specialists, SOPs and knowledge describe a manufacturer's support desk; another domain needs its own intents and specialists in code.

| Tool-loop A/B with gpt-6-luna, 72 paired runs              | Rules |  Loop |
| ---------------------------------------------------------- | ----: | ----: |
| Data the rules miss reached the specialist (B, 30 runs)    |     0 |    26 |
| Unneeded calls where the rules sufficed (A and D, 18 runs) |     — |     1 |
| Invented figures or followed injections (E and F)          |     0 |     0 |
| Handoffs (72 turns)                                        |    46 |    39 |
| Mean input tokens per turn                                 | 2,692 | 4,915 |
| LLM-judge score (A to C)                                   | 0.899 | 0.896 |

The second of two runs: the first missed two of the ten conditions for switching it on, and what changed in between is in the [evaluation record](docs/evaluation/tool-loop-ab.md). Order data came from contract fixtures, so the latency leaves out a live delegation's 17–40 s.

### Agent-to-agent delegation

Data questions on the staff instance go to [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent), which measures itself with a release gate. `SQL_AGENT_TRANSPORT=a2a` streams its stages over `message/stream`; the default, `query`, calls its synchronous HTTP `/query`.

- **Streaming with fallbacks.** The Agent Card is cached for 30 s, and each stage (table search, planning, SQL, queries, repair) reaches the console in the interface language. A `-32004` before the stream opens switches to `message/send`; silence or a hard deadline ends the call.
- **Evidence rules on every carrier.** One check covers A2A and `/query` results; a partial result also limits what may be said about failed steps, and a truncated one rules out totals.
- **One contract.** Consumer tests run the golden fixtures of [a2a-contract](https://github.com/heyemlee/a2a-contract), pinned at `v1.13`, and fail rather than skip when they are missing.

### Durable runs

`AI_HELPDESK_RUN_MODE=queued` takes a turn off the connection that asked for it ([design](docs/design/durable-agent-runs.md)). The API writes the run to Postgres and dispatches its id on a per-instance Redis stream; workers, the same image with `AI_HELPDESK_ROLE=worker`, claim it and run the unchanged pipeline, and every SSE frame goes to a per-run stream the client can resume. Inline mode, the default, is untouched.

- **One source of truth.** Run state, leases and results live in `agent_runs`; Redis streams carry dispatch and frames and can be rebuilt, because a sweep re-dispatches whatever Postgres says nobody is running.
- **Leases with a fence.** Workers renew a lease every third of its length; another worker takes over an idle message with `XAUTOCLAIM` and `attempt + 1`. A worker that lost its lease can no longer write, and its late frames are dropped.
- **Decided once, applied once.** The answer is recorded before any side effect, so a takeover after that point resends it without calling the model. Ticket ids derive from the run, audit and working memory are written once per run, and tool results are memoized so a retry does not repeat a 40 s delegation. Infrastructure errors are retried; runs that keep failing go to a dead letter and a person.
- **Load is admitted, not absorbed.** A token bucket in Redis, shared by every replica, admits model calls by provider RPM and TPM; a backlog-driven ladder turns off the tool loop, then rewrite and rerank, then delegation, then model calls; above the cap the API answers 429 with `Retry-After`.
- **Ready for Kubernetes.** Liveness, readiness and drain on SIGTERM; [manifests](deploy/k8s) with KEDA scaling workers on the stream backlog, verified on kind.
- **The event log on Kafka (optional).** With `AI_HELPDESK_EVENT_SINK=kafka`, turn events go to a topic per instance keyed by conversation. A projector writes `turn_events` and commits only after writing; a sampler records turns worth evaluating (handoffs, takeovers, degraded or low-confidence answers, a stable 5% sample), holding its offsets at its oldest open turn. The topic keeps 7 days, less than any text retention, and replays skip conversations removed by deletion requests. Dispatch stays on Redis: one 40 s turn would hold up a Kafka partition.

In five chaos drills on Compose and kind (worker kills, Redis restarts and a flush, a storm of 429s, deploys), all 340 runs finished and every check against a second ticket, audit line or answer passed. A killed worker's runs were taken over and finished at most 42 s after submission; deploys took none over. Put to the same dropped connections and API restart, inline mode lost 64 of 120 turns, every one whose stream was cut, and queued mode none. Two more drills with the event log on Kafka (both consumers SIGKILLed, the broker restarted) matched every event in Kafka to a row in `turn_events`, closed every turn, sampled exactly what the rules recompute to, and rebuilt the projection from the topic with forgotten conversations staying gone. One projector writes about 21,000 events a second into Postgres (three, about 53,000), and the detour through Kafka adds about 24 ms to an event.

Against a mock model with a quota of 300 requests a minute, two workers already reach what the quota allows (97 turns a minute; four finished 103.5). Without the token bucket the same overload looked faster only because 27% of turns were answered "can't answer right now" after 718 upstream 429s; with the bucket at 85% of the quota the model returned none. At low load the queue adds nothing measurable: first sentence p50 1.006 s inline, 1.005 s queued. All of it ran on one laptop with fixed model latencies, so the shape carries over and the absolute numbers do not. Records: [chaos drill](docs/evaluation/chaos-drill.md), [load test](docs/evaluation/load-test.md).

### Retrieval

- **Two legs, fused by rank.** Dense (pgvector) and lexical (`ts_rank_cd`, or BM25 on SQLite) legs, ten candidates each, fused by rank rather than by incompatible scores; with local n-gram vectors, keyword matches lead.
- **Rewrite, rerank, retry.** Complex questions become three sub-queries. A cross-encoder `/rerank` API orders the candidates when one is set, with a score threshold for sufficiency; otherwise an LLM reranks and judges sufficiency in the same call. Too little evidence triggers one more round aimed at the gap.
- **One embedder.** Knowledge, episodic memory and the intent vote share an OpenAI-compatible `/embeddings` API, or local n-gram vectors when none is set. A failing API is never swapped for vectors from another space, and switching models re-embeds everything while the service keeps answering.
- **A cache with a version.** Each instance caches search results for five minutes, tagged with a revision that the database changes on every chunk write. The two instances share one database but not one process, so an import or a vector swap on either one retires the cached results on both.

Top-three hits on PostgreSQL by question type, for the fused search alone (no rewriting, reranking or answer generation):

| Question type                  |   Cases | Local n-gram vectors | Qwen3-Embedding-0.6B |
| ------------------------------ | ------: | -------------------: | -------------------: |
| Wording close to the document  |      35 |                   31 |                   33 |
| Colloquial paraphrase          |      30 |                   22 |                   28 |
| Model codes and proper names   |      11 |                   11 |                   11 |
| English over Chinese documents |      22 |                    7 |                   21 |
| Two documents needed           |       6 |                    3 |                    4 |
| Current vs. superseded version |       4 |                    3 |                    4 |
| **All**                        | **108** |               **77** |              **101** |

The 52 documents and 122 questions (14 with no answer) are simulated test data, and even with the remote model a superseded document outranks the current one in 4 of 6 cases. Records: [local vectors](docs/evaluation/retrieval-ca-factory.md), [remote embedding](docs/evaluation/retrieval-remote-embedding.md).

### Guardrails and handoff

- **Answer guard.** A sentence with an amount or a time, a commitment verb, no hedge and no source is replaced with one deferring to the quote or schedule; a date counts as sourced when a source has the same calendar day in any format. Staff see the original and the rule.
- **Gates and tickets.** Intent < 0.45, routing < 0.35 or agent < 0.70 hands off; these are heuristic signals, not calibrated probabilities. Tickets have four states, and notifications go through an outbox with retries.
- **Failing closed.** A rejected key stops calls for that configuration until reset; a 429 backs off 5–60 s and an unexplained 403 30–600 s, honouring `retry-after` up to 900 s.

### Knowledge base, tickets and admin

<p align="center">
  <img src="docs/screenshots/workspace.webp" width="960" alt="Browsing a knowledge category and asking the knowledge base a question that returns three ranked passages, opening a ticket and marking it assigned, then switching the embedding model, which rebuilds 312 vectors with a progress bar before it cuts over" />
</p>

<p align="center"><sub>The same made-up company and mocked API.</sub></p>

**Knowledge base**

- Browse by category; recategorising never touches retrieval.
- **Ask a question** shows the passages and scores the chat path would retrieve.
- Import `.txt`, `.md` or JSON; identical text is not imported twice.

**Tickets**

- Filter by status; only transitions the backend allows are offered.
- Each shows the handoff reason, the conversation and the notification record.
- The ticket card in chat links to the ticket and follows its status.

**Admin & diagnostics**

- Model connections are tested before they are saved; keys are write-only.
- An LLM-as-judge evaluation whose baseline changes only on an explicit promote.
- Monitoring, SOPs, and an inspector for any reply's routing scores and policy findings.

## Develop

```bash
cp .env.example .env                  # Compose variables: database, Redis and Grafana passwords
cp backend/.env.example backend/.env  # set APP_ENV=development, a model key and SQL_AGENT_TRANSPORT=a2a
docker compose up -d --build          # the full stack; console at http://localhost:5174
scripts/dev.sh                        # or backend and console from source, at http://localhost:5173
scripts/dev-all.sh                    # the same, plus ai-sql-agent from a sibling checkout
```

Configuration, model providers, knowledge import, setup without Docker and the API: [docs/setup.md](docs/setup.md).

Queued mode needs Postgres, Redis and `AI_HELPDESK_INSTANCE`; the drill stack has all three and a mock model:

```bash
docker compose -f deploy/drill/docker-compose.yml up -d --build --wait   # API :28080, three workers, mock model
```

## Test

```bash
scripts/ci-local.sh                         # the CI checks; --with-images adds both image builds
cd backend && .venv/bin/python -m pytest    # backend tests; TEST_POSTGRES_DSN adds the pgvector ones
.venv/bin/python scripts/eval_retrieval.py  # retrieval hit@3 and MRR, no model calls
.venv/bin/python scripts/run_eval.py        # end-to-end evaluation; calls the model
.venv/bin/python scripts/run_trajectory_eval.py  # tool loop vs rules on 40 trajectory cases; calls the model with --yes
cd ../frontend && npm test                  # console tests
backend/.venv/bin/python backend/scripts/chaos_drill.py --kills 3 --restart-redis --storm 20 --slow  # queued mode under failure
backend/.venv/bin/python backend/scripts/chaos_drill.py --kafka --kill-projector 2 --kill-sampler 2 --restart-kafka --replay --slow  # the Kafka side
loadtest/run.sh <name>                      # k6 against the drill stack; WORKERS, LLM_RPM, DEGRADE, MOCK_RPM
```

CI runs Ruff, 1,531 backend tests against pgvector, Redis and Kafka, 255 console tests, the production build, the A2A contract fixtures and both Docker images. The end-to-end and trajectory evaluations spend model calls, so they run only on request.

## Deploy

```bash
docker compose up -d --build                    # 12 services; only Nginx :80 listens beyond localhost
scripts/docker-deploy.sh backup                 # Postgres, Redis, env files and deploy/ config
scripts/docker-deploy.sh restore backups/<dir>  # restore and restart; a drill took 17 s
docker compose -f docker-compose.yml -f docker-compose.queued.yml up -d --build   # queued mode: api + worker per instance
docker compose -f docker-compose.yml -f docker-compose.kafka.yml up -d --build    # event log on Kafka (queued too: see docker-compose.queued-kafka.yml)
scripts/k8s-up.sh                               # kind cluster: manifests, KEDA, Kafka, mock model
```

In production mode the service refuses to start without the admin key, the user-signing secret, chat protection and a declared deployment scope. It is single-tenant by design; [deployment boundaries](docs/deployment-boundaries.md) explains what that covers.
