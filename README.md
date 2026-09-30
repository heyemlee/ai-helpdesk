<div align="center">

# AI Helpdesk

**Ground support answers in business knowledge. Keep routing and handoff decisions visible.**

A multi-agent support service with hybrid retrieval, specialist collaboration, conversational memory, and guarded streaming answers. Includes a React console for chat, knowledge, monitoring, and evaluation.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](backend/requirements.txt)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/app/api/main.py)
[![React](https://img.shields.io/badge/React-TypeScript-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

**English** · [简体中文](README.zh-CN.md)

[Quickstart](#quickstart) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Design decisions](#design-decisions) · [Evaluation](#evaluation-and-current-limits)

</div>

## Preview

<!-- Add docs/images/main-interface.png, then replace the src below in both READMEs. -->
<p align="center">
  <img src="docs/images/screenshot-placeholder.svg" alt="Reserved for a screenshot of the support console and routing inspector" width="960" />
</p>

> **“My cabinet door won't close properly. What should I do?”** → identify an installation issue → retrieve troubleshooting knowledge and the specialist's SOP → check the answer → return guidance or create a human ticket.

- **Context-aware support** — use recent conversation, historical summaries, and user profiles to handle follow-up questions; clarify unclear requests before proceeding.
- **Specialist collaboration** — route to product/installation, pricing/billing, or order fulfillment; combine specialists for cross-domain questions, with a generalist fallback.
- **Controlled answers** — check unsupported price and turnaround commitments before releasing each sentence; apply separate confidence gates for intent, routing, and agent output.
- **Visible decisions** — inspect routing reasons, retrieved knowledge usage, policy violations, and handoff state in the console; connect requests to audit records, tickets, and traces.

The bundled domain is cabinet customization and fulfillment. Replace the knowledge and SOPs to adapt business content; a different intent taxonomy or specialist lineup also requires changes to recognition and routing.

## Architecture

![AI Helpdesk architecture](docs/diagrams/architecture.en.svg)

Created with [Archify](https://github.com/tt-a1i/archify) · [Interactive HTML](docs/diagrams/architecture.en.html) (download and open in a browser) · [Editable JSON](docs/diagrams/architecture.en.archify.json)

The diagram shows logical modules within one FastAPI service. PostgreSQL + pgvector persists knowledge, long-term memory, profiles, tickets, and audit; Redis holds working memory and shared operational state. SQLite can replace PostgreSQL for local persistence, while Redis is still required.

A request follows **identity → memory → intent → retrieval / optional business query → specialist execution → answer policy → confidence gate → reply or handoff**. Rule anchors skip the intent model call and the query-rewrite/rerank stages. Complex requests use the full retrieval path; live order, stock, and payment data can come from a separate [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent) service.

## Tech stack

| Layer | Technology | Role |
| --- | --- | --- |
| Runtime & API | Python 3.12 · FastAPI · Uvicorn · Pydantic 2 | Typed requests/responses, async endpoints, startup configuration checks |
| Web console | React 18 · TypeScript 5 · Vite 6 · Tailwind CSS 4 · TanStack Query 5 | Chat, routing inspection, knowledge, monitoring, and evaluation; Markdown answers |
| Model integration | Anthropic SDK · OpenAI SDK · HTTPX | Intent, query rewriting, reranking, generation, and judging through configurable providers |
| Knowledge retrieval | pgvector · tsvector · Python BM25 · RRF | Semantic/lexical recall and rank fusion; optional cross-encoder reranking, LLM fallback |
| Memory & persistence | Redis 7 · PostgreSQL 16 · asyncpg · SQLite | Short-lived state, long-term memory, profiles, tickets, and audit; local persistence fallback |
| Orchestration & integration | Custom asyncio orchestration · HTTP tools · HMAC tokens · SSE | Specialist collaboration, downstream timeouts/circuit breakers, identity, and streamed progress/answers |
| Observability | OpenTelemetry · Prometheus · Grafana · Tempo · Loki | Correlated metrics, logs, and traces; Compose currently collects logs with Promtail |
| Engineering & delivery | pytest · Ruff · Vitest · Testing Library · GitHub Actions · Docker Compose · Nginx | Backend/frontend/contract/image checks and an 11-service deployment stack |

Pinned dependencies live in [requirements.txt](backend/requirements.txt), [package-lock.json](frontend/package-lock.json), and [Compose](docker-compose.yml). Live chat requires a model provider. Embedding and reranking are configured independently; without a remote embedding endpoint, retrieval uses local character n-gram vectors.

## Design decisions

| Engineering problem | Implementation | Source |
| --- | --- | --- |
| Routine questions should not pay for the full reasoning path | Classify 19 intents with LLM / embedding / rule signals (70% / 20% / 10%); deterministic anchors bypass intent inference and query rewrite/rerank | [intent](backend/app/core/intent_recognizer.py) · [request preparation](backend/app/api/main.py) |
| Semantic search misses exact business terms | Recall dense and lexical candidates in parallel; fuse **ranks**, not incompatible similarity/BM25 scores, using RRF; index Chinese character bigrams and deduplicate chunks by content hash | [knowledge](backend/app/tools/knowledge_base.py) · [lexical](backend/app/core/lexical.py) |
| Retrieved passages still do not answer the question | Rewrite into three subqueries, merge and rerank; reuse the LLM rerank's sufficiency verdict or a score threshold, then allow **one** gap-directed retrieval round | [retrieval loop](backend/app/tools/tool_manager.py) · [reranker](backend/app/core/reranker.py) |
| A question spans domains, or a specialist becomes unreliable | Score intent, entities, and domain terms; run qualifying specialists in parallel, fall back on execution failure, and use monitor penalties to steer traffic between instances | [orchestrator](backend/app/agents/agent_orchestrator.py) · [monitor](backend/app/monitor/performance_monitor.py) |
| Prompt instructions alone cannot constrain commitments | Inject scoped, reloadable SOPs; check generated price/time statements against evidence, rewrite unsupported promises, and record violations. SSE buffers complete sentences before applying the same guard | [answer policy](backend/app/core/answer_policy.py) · [SOP loader](backend/app/core/skill_loader.py) |
| Human escalation or webhook delivery fails silently | Persist a handoff ticket; record notification work in an outbox before delivery, check HTTP status, and track retries and terminal failures | [tickets](backend/app/tickets/ticket_store.py) · [notifications](backend/app/tickets/notifier.py) |
| Quality changes are hard to diagnose | Return structured routing and policy metadata, correlate audit/telemetry by request, and evaluate grounded answers against an explicitly promoted baseline | [API](backend/app/api/main.py) · [evaluator](backend/app/evaluation/evaluator.py) |

Current handoff thresholds are **intent < 0.45, routing < 0.35, or agent confidence < 0.70**. These are heuristic signals, not calibrated probabilities of correctness. The answer guard targets price/time commitments; it does not verify every factual claim.

## Quickstart

Requires **Docker, Docker Compose, and model credentials**. Start in a fresh directory:

```bash
git clone https://github.com/heyemlee/ai-helpdesk.git
cd ai-helpdesk
cp .env.example .env
cp backend/.env.example backend/.env
```

Edit `backend/.env` for a local demo; edit existing environment files directly rather than replacing them:

```dotenv
APP_ENV=development
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-api-key
# ANTHROPIC_BASE_URL=https://your-compatible-endpoint
# ANTHROPIC_MODEL=your-model-name
```

The example file defaults to production mode, which requires the security settings described below. After configuring the local demo:

```bash
docker compose up -d --build
```

Open the console at [http://localhost:5174](http://localhost:5174), or call the API:

```bash
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","message":"My cabinet door will not close properly. What should I do?"}'
```

The response includes `response`, `intent`, `primary_agent`, `supporting_agents`, `routing_reason`, `answer_confidence`, `policy_violations`, `handoff`, and `ticket_id`. Reuse the returned `conv_id` for follow-up questions. This unauthenticated example applies to local demo mode.

API docs: [localhost:8080/docs](http://localhost:8080/docs) · Grafana: [localhost:3000](http://localhost:3000) · Prometheus: [localhost:9090](http://localhost:9090). Grafana and database credentials are set in the root `.env`.

### Choose a model provider

For OpenAI-compatible endpoints, set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and, if needed, `OPENAI_BASE_URL`. `EMBEDDING_*` and `RERANK_*` configure separate retrieval services. Match the embedding dimension and rebuild stored vectors when changing embedding models. See [backend/.env.example](backend/.env.example); restart after configuration changes.

### Add your business knowledge

Upload `.txt`, `.md`, or JSON document lists in the knowledge panel, or use the API:

```bash
curl -X POST http://localhost:8080/knowledge/upload \
  -F 'file=@backend/data/demo_docs/troubleshooting.md'
```

Maintain specialist procedures in `backend/skills/*/SKILL.md`, then call `POST /skills/reload`. Knowledge supplies reference material; SOPs define handling steps and escalation rules. For live business data, configure `SQL_AGENT_URL` and its credentials to connect ai-sql-agent. See [SOP authoring](backend/skills/README.md) and the [identity integration requirements](docs/integrations/sql-agent-identity-requirements.md). Protected deployments require authentication headers on these endpoints.

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

To run together with [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent) using the local setup from [a2a-contract](https://github.com/heyemlee/a2a-contract#2-enable-a2a-collaboration), check out ai-sql-agent as a sibling directory (or set `SQL_AGENT_DIR`) and run:

```bash
scripts/dev-all.sh
```

The script starts ai-sql-agent on `127.0.0.1:8000` (its own `AI_SQL_PORT`), waits for `/health` and the Agent Card, then runs `scripts/dev.sh` with `SQL_AGENT_URL=http://127.0.0.1:8000`. Put `API_PORT` on another port such as `8080`; the script refuses to start when both use the same port. Ctrl-C stops all three processes, and running it again first stops the previous copies.

</details>

## Evaluation and current limits

**Retrieval smoke measurement, 2026-09-22:** eight bundled Chinese queries, six knowledge chunks, SQLite, deterministic 256-dimensional character n-gram vectors, no remote embedding or generative model calls.

| Retrieval strategy | MRR | hit@3 |
| --- | --- | --- |
| Dense only | 0.583 | 6/8 |
| Lexical only | **0.844** | 7/8 |
| Hybrid, RRF | 0.812 | **8/8** |

Hybrid retrieval places the expected document in the top three for every case here; lexical retrieval has the higher MRR. This small check measures retrieval ranks, not answer accuracy, remote embedding quality, or production performance. Conditions, per-query ranks, and the reproduction command are in the [measurement record](docs/evaluation/retrieval-smoke.md).

```bash
# From the repository root, after installing dependencies; Ruff is separate
backend/.venv/bin/pip install ruff
(cd backend && .venv/bin/python -m pytest && .venv/bin/ruff check .)
(cd frontend && npm test && npm run typecheck && npm run build)
```

[CI](.github/workflows/ci.yml) covers backend tests with a real pgvector service, frontend tests/builds, pinned cross-repository A2A fixtures, and Docker images. Some local integration tests need services or the contract checkout. LLM-as-Judge evaluates relevance, accuracy, completeness, and helpfulness through `POST /eval/run`; update its baseline explicitly with `POST /eval/promote`.

Load tests use `backend/scripts/load_test.py --unique-messages --requests 100 --concurrency 10`. Keep `--unique-messages` to avoid intent-cache hits from repeated questions. No production capacity or SLA is established by these checks.

**Deployment scope:** this is a single-tenant system. Signed identities isolate user memory; a shared downstream service key still governs business-data access. Per-customer row-level authorization is not implemented, and enabling impersonation or declaring `multi_tenant` does not add it. Production startup requires the admin key, user-signing secret, protected chat, token-issuer source allowlist, and explicit deployment scope. See [deployment boundaries](docs/deployment-boundaries.md).

<details>
<summary><strong>API reference</strong></summary>

| Endpoint | Purpose |
| --- | --- |
| `POST /chat` · `POST /chat/stream` | Replies, routing metadata, policy findings, and handoff state; SSE includes preparation progress |
| `GET /tickets` · `POST /tickets/{ticket_id}/status` | Inspect and advance human tickets |
| `POST /knowledge/add` · `POST /knowledge/upload` | Ingest knowledge; identical chunks are deduplicated |
| `GET /skills` · `POST /skills/reload` | Inspect and reload business SOPs |
| `POST /eval/run` · `POST /eval/promote` | Run evaluations and explicitly promote the baseline |
| `GET /health` · `GET /monitor` · `GET /metrics` | Health, agent/tool monitoring, and Prometheus metrics |
| `POST /auth/token` | Issue a signed user identity through a trusted integration |

In protected deployments, management endpoints use `X-API-Key`; user identity is carried by `X-User-Token`. `/auth/token` is a trusted infrastructure endpoint, not an end-user login system. See [login integration](docs/auth-token-integration.md).

</details>

<details>
<summary><strong>Repository map</strong></summary>

```text
backend/app/
  api/ · core/             HTTP/SSE, intent, identity, policies, model adapters
  agents/ · tools/         Specialist routing, RAG, business queries, tool governance
  memory/ · storage/       Working/long-term memory, persistence, conversation audit
  tickets/                 Human tickets and notification outbox
  monitor/ · evaluation/   Operational feedback and quality regression
backend/skills/            Scoped, reloadable business SOPs
backend/tests/ · scripts/  Test suite, retrieval eval, load testing
frontend/src/             React console and routing inspector
deploy/                   Nginx, metrics, logs, and tracing configuration
scripts/                  Local development, deployment, backup/restore, local CI
docs/                     Architecture, integration, measurements, and ops records
```

</details>

## Further reading

[Architecture](docs/architecture.md) · [Code guide](docs/code-guide.md) · [Backend](backend/README.md) · [SOP authoring](backend/skills/README.md) · [Login integration](docs/auth-token-integration.md) · [Deployment boundaries](docs/deployment-boundaries.md) · [Diagram maintenance](docs/diagrams/README.md)
