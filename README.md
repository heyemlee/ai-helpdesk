<div align="center">

# AI Helpdesk

**Multi-agent customer support: routed specialist agents, a peer SQL agent over A2A, a guard on every sentence, and a person when confidence runs out.**

A FastAPI agent service routes each question through an intent vote, runs its tools concurrently (hybrid knowledge search with a corrective retry, and data lookups delegated to a separate SQL agent over the A2A protocol), lets specialist agents answer, in parallel when a question spans domains, and checks every sentence before it streams. A React console shows each agent decision as it happens.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/requirements.txt)
[![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](docker-compose.yml)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

**English** · [简体中文](README.zh-CN.md)

[Highlights](#highlights) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Features](#features) · [Develop](#develop)

</div>

<p align="center">
  <img src="docs/screenshots/chat.webp" width="960" alt="Three questions in the AI Helpdesk console with Learning mode on: a cracked-countertop question is answered by two specialist agents in parallel after a corrective search, with an unsupported date promise replaced; an order-status question is delegated to the SQL agent, whose table search, planning, queries and repair stream into the console; and a request for a person opens a ticket whose notification is delivered" />
</p>

<p align="center"><sub>A made-up company with made-up orders and documents, served by a mocked API.</sub></p>

## Highlights

- **A router and parallel specialists.** An LLM, embeddings and rules vote (0.7 / 0.2 / 0.1) on 19 intents, 77 of 90 labelled messages right with gpt-6-luna. Domain scores pick one of three specialist agents; a second runs in parallel when it scores at least 0.45 and 55% of the first, and a generalist covers failures.
- **Agent-to-agent delegation.** Data questions go to a separate SQL agent that plans, writes, runs and repairs its own queries, streaming each stage back over A2A. Agent Card discovery, a `message/send` fallback, silence and deadline timeouts, and contract tests pinned at `v1.12` in CI.
- **Trust between agents fails closed.** A delegated result is evidence only with a success or partial-success status and no risk or degraded flag; a missing field counts against it. Only evidence, retrieved passages and SOPs can ground a price or a date.
- **Retrieval with a corrective loop.** Three sub-queries over pgvector and a keyword index, an LLM rerank that judges whether the evidence is enough, and one more round for what is missing: the right document is in the top three for 101 of 108 test questions.
- **Guardrails and a human in the loop.** Answers stream a sentence at a time, and a rule-based guard replaces unsupported price or date promises before they are sent. Low confidence opens a ticket; a model outage stops calls and opens none.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="The staff console and customer clients reach an agent service deployed with Docker Compose as a staff and a customer instance; a router votes on the intent, tools run concurrently (knowledge search, and an order lookup delegated over A2A to a peer SQL agent that plans, writes, runs and repairs its own queries), specialist agents answer, in parallel across domains, and an answer guard with confidence gates returns a reply or opens a handoff ticket; Redis holds working memory, PostgreSQL with pgvector holds knowledge, memory, tickets and audit records, and telemetry goes to Tempo, Prometheus, Loki and Grafana" />
</p>

A question reaches the staff instance from the console, or the customer instance through Nginx on port 80. The service loads working memory from Redis and runs the router; the tools it picks run concurrently, a hybrid search in PostgreSQL and, on the staff instance, a lookup delegated to [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent) over A2A. Specialist agents answer with their own SOPs, and the guard checks each sentence before it streams back, or a ticket is opened and its webhook queued in an outbox.

- **Rules pick the tools; models write the answers.** Whether to search or query data is decided from the voted intent and its entities before any agent runs, so a turn costs a predictable number of model calls and both lookups run at once. The agents don't call tools themselves; the open-ended loop, planning and repairing SQL, lives in the SQL agent behind a pinned contract.
- **Two entrances, one image.** The customer instance never delegates to the SQL agent and keeps no memory across sessions, because its user IDs come from the request body and a guessed staff ID would otherwise pull in staff history. Only Nginx on port 80 accepts connections from other machines.

## Tech stack

| Layer          | Technology                                                                   | Role                                                                                           |
| -------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Agent service  | Python 3.12 · FastAPI · Pydantic 2 · asyncio                                 | Router, concurrent tools, parallel specialists, fallback and handoff, SSE streaming            |
| Models         | Anthropic SDK · OpenAI SDK                                                   | Intent vote, query rewriting, reranking, specialist answers, LLM-as-judge; switchable live     |
| Agent protocol | A2A over JSON-RPC (`message/stream`, `message/send`) · Agent Card            | Delegation to the SQL agent, with streamed progress and a contract pinned in CI                |
| Retrieval      | PostgreSQL 16 · pgvector · `tsvector` · BM25 · RRF                           | Hybrid search over knowledge passages; SQLite with BM25 as a local fallback                    |
| Memory, state  | Redis 7 · asyncpg                                                            | Working memory, summaries and user profiles; rate limits and shared circuit-breaker state      |
| Guardrails     | Rule-based answer policy · confidence gates · HMAC user tokens               | Sentence-level checks, handoff decisions, signed identities                                    |
| Console        | React 18 · TypeScript · Vite 6 · TanStack Query 5 · Tailwind CSS 4           | Chat with per-turn agent explanations, knowledge, tickets and model settings, in two languages |
| Operations     | Docker Compose · Nginx · OpenTelemetry · Prometheus · Grafana · Tempo · Loki | 12 services in one deploy, with traces, metrics, logs and alert rules                          |
| Quality        | pytest · Ruff · Vitest · Testing Library · GitHub Actions                    | Unit, contract and image checks in CI; retrieval and end-to-end evaluations                    |

## Features

### Agent orchestration

A streamed turn arrives as `progress → meta → routing → delta → result`: the console shows each stage live, and Learning mode explains every decision.

- **Routing.** A rule anchor fixes the intent without a model call and skips rewriting and reranking; conflicting anchors fall back to the full vote, and unclear questions get a clarifying reply.
- **Collaboration and fallback.** Parallel specialists' answers are merged, each marked with its role. A failing specialist is replaced by the generalist, except mid-stream, where sent text can't be recalled and the turn is reported as not answered.
- **Memory and skills.** Working memory stays in Redis for 24 hours and is summarised by the model at 15 messages; past sessions and a user profile return to the context. Each specialist loads its own SOPs from reloadable `SKILL.md` files.

| One run with gpt-6-luna             | Result                                                                            |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| Intent vote on 90 labelled messages | 77 correct (85.6%), macro-F1 0.798                                                |
| LLM-judged dialog turns             | 14 of the 15 that completed pass; 1 of 16 was cut by an upstream connection error |

The judge is the model under test and the cases are in Chinese; see the [evaluation record](docs/evaluation/end-to-end.md). The bundled intents, specialists, SOPs and knowledge describe a manufacturer's support desk; another domain needs its own intents and specialists in code, not only new documents.

### Agent-to-agent delegation

Data questions on the staff instance go to [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent), which measures itself with a release gate.

- **Streaming with fallbacks.** The Agent Card is cached for 30 s, and each stage (table search, planning, SQL, queries, repair) reaches the console in the interface language. A `-32004` before the stream opens switches to `message/send`; silence or a hard deadline ends the call.
- **Evidence rules.** Beyond the status and flags, a partial result limits what may be said about the failed steps, and a truncated one rules out totals.
- **One contract.** Consumer tests run the golden fixtures of [a2a-contract](https://github.com/heyemlee/a2a-contract), pinned at `v1.12`, and fail rather than skip when they are missing.

### Retrieval

- **Two legs, fused by rank.** Dense (pgvector) and lexical (`ts_rank_cd`, or BM25 over Chinese bigrams on SQLite), ten candidates each, fused by rank rather than by incompatible scores. With local n-gram vectors, keyword matches lead and vectors fill in.
- **Model switches without downtime.** A new embedding model re-embeds every stored vector while the service keeps answering, then cuts over; vectors from another model stop the service from starting.

Top-three hits on PostgreSQL, by question type:

| Question type                  |   Cases | Local n-gram vectors | Qwen3-Embedding-0.6B |
| ------------------------------ | ------: | -------------------: | -------------------: |
| Wording close to the document  |      35 |                   31 |                   33 |
| Colloquial paraphrase          |      30 |                   22 |                   28 |
| Model codes and proper names   |      11 |                   11 |                   11 |
| English over Chinese documents |      22 |                    7 |                   21 |
| Two documents needed           |       6 |                    3 |                    4 |
| Current vs. superseded version |       4 |                    3 |                    4 |
| **All**                        | **108** |               **77** |              **101** |

The 52 documents and 122 questions (14 with no answer) are simulated test data. Even with the remote model, a superseded document outranks the current one in 4 of 6 cases. Records: [local vectors](docs/evaluation/retrieval-ca-factory.md), [remote embedding](docs/evaluation/retrieval-remote-embedding.md).

### Guardrails and handoff

- **Answer guard.** A sentence with an amount or a time, a commitment verb, no hedge and no source is replaced with one that defers to the quote or the schedule; staff see the original and the rule that caught it.
- **Gates and tickets.** Intent < 0.45, routing < 0.35 or agent < 0.70 hands off; these are heuristic signals, not calibrated probabilities. Tickets move through four states, and notifications go through an outbox with retries.
- **Failing closed.** A rejected key stops calls for that configuration until reset; a 429 backs off 5–60 s and an unexplained 403 30–600 s, honouring `retry-after` up to 900 s.

### Knowledge base, tickets and admin

<p align="center">
  <img src="docs/screenshots/workspace.webp" width="960" alt="Browsing a knowledge category and asking the knowledge base a question that returns three ranked passages, opening a ticket and marking it assigned, then switching the embedding model, which rebuilds 312 vectors with a progress bar before it cuts over" />
</p>

<p align="center"><sub>The same made-up company and mocked API.</sub></p>

**Knowledge base**

- Browse by fixed category; recategorising a document never touches retrieval.
- **Ask a question** shows the passages and scores the chat path would retrieve.
- Import `.txt`, `.md` or JSON document lists; identical text is not imported twice.

**Tickets**

- Filter by status; only the transitions the backend's state machine allows are offered.
- Each ticket shows the handoff reason, the conversation and the notification's delivery record.
- The ticket card in chat links to the ticket and follows its status.

**Admin & diagnostics**

- Model connections are tested before they are saved; keys are write-only.
- An LLM-as-judge evaluation whose baseline changes only on an explicit promote.
- Monitoring, SOPs and an inspector for any reply's routing scores and policy findings.

## Develop

```bash
cp .env.example .env                  # Compose variables: database, Redis and Grafana passwords
cp backend/.env.example backend/.env  # set APP_ENV=development and a model provider key
docker compose up -d --build          # the full stack; console at http://localhost:5174
scripts/dev.sh                        # or backend and console from source, at http://localhost:5173
scripts/dev-all.sh                    # the same, plus ai-sql-agent from a sibling checkout
```

Configuration, model providers, importing knowledge, local setup without Docker and the API are in [docs/setup.md](docs/setup.md).

## Test

```bash
scripts/ci-local.sh                         # the CI checks; --with-images adds both image builds
cd backend && .venv/bin/python -m pytest    # backend tests; TEST_POSTGRES_DSN adds the pgvector ones
.venv/bin/python scripts/eval_retrieval.py  # retrieval hit@3 and MRR, no model calls
.venv/bin/python scripts/run_eval.py        # end-to-end evaluation; calls the model
cd ../frontend && npm test                  # console tests
```

CI runs Ruff, 953 backend tests against pgvector, 218 console tests, the production build, the A2A contract fixtures and both Docker images. The end-to-end evaluation spends model calls, so it runs only on request.

## Deploy

```bash
docker compose up -d --build                    # 12 services; only Nginx :80 listens beyond localhost
scripts/docker-deploy.sh backup                 # Postgres, Redis, env files and deploy/ config
scripts/docker-deploy.sh restore backups/<dir>  # restore and restart; a drill took 17 s
```

In production mode the service refuses to start without the admin key, the user-signing secret, chat protection and a declared deployment scope. It is single-tenant by design; [deployment boundaries](docs/deployment-boundaries.md) explains what that covers.
