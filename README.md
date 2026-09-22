<div align="center">

# AI Helpdesk

**Multi-agent customer support & ticket orchestration**

Understand intent · Retrieve evidence · Coordinate specialists · Hand off to humans

**English** · [简体中文](README.zh-CN.md)

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?style=flat-square&logo=typescript&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)

[Capabilities](#capabilities) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Quick start](#quick-start) · [Validation](#validation)

</div>

AI Helpdesk turns a support question into a traceable workflow: classify intent, load memory, retrieve evidence, route to specialists, check the answer, and create a human ticket when confidence is too low. A React console brings together streaming chat, routing inspection, knowledge management, monitoring and evaluation.

The bundled knowledge and SOPs cover cabinet customization, installation, pricing and order fulfillment. Adapt the domain through the knowledge base and `backend/skills/`.

## Preview

<!-- Add your screenshot as docs/images/main-interface.png, then replace the src below. Both READMEs share this image. -->
<p align="center">
  <img src="docs/images/screenshot-placeholder.svg" alt="Placeholder for the main application screenshot" width="100%" />
</p>
<p align="center"><sub>Main interface screenshot · Show the conversation and routing inspector</sub></p>

## Capabilities

| Capability | Implementation & purpose |
| --- | --- |
| **Explainable intent recognition** | 19 intents; LLM, embedding and rule signals weighted 70% / 20% / 10%. Returns confidence, entities and per-source scores. Deterministic anchors skip the intent model call for common questions. |
| **Hybrid retrieval with feedback** | pgvector semantic search + tsvector lexical search, fused with RRF. The full path adds query rewriting, parallel retrieval, deduplication and reranking; insufficient evidence can trigger one additional retrieval round. |
| **Specialist collaboration** | Three specialists for product/installation, pricing/billing and order fulfillment, plus a generalist. Intent, entities and domain scores drive routing; cross-domain requests can run in parallel, with fallback and confidence-based human handoff. |
| **Guarded streaming answers** | A rule guard rewrites unsupported price and turnaround commitments against retrieved knowledge and SOPs. SSE releases complete checked sentences, using the same policy as ordinary replies. |
| **Memory & ticket continuity** | Redis stores working memory; PostgreSQL stores long-term memory, profiles, tickets and audit records. HMAC tokens establish user identity; an outbox tracks notification delivery and retries. |
| **Operational & quality feedback** | The monitor adjusts instance routing penalties from success rate and latency. A four-dimension LLM judge detects regressions against a baseline that changes only on explicit promotion. |

### Engineering details

- **Two execution paths.** The rule fast path skips intent LLM calls, query rewriting and reranking. The full path adds reasoning where needed. Final answers can still require a model call. [Intent recognizer](backend/app/core/intent_recognizer.py)
- **Separate recall from ranking.** Chinese lexical search uses character bigrams; RRF fusion requires no LLM. Reranking uses a configured cross-encoder or falls back to an LLM, with evidence sufficiency included in the LLM result. SQLite mode supplies a Python BM25 sparse index. [Retrieval](backend/app/tools/knowledge_base.py) · [Reranker](backend/app/core/reranker.py)
- **Maintainable business policies.** Each agent receives scoped, hot-reloadable `SKILL.md` SOPs. Deterministic guards check generated answers, while intent, routing and agent confidence have separate handoff thresholds. [Orchestrator](backend/app/agents/agent_orchestrator.py) · [Answer policy](backend/app/core/answer_policy.py) · [SOPs](backend/skills/README.md)
- **Bounded external dependencies.** Live order, stock and payment questions can be delegated to a separate `ai-sql-agent`. The tool layer manages timeouts and circuit breakers; business results join retrieved knowledge in the answer context. [Business queries](backend/app/tools/sql_agent_client.py) · [Tool manager](backend/app/tools/tool_manager.py)

## Architecture

![AI Helpdesk architecture, generated with Archify](docs/diagrams/architecture.en.svg)

[Interactive HTML](docs/diagrams/architecture.en.html) · [Editable JSON](docs/diagrams/architecture.en.archify.json) · [Architecture guide](docs/architecture.md)

Generated with [Archify](https://github.com/tt-a1i/archify). GitHub displays the static SVG; download and open the HTML in a browser for zoom, node inspection, themes and export. Gateway, retrieval, orchestration and guards are logical modules within one FastAPI service.

The primary request path:

```text
Identity → Memory → Intent → Optional retrieval / business query
         → Specialist routing & execution → Answer policy → Confidence gate
         → Reply or human handoff → Memory & audit persistence
```

## Tech stack

| Layer | Technology | Role |
| --- | --- | --- |
| Frontend | React 18 · TypeScript 5 · Vite 6 | Console, typed components, development proxy and builds |
| UI & data | Tailwind CSS 4 · TanStack Query 5 · react-markdown | Styling, server-state caching, Markdown replies; `fetch + ReadableStream` for POST SSE |
| API & orchestration | Python 3.12 · FastAPI · Pydantic 2 · asyncio | Request validation, async I/O, custom agent routing and parallel execution |
| Model integration | Anthropic SDK · OpenAI SDK · HTTPX | Two model protocols selected by `LLM_PROVIDER`; separately configurable embedding and rerank endpoints |
| Persistence & retrieval | PostgreSQL 16 · pgvector · tsvector · asyncpg | Durable state, vector and lexical search; SQLite persistence fallback for local development |
| Short-lived state | Redis 7 | Working memory, per-user rate limits, shared circuit-breaker state and routing penalties |
| Observability | OpenTelemetry · Prometheus · Grafana · Tempo · Loki | Metrics, logs and traces; the current log collector is Promtail |
| Delivery & testing | Docker Compose · Nginx · GitHub Actions · pytest · Ruff · Vitest · Testing Library | 11-service stack, reverse proxy, backend/frontend/contract/image CI checks |

Exact dependencies live in [requirements.txt](backend/requirements.txt), [package.json](frontend/package.json) and [Compose](docker-compose.yml).

## Quick start

### Local Docker demo

Requires Docker and Docker Compose. From your shell:

```bash
git clone https://github.com/heyemlee/ai-helpdesk.git
cd ai-helpdesk
cp .env.example .env
cp backend/.env.example backend/.env
```

Edit `backend/.env`: fill in `ANTHROPIC_API_KEY` and set **`APP_ENV=development`** for the local demo. Alternatively, use `LLM_PROVIDER=openai` with `OPENAI_API_KEY`, `OPENAI_MODEL` and, if needed, `OPENAI_BASE_URL`. The example defaults to production mode; a model key alone will not pass startup validation.

```bash
docker compose up -d --build
```

| Address | Service |
| --- | --- |
| [localhost:5174](http://localhost:5174) | Support console |
| [localhost:8080/docs](http://localhost:8080/docs) | OpenAPI / Swagger |
| [localhost:3000](http://localhost:3000) | Grafana; credentials are in the root `.env` |
| [localhost:9090](http://localhost:9090) | Prometheus |

<details>
<summary>Local development: Python + Node.js</summary>

Use Python 3.12, Node.js 24 and Redis. After preparing the environment files above, run from the repository root:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm ci)
docker compose up -d redis
```

Set `REDIS_URL` in `backend/.env` to `redis://:<root REDIS_PASSWORD>@127.0.0.1:6379/0`, URL-encoding special characters in the password. With `POSTGRES_DSN` unset, persistence falls back to SQLite; **Redis is still required**. For local evaluations, set `EVAL_BASELINE_PATH=./data/eval/baseline.json`.

```bash
scripts/dev.sh
```

Open [localhost:5173](http://localhost:5173). The backend defaults to port `8000`; `API_PORT` changes it and the frontend proxy follows.

</details>

## Validation

From the repository root, with backend dependencies installed in the virtual environment and `ruff` installed separately:

```bash
(cd backend && .venv/bin/python -m pytest && .venv/bin/ruff check .)
(cd frontend && npm test && npm run typecheck && npm run build)
```

[CI](.github/workflows/ci.yml) checks the backend, frontend, cross-repository A2A contract and Docker images independently. Backend CI exercises a real pgvector service; the contract job fetches version-pinned `a2a-contract` fixtures. Use actual test results for current counts.

<details>
<summary>Retrieval evaluation & load testing</summary>

```bash
(cd backend && .venv/bin/python scripts/eval_retrieval.py)
(cd backend && .venv/bin/python scripts/load_test.py --unique-messages --requests 100 --concurrency 10)
```

The retrieval script compares dense / lexical / hybrid MRR and hit@3 on eight bundled cases, a small regression set. Keep `--unique-messages` during load testing to avoid lower latency from intent-cache hits on repeated questions. Run LLM-as-Judge through the console or `POST /eval/run`; explicitly promote the baseline through `POST /eval/promote`.

</details>

## Deployment boundary

This is a **single-tenant system**. User identities and memory can be isolated; downstream business-data access is still governed by a shared service key. Per-customer row-level authorization is not implemented. Neither `SQL_AGENT_IMPERSONATE` nor declaring `multi_tenant` creates that authorization boundary.

Production requires an admin key, a user-signing secret, protected chat, a token-issuer source allowlist and an explicit deployment scope. Missing settings fail startup. Local demo settings are not suitable for public deployment. See [deployment boundaries](docs/deployment-boundaries.md) and [login integration](docs/auth-token-integration.md).

Evaluation, observability and deployment tooling are included; capacity and SLAs have not been established against real production traffic.

## Code & documentation

```text
backend/app/      API, intent, agents, retrieval, memory, tickets and evaluation
backend/skills/   Scoped, hot-reloadable business SOPs
frontend/src/    Chat, knowledge, monitoring, evaluation and routing console
deploy/          Nginx, metrics, logging and tracing configuration
scripts/         Development, deployment, backup/restore and local CI
docs/            Architecture, code guide, identity integration and ops records
```

[Architecture](docs/architecture.md) · [Code guide](docs/code-guide.md) · [Backend](backend/README.md) · [Frontend](frontend/README.md) · [Diagram maintenance](docs/diagrams/README.md)
