# AI Helpdesk

**English** · [简体中文](README.zh-CN.md)

A multi-agent customer support and ticket orchestration system. It classifies the
incoming question, retrieves supporting knowledge, routes to the right specialist
agent, and hands off to a human whenever the answer is not trustworthy enough.

FastAPI backend, React + TypeScript console. Tickets, knowledge base, monitoring,
tracing and regression evaluation are part of the system, not bolted on afterwards.

## What it is

One support request is split into seven independently governed stages — classify,
enrich with context, retrieve knowledge, collaborate across specialists, enforce
safety, escalate to a human, and regress on quality — each one observable and each
one able to degrade on its own.

- **Fine-grained intent recognition with a zero-latency path.** Requests are split
  into 18 intents, fused from three signals: LLM (70%), embeddings (20%) and rules
  (10%). The recognizer emits an intent, a confidence, per-source scores, an urgency
  level, and structured entities such as order numbers, amounts and part numbers.
  For 9 high-frequency deterministic classes, 53 high-precision anchor phrases
  short-circuit the whole thing: on a hit there is no network call at all, and the
  LLM round trips for one ticket drop from four to one.
- **Hybrid retrieval, gated by intent.** The knowledge base is only queried for
  business intents. Retrieval runs two ways in parallel — pgvector cosine similarity
  for semantic neighbours, and a tsvector keyword index for literal hits (Chinese is
  indexed as character bigrams, so no external tokenizer extension is required) —
  then fuses the two by rank with RRF. The lexical side covers exactly what dense
  vectors are systematically blind to: domain proper nouns and exact strings like
  order numbers. On the built-in corpus, hit@3 goes from 6/8 with vectors alone to
  8/8 (reproducible via `scripts/eval_retrieval.py`). Fusion happens in the storage
  layer and costs no LLM call, so the rule fast path benefits from it too and the
  zero-round-trip promise still holds. On top of that the full path rewrites the
  query into 3 parallel subqueries, deduplicates by content hash, and reranks to the
  top 3 — cross-encoder when configured, LLM otherwise. Without Postgres it falls
  back to SQLite with a pure-Python BM25 sparse side, which keeps local development
  dependency-free. Ingestion deduplicates by content hash, so re-uploading a
  document does not create a duplicate.
- **A retrieval loop with feedback.** Reranked passages are not handed straight to
  the agent. The system first decides whether the evidence actually answers the
  question; if it does not, it runs one more retrieval round aimed at the gap (hard
  cap of one — still insufficient, and the confidence gate escalates to a human).
  The sufficiency verdict comes back in the same call as the LLM rerank, so the
  extra LLM round trips are zero.
- **Multi-agent orchestration with a human backstop.** Three specialists — product
  and installation, quoting and billing, order fulfillment — plus a general showroom
  agent as fallback. Routing picks the primary agent from the intent, 40-odd keyword
  anchors and the extracted entities; cross-domain questions that clear the
  threshold get a supporting agent working in parallel. Unclear intent triggers a
  follow-up question, and any weak confidence signal opens a ticket and escalates.
- **Answer safety as a hard constraint, not a prompt.** Outbound answers pass
  through a zero-LLM rule guard, sentence by sentence. Price and turnaround
  commitments with no basis in the retrieved knowledge or the injected SOP are
  blocked and rewritten into something conservative, with the violation recorded in
  structured form. The streaming SSE path runs the same sentence-level check, so
  streaming and non-streaming answers cannot diverge on what they are allowed to say.
- **Per-user identity and memory (single tenant — see the boundary below).** User
  identity is derived server-side from an HMAC-signed token, so a client cannot forge
  a `user_id` and read someone else's memory. Redis holds per-user rate limits and
  24-hour working memory; pgvector holds episodic memory and user profiles; tickets
  and conversation audit logs are persisted and replayable. This isolates *memory*
  between users. It does **not** isolate *business data* — read
  [Security boundary](#security-boundary-single-tenant-by-design) before pointing this
  at more than one customer's data.
- **A closed engineering loop.** Monitor adjusts the routing penalty every 10
  seconds from agent success rate and latency, shared across instances through
  Redis. LLM-as-Judge replays evaluations on four dimensions — relevance, accuracy,
  completeness, helpfulness — and detects regressions against a baseline that is
  only updated on an explicit promote. 687 backend tests, 48 frontend tests, an
  11-service Docker Compose environment, and metrics, logs and traces via
  Prometheus, Grafana, Tempo, Loki and OpenTelemetry.

## Security boundary: single tenant by design

**This system is a single-tenant deployment. Do not describe it as multi-tenant
isolation.** The boundary is written down here because the two halves of "who is
asking" and "what they are allowed to see" are answered in different places, and
only the first one is enforced.

**This boundary is now enforced at startup, not just documented.**
`AI_HELPDESK_DEPLOYMENT_SCOPE` must be declared explicitly in production
(`internal_single_tenant` | `multi_tenant`) or the process refuses to start —
there is deliberately no default, because "forgot to set it" and "decided on
this value" carry different risk. Declaring `internal_single_tenant` while
`SQL_AGENT_IMPERSONATE=true` is also refused: that combination means somebody
*believes* this deployment isolates business data, and that belief is the risk.
See `backend/app/core/preflight.py::check_deployment_scope`.

**What is isolated.** Conversation memory, episodic memory, user profiles and rate
limits are keyed by a `user_id` that the server derives from an HMAC-signed token
(`AI_HELPDESK_AUTH_SECRET`). A client cannot forge it. The session key sent to the
downstream data service is derived from that same trusted identity, so one customer
cannot address another customer's downstream conversation.

**What is not isolated: business data.** Order, production, stock and payment
answers come from a separate service (`ai-sql-agent`) that this system delegates to.
By default every query is sent under **one shared service identity**
(`SQL_AGENT_USER_ID`), so what comes back is whatever that key's role policy allows —
**decided by the key, not by the customer asking**. Turning on `SQL_AGENT_IMPERSONATE`
forwards the real customer id, but downstream impersonation only swaps the `user_id`;
it keeps the service key's roles and policies and does not re-resolve permissions for
the end user. Verified with two customer identities: same key, same visible data.

**What follows from that.**

- Scope the downstream key's policy to *the least-trusted visitor who can reach this
  helpdesk*. Anything that key can read, any customer can potentially be told.
- Do not deploy one instance in front of several customers' data and call it
  isolation. Real per-customer separation needs the intersection of the service
  permission and the end-user permission, with the tenant/row scope pushed down into
  the database itself. That is not implemented here.
- `POST /auth/token` mints identities and is gated by the shared admin key
  (`AI_HELPDESK_API_KEY`), which proves "the caller is our infrastructure", not "this
  user logged in". Expose it to your gateway only, never to the public internet. The
  long-term fix is for a real login system to issue these tokens.

In production mode (`APP_ENV=production`) the process refuses to start unless
`AI_HELPDESK_API_KEY`, `AI_HELPDESK_AUTH_SECRET` and `AI_HELPDESK_PROTECT_CHAT` are
all set — a missing one used to mean "this endpoint needs no auth".

## How one request flows

![AI Helpdesk request lifecycle](docs/diagrams/request-flow.svg)

## Architecture

![AI Helpdesk technical architecture](docs/diagrams/tech-stack.svg)

## Quick start

### Run the whole stack with Docker

With Docker and Docker Compose installed, from the project root:

```bash
cp .env.example .env
cp backend/.env.example backend/.env
# edit backend/.env and fill in ANTHROPIC_API_KEY
docker compose up -d --build
```

Once it is up:

| URL | What it is |
| --- | --- |
| http://localhost:5174 | Support console |
| http://localhost:8080/docs | API docs |
| http://localhost:3000 | Grafana — metrics, logs and traces |
| http://localhost:9090 | Prometheus metrics |

### Local development

Create `backend/.env` from `backend/.env.example`, fill it in, then:

```bash
scripts/dev.sh
```

That starts the backend and the frontend together. Open http://localhost:5173.

## Where things live

| Task | Entry point |
| --- | --- |
| Talk to support | The console, or `POST /chat` |
| Streaming conversation | `POST /chat/stream` |
| Inspect the knowledge base | The console, or `GET /knowledge/stats` |
| Ingest knowledge | `POST /knowledge/add`, `POST /knowledge/upload` |
| List human tickets | `GET /tickets` |
| Service health | `GET /health`, `GET /monitor` |
| Run an evaluation | `POST /eval/run` |

Full API reference is at http://localhost:8080/docs once the service is running.

## Layout

```text
backend/          FastAPI service; the Python package is backend/app/
backend/skills/   Per-agent SOPs, hot-reloaded at runtime
frontend/         React admin console
deploy/           Nginx / Prometheus / Grafana / Loki / Tempo / OTel config, mounted read-only by docker-compose
scripts/          dev.sh (run both halves locally), docker-deploy.sh (compose lifecycle + backup/restore), ci-local.sh
docs/             Architecture, code guide, integration notes, ops records
```

Redis holds short-lived state — rate limits, recent conversation. PostgreSQL +
pgvector holds the knowledge base, tickets, audit records and long-term memory.
Without Postgres configured the backend falls back to SQLite, which is enough to
try the system locally.

## Tests

```bash
cd backend && pytest && ruff check .
cd frontend && npm test && npm run typecheck
```

Currently **687 passed / 2 skipped / 1 xfailed** on the backend and **48 passed**
on the frontend. The backend figure includes the cross-repo contract cases, which
need a checkout of a2a-contract at the pinned tag; without it they skip and the
count drops to 612. CI runs 4 jobs; the backend one brings up a pgvector service
container so the Postgres path is exercised for real rather than falling back to
SQLite, and a separate job replays the contract fixtures.

## Performance

`backend/scripts/load_test.py` writes a JSON report with p50 / p95 / RPS.

```bash
cd backend && ./.venv/bin/python scripts/load_test.py --unique-messages --requests 100 --concurrency 10
```

**`--unique-messages` is not optional.** The message pool only holds 10 entries;
without the flag the script cycles through them, and from request 11 onward the
in-process intent cache in `app/core/intent_recognizer.py` hits every time. Those
requests skip the intent LLM call entirely and p95 comes out roughly 11% below a
genuine first-time ticket. The script warns when the flag is missing and records a
`mode` field in the report.

These are single-instance cold-path numbers. No capacity planning has been done and
nothing here has been validated against real production traffic — this is
engineering built to production standards, not a production-proven system.

## Stack

FastAPI · React · TypeScript · PostgreSQL + pgvector · Redis · Anthropic-compatible
API · OpenTelemetry · Prometheus · Grafana · Docker Compose
