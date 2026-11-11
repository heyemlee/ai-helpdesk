<div align="center">

# AI Helpdesk

**Two cooperating agent services and the contract between them: a multi-agent support service, a Text2SQL agent it delegates to over A2A, and a versioned, machine-checked A2A contract, in one repository.**

[![Python](https://img.shields.io/badge/Python-3.12_%C2%B7_3.11-3776AB?style=flat-square&logo=python&logoColor=white)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119_%C2%B7_0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/requirements.txt)
[![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](docker-compose.yml)
[![A2A](https://img.shields.io/badge/A2A_0.2-contract_v1.15-6E56CF?style=flat-square)](a2a-contract/README.md)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

**English** · [简体中文](README.zh-CN.md)

[Highlights](#highlights) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Run it](#run-it) · [Docs](#docs)

</div>

<p align="center">
  <img src="docs/screenshots/chat.webp" width="960" alt="Two questions in the AI Helpdesk console. An order-status question is delegated to the SQL agent over A2A and its stages stream into the console: working out the kind of question, finding the tables, planning, writing and running the queries on two databases, and repairing one that failed. The reply is rendered from the verified rows: the order's status and ship date, the time of the data, a note that an employee must review it, and the evidence to review. Learning mode then explains the turn: the weighted intent vote, the specialist that was chosen, the delegation and its verification, the answer check and the handoff decision. A warranty question is then answered from the knowledge base with the published policy excerpt, its version, scope and effective date" />
</p>

<p align="center"><sub>A made-up company with made-up orders and documents, served by a mocked API and sped up.</sub></p>

## Highlights

- **Two agents, one contract.** The support agent delegates data questions to a separate Text2SQL agent over A2A (JSON-RPC 2.0, with each stage streamed back over SSE). The contract between them, JSON Schemas and golden fixtures, sits in the same repository, and both agents' tests read it from the same commit.
- **A pipeline with a bounded loop inside it.** An LLM, embeddings and rules vote on the intent, rules pick the tools and `asyncio.gather` runs them together, and domain scores route the turn to specialist agents. A specialist that still lacks something calls the same tools itself through function calling, within a fixed budget.
- **Replies rendered from verified evidence.** The model decides what to look up and whether a person is needed; the text a user reads is rendered by the program from row-bound SQL evidence and published policy excerpts. Without evidence there is no business claim, and a missing or malformed field counts against a result.
- **Text2SQL behind a permission gate.** The SQL agent plans a step per data source, has a model write the SQL, checks every table and column at an SQLGlot AST gate, runs it read-only, repairs a failure at most twice and checks its own result. Its release gate measures 94.7% cross-database accuracy, with 0.9% of answers wrong without a warning.
- **Hybrid retrieval with a corrective round.** pgvector and a `tsvector` keyword index fused by rank, a cross-encoder or LLM rerank that also judges whether the evidence is enough, and one more round aimed at what is missing.
- **A person in the loop.** Low confidence, or a specialist's own request, hands the turn to staff with a ticket. A Case carries one problem across messages, and an employee approves each draft after its evidence is checked again. Staff sign in with local accounts and roles.
- **Turns that outlive their worker.** In queued mode a turn is a run in Postgres, dispatched on Redis Streams to workers that hold a fenced lease, and the console resumes the stream with `Last-Event-ID`. In chaos drills that killed workers and restarted Redis, all 340 runs finished with no second ticket or answer.
- **Observable and gated.** Every step of a turn is an ordered event in Postgres, optionally through Kafka, next to OpenTelemetry traces and Grafana dashboards. Five required CI checks run more than 3,000 tests on every pull request.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" width="844" alt="System architecture: one repository holds two agent services and the contract between them. The staff console (React 18, Vite) posts a question to the support agent in backend/, a FastAPI service on Python 3.12 with asyncio, and customers reach it through Nginx; the answer streams back over SSE. The support agent runs five stages: 1 Understand, an intent vote with working memory; 2 Gather, a hybrid search and a delegation over A2A; 3 Route, to specialist agents; 4 Answer, in a bounded tool loop; 5 Verify, which renders the reply from verified evidence, or hands off to staff review as a Case or ticket. Gather delegates data questions over A2A, JSON-RPC, to the SQL agent in ai-sql-agent/, a FastAPI service on Python 3.11 that retrieves tables with BM25 and vectors, plans a step per source, has a model write SQL, checks it at an SQLGlot AST gate and runs it read-only with repair; evidence returns over SSE. Contract v1.15, JSON Schemas and golden fixtures, is tested on both sides. Below them: PostgreSQL 16 with pgvector and tsvector, Redis 7 for memory and the run queue, Kafka as an optional turn event log, the Anthropic and OpenAI model APIs, and the business data the SQL agent reads. Platform: Docker Compose, Kubernetes, KEDA, Nginx, OpenTelemetry, Grafana and GitHub Actions. A pulse follows one data question through all of it in that order" />
</p>

A question from the staff console, or from a customer through Nginx, runs through five stages in the support agent. **Understand** loads memory and votes on the intent. **Gather** runs a hybrid search in PostgreSQL and, for data questions, a delegation to the SQL agent over A2A, at the same time. **Route** picks the specialist agents, **Answer** runs them with a bounded tool loop, and **Verify** renders the reply from the evidence that passed its checks, or hands the turn to a person. The SQL agent runs its own loop (retrieve, plan, write SQL, gate, run, repair) and streams each stage back. Blue marks requests and data, teal the A2A exchange, orange model calls and violet the contract.

The two agents stay separate services: only the SQL agent can reach business data and run model-written SQL, each pins its own Python and framework versions, and a heavy query can't take the chat down with it.

| Directory | What it is | Docs |
| --- | --- | --- |
| `backend/` · `frontend/` | The support agent and its staff console | [setup](docs/setup.md) |
| `ai-sql-agent/` | The Text2SQL agent, with A2A, MCP and HTTP interfaces | [README](ai-sql-agent/README.md) |
| `a2a-contract/` | The contract: protocol document, JSON Schemas, golden fixtures, self-check | [README](a2a-contract/README.md) |
| `deploy/` · `scripts/` | Compose, Kubernetes and monitoring config; CI, backup and release scripts | [setup](docs/setup.md) |

## Tech stack

| Layer | Technology | Role |
| --- | --- | --- |
| Agent runtime | Python 3.12 · FastAPI · asyncio · Pydantic 2 | Chat over SSE, concurrent tools and specialists, every model decision a validated schema |
| Model calls | Anthropic SDK · OpenAI SDK (Chat Completions, Responses) | One interface over both providers, switchable at runtime, with function calling |
| Agent protocol | A2A 0.2 · JSON-RPC 2.0 · SSE · Agent Card · JSON Schema 2020-12 | Delegation, discovery, and a contract both agents test |
| Text2SQL agent | Python 3.11 · FastAPI · SQLGlot · psycopg 3 · MCP | An AST permission gate in front of read-only SQL on PostgreSQL or SQLite |
| Retrieval | PostgreSQL 16 · pgvector · `tsvector` · cross-encoder rerank | Dense and keyword search fused by rank |
| State and events | Redis 7 (Streams, Lua) · Apache Kafka 4.3 (optional) | Working memory, the run queue and shared rate limits; the turn event log |
| Console | React 18 · TypeScript · Vite 6 · TanStack Query 5 · Tailwind CSS 4 | Streaming chat, Cases, tickets, knowledge, data and admin pages |
| Observability | OpenTelemetry · Prometheus · Grafana · Tempo · Loki | Traces, metrics, logs and alert rules |
| Delivery | Docker Compose · Nginx · Kubernetes · KEDA · GitHub Actions | 13 services in one Compose deploy, or manifests with KEDA-scaled workers |
| Testing | pytest · Ruff · Vitest · k6 | Unit and contract tests, load tests and chaos drills |

## Run it

```bash
cp .env.example .env                  # Compose variables; fill in the demo admin to sign in to the console
cp backend/.env.example backend/.env  # set APP_ENV=development and a model key
docker compose up -d --build          # the whole stack; console at http://localhost:5174
scripts/dev.sh                        # or the backend and console from source, at http://localhost:5173
scripts/ci-local.sh                   # every CI job, in throwaway copies
```

Model providers, queued mode, the event log and running from source are in [docs/setup.md](docs/setup.md). The SQL agent keeps its own Python 3.11 environment ([develop](ai-sql-agent/README.md#develop)).

## Docs

- **Components:** [SQL agent](ai-sql-agent/README.md) · [A2A contract](a2a-contract/README.md) · [setup and API](docs/setup.md)
- **Design:** [durable runs](docs/design/durable-agent-runs.md) · [deployment boundaries](docs/deployment-boundaries.md)
- **Measurements:** [retrieval](docs/evaluation/retrieval-remote-embedding.md) · [tool-loop A/B](docs/evaluation/tool-loop-ab.md) · [chaos drill](docs/evaluation/chaos-drill.md) · [load test](docs/evaluation/load-test.md) · [SQL agent release gate](ai-sql-agent/README.md)
- **Acceptance:** [the harness](acceptance/README.md) that gates a release on a frozen question set

## AI-assisted development

This repository is developed with Claude Code, inside these controls:

- **Branch protection on `main`.** Changes arrive only through pull requests, five CI checks must pass, and admins can't bypass them.
- **Project skills.** [`test-backend`](.claude/skills/test-backend/SKILL.md) runs the tests in throwaway copies, so the real `.env` files and databases can't reach them.
- **A read-only MCP server.** [`helpdesk-dev`](backend/scripts/dev_mcp_server.py) lets an agent read recorded turns from a development database, and refuses the production one.
- **An advisory Claude review.** [claude-review.yml](.github/workflows/claude-review.yml) comments on pull requests but can't approve or block them.
