# Setup and configuration

[← README](../README.md) · [中文完整指南](../backend/README.md)

How to run AI Helpdesk, connect a model, load your own knowledge and call the API. The README has the short version.

## Run the full stack with Docker

Requires Docker with Compose and a model provider key. From the repository root:

```bash
cp .env.example .env
cp backend/.env.example backend/.env
```

The root `.env` holds the Compose variables (database, Redis and Grafana passwords). `backend/.env` configures the service. It defaults to production mode, which needs the security settings below; for a local demo, edit it to:

```dotenv
APP_ENV=development
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-api-key
# ANTHROPIC_BASE_URL=https://your-compatible-endpoint
# ANTHROPIC_MODEL=your-model-name
```

Then start it:

```bash
docker compose up -d --build
```

| Address                                           | What                                     |
| ------------------------------------------------- | ---------------------------------------- |
| [localhost:5174](http://localhost:5174)           | Staff console                            |
| [localhost:8080/docs](http://localhost:8080/docs) | Staff instance API docs                  |
| [localhost](http://localhost)                     | Customer instance, through Nginx         |
| [localhost:3000](http://localhost:3000)           | Grafana (credentials in the root `.env`) |
| [localhost:9090](http://localhost:9090)           | Prometheus                               |

Every port except Nginx's 80 binds to `127.0.0.1`. The staff instance has no API key of its own, so put a login in front of it before you open it to other machines.

Try the API:

```bash
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","message":"My cabinet door will not close properly. What should I do?"}'
```

The response includes `response`, `intent`, `primary_agent`, `supporting_agents`, `routing_reason`, `answer_confidence`, `policy_violations`, `handoff` and `ticket_id`. Send the returned `conv_id` with follow-up questions. `POST /chat/stream` returns the same turn as server-sent events: `progress`, `meta`, `routing`, one `delta` per sentence, then `result`.

## Choose a model provider

For an OpenAI-compatible endpoint, set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL` and, if needed, `OPENAI_BASE_URL`. Endpoints that ignore JSON Schema `response_format` still work: the schema is moved into the prompt and the reply is validated strictly.

`EMBEDDING_*` and `RERANK_*` configure the retrieval models separately. Without a remote embedding endpoint, retrieval ranks keyword matches first and uses local character n-gram vectors only to fill the remaining places. Vectors from different embedding models cannot be mixed: after changing the model in `backend/.env`, set `EMBEDDING_DIM` to its output size, stop the service and rebuild the stored vectors with [reembed.py](../backend/scripts/reembed.py) (it prints a plan first; `--apply` runs it). The service refuses to start on vectors another model produced. Restart after any change to `backend/.env`.

Both connections can also be changed from the console without a restart, under **Admin & diagnostics → Model connections**:

- A connection is tested before it is saved: a plain reply and a structured-output call for the chat model, one embedding request for the embedding model.
- Saving an embedding model re-embeds every stored vector while the service keeps answering, then switches over.
- Saved values override `backend/.env` until you press **Restore server configuration**.
- Keys are write-only. The page shows whether one is set and its last four characters, and a key is only sent to the host it was entered for.
- Editing works only from a console opened on the same machine, and needs `AI_HELPDESK_MODEL_SETTINGS_PATH`. Compose sets it, and the customer instance follows what the staff console saves.

## Let specialists call tools

By default the rules decide what to search and whether to delegate, before any specialist runs. `AI_HELPDESK_TOOL_LOOP=on` in `backend/.env` adds a bounded tool loop to the Answer stage: a specialist that finds the prefetched material lacking can call `knowledge_search` itself, through the chat model's function calling, and on an instance with business data every specialist but the generalist can also call `order_data_query`. The model fills in only the query; identity comes from the server.

| Variable                                | Default  | Meaning                                                                                                                                       |
| --------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `AI_HELPDESK_TOOL_LOOP_MAX_CALLS`       | 3        | Model calls per specialist and turn; the last one must answer                                                                                 |
| `AI_HELPDESK_TOOL_LOOP_MAX_TOOL_CALLS`  | 4        | Tool calls per turn; a repeat of a lookup already made this turn is free                                                                      |
| `AI_HELPDESK_TOOL_LOOP_MAX_DELEGATIONS` | 1        | `order_data_query` calls the model may start                                                                                                  |
| `AI_HELPDESK_TOOL_LOOP_DEADLINE_S`      | derived  | Wall clock for the loop: the slowest tool's timeout + 15 s for the answer + 20 s lead-in, at least 75 s (127 s with `SQL_AGENT_TIMEOUT_S=90`) |
| `AI_HELPDESK_TOOL_LOOP_TOOLS`           | built in | Per-specialist allowlist override, e.g. `technical=knowledge_search` to keep a specialist off the business systems                            |

The startup log states the deadline in use and warns when an explicit one leaves no room for `order_data_query`. A delegation that already timed out during the rule prefetch is not retried by the model in the same turn. If the endpoint rejects tool calls, the turn falls back to a single answer and `GET /health` reports `tool_loop.tools_rejected`.

[run_trajectory_eval.py](../backend/scripts/run_trajectory_eval.py) compares the loop with the rule-only path on 40 trajectory cases. It calls the model only with `--yes`, prints its call estimate first, and replays order data from the [a2a-contract](https://github.com/heyemlee/a2a-contract) fixtures.

## Keep a record of each turn

`AI_HELPDESK_EVENT_LOG=full` records everything a turn does, in order: memory, the intent vote, the prefetch plan and both rule lookups, routing, every model call (intent, rewrite, rerank, answer, summary, profile) with its tokens, every tool call the loop starts, answer-guard replacements, handoff and ticket, and the final answer, or why there was none. Events go to the `turn_events` table in Postgres, written in batches outside the answer path. If a write fails, the log loses events and the reply goes out unchanged; `GET /health` reports `event_log.dropped`. Without Postgres the last 200 turns are kept in memory.

Each event has a structure half (enums, numbers, ids, hashes) and a text half (what the customer, the model, the knowledge base or the business systems said). The text expires first, so an old turn still shows its routing, scores and outcome without the words.

| Variable                          | Default | Meaning                                                                                              |
| --------------------------------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `AI_HELPDESK_EVENT_LOG`           | `off`   | `full` records structure and text, `meta` structure only, `off` nothing. A `turn_id` is returned either way. Set it in `backend/.env`, like the tool loop; both instances read that file |
| `AI_HELPDESK_INSTANCE`            | unset   | `staff` or `customer`. Both instances share one table; with the log on and this unset, the log stays off |
| `AI_HELPDESK_EVENT_LOG_BODY_DAYS` | 30      | Days before the text is cleared (14 on the customer instance in compose); `0` keeps it               |
| `AI_HELPDESK_EVENT_LOG_DAYS`      | 180     | Days before whole rows are deleted; `0` keeps them. The service purges once a day                    |
| `AI_HELPDESK_EVENT_LOG_READ`      | `on`    | The read endpoints below; compose turns them off on the customer instance                           |

With reads on and the staff key, `GET /turns/{turn_id}` returns a past turn as the same `meta`, `routing` and `result` the stream sent, and the console uses it to reopen the inspector and Learning mode after a refresh and to show a ticket's handed-off turn. `GET /conversations/{conv_id}/turns` lists a conversation's turns, and `GET /turns/{turn_id}/events` returns the raw events. Backups made by `scripts/docker-deploy.sh` keep the table but not its rows.

Two scripts run on the host against `POSTGRES_DSN`:

- [turn_events.py](../backend/scripts/turn_events.py) `show`, `check` and `forget` read one instance's turns, report turns with missing events, and delete a conversation's or a user's events (with `--yes`). A user's conversations are found in the turn log's text and in the audit log, so turns whose text has already expired are still found; the audit log itself is left alone.
- [replay_turns.py](../backend/scripts/replay_turns.py) replays recorded turns through the current routing (`route`) or answer guard (`guard`) and exports them as trajectory cases (`export`), all without a model call. `answer` re-answers with the current model and only runs with `--yes`.

## Add your business knowledge

Upload `.txt`, `.md` or JSON document lists in the knowledge page, or use the API:

```bash
curl -X POST http://localhost:8080/knowledge/upload \
  -F 'file=@backend/data/demo_docs/troubleshooting.md'
```

Knowledge supplies reference material; SOPs define handling steps and escalation rules. Keep each specialist's procedure in `backend/skills/*/SKILL.md` and call `POST /skills/reload` after editing. See [SOP authoring](../backend/skills/README.md).

The bundled domain is cabinet manufacturing and fulfilment. Replacing the knowledge and SOPs adapts the content; a different intent taxonomy or specialist lineup also needs changes to intent recognition and routing.

For live order data, set `SQL_AGENT_URL` and its credentials to connect [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent). Set `SQL_AGENT_TRANSPORT=a2a` to reach it over A2A, which streams its stages (table search, planning, SQL, queries, repair) to the console; the default, `query`, calls its synchronous HTTP `/query` and shows no progress. The identity requirements are in [sql-agent-identity-requirements.md](integrations/sql-agent-identity-requirements.md).

## Run from source

Use Python 3.12, Node.js 24 and Redis. After preparing the environment files above, from the repository root:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm ci)
docker compose up -d redis
```

Set `REDIS_URL` in `backend/.env` to `redis://:<REDIS_PASSWORD from the root .env>@127.0.0.1:6379/0`, URL-encoding special characters in the password. With `POSTGRES_DSN` unset, persistence falls back to SQLite; Redis is still required. For local evaluations, set `EVAL_BASELINE_PATH=./data/eval/baseline.json`.

```bash
scripts/dev.sh
```

Open [localhost:5173](http://localhost:5173). The backend listens on `API_PORT` (default `8000`) and the console's proxy follows it.

To run it with ai-sql-agent and the local setup from [a2a-contract](https://github.com/heyemlee/a2a-contract#2-enable-a2a-collaboration), check out ai-sql-agent next to this repository (or set `SQL_AGENT_DIR`) and run:

```bash
scripts/dev-all.sh
```

It starts ai-sql-agent on `127.0.0.1:8000` (its own `AI_SQL_PORT`), waits for its `/health` and Agent Card, then runs `scripts/dev.sh` with `SQL_AGENT_URL=http://127.0.0.1:8000`. Put `API_PORT` on another port such as `8080`; the script refuses to start when both use the same one. Ctrl-C stops all three processes, and running it again stops the previous copies first.

## Production settings

With `APP_ENV=production` the service refuses to start unless `AI_HELPDESK_API_KEY`, `AI_HELPDESK_AUTH_SECRET` and `AI_HELPDESK_PROTECT_CHAT` are set and `AI_HELPDESK_DEPLOYMENT_SCOPE` is declared. Management endpoints then need `X-API-Key`, and user identity travels in `X-User-Token`, issued by `POST /auth/token` to a trusted integration ([login integration](auth-token-integration.md)).

The system is single-tenant. Signed identities isolate each user's memory, but one downstream service key governs business-data access. [Deployment boundaries](deployment-boundaries.md) lists what per-customer isolation would take.

## API

| Endpoint                                                                         | Purpose                                                                                                                                                 |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /chat` · `POST /chat/stream`                                               | A reply with routing metadata, policy findings, handoff state and, with the tool loop on, its trajectory; the stream adds preparation and tool progress |
| `GET /tickets` · `GET /tickets/{ticket_id}` · `POST /tickets/{ticket_id}/status` | List, read and move tickets; each reports its allowed transitions and notification record                                                               |
| `GET /conversations/{conv_id}`                                                   | The transcript behind a ticket                                                                                                                          |
| `GET /knowledge/documents` · `GET /knowledge/documents/{doc_id}`                 | Browse documents (optional `category`; `q` filters title and text) and read one with its passages                                                       |
| `GET /knowledge/categories` · `PATCH /knowledge/documents/{doc_id}`              | Category counts; move a document to another category (retrieval is unaffected)                                                                          |
| `POST /knowledge/add` · `POST /knowledge/upload`                                 | Import documents, optionally into a category; re-importing identical text is a no-op                                                                    |
| `POST /search`                                                                   | The chat path's retrieval (rewrite, hybrid recall, rerank); hits name their source document                                                             |
| `GET /skills` · `GET /sops` · `POST /skills/reload`                              | SOP metadata and text; reload from disk                                                                                                                 |
| `POST /eval/run` · `POST /eval/promote`                                          | Run the LLM-as-judge evaluation; promote its result to the baseline                                                                                     |
| `GET /admin/model` and `/admin/model/*`                                          | Model connections: status, model lists, test, save, restore                                                                                             |
| `GET /health` · `GET /monitor` · `GET /metrics`                                  | Health (including whether the tool loop is on), agent and tool monitoring, Prometheus metrics                                                           |
| `POST /auth/token`                                                               | Issue a signed user identity to a trusted integration                                                                                                   |

## Repository map

```text
backend/app/
  api/ · core/             HTTP and SSE, intent, identity, answer policy, model adapters
  agents/ · tools/         Specialist routing, retrieval, business-data delegation, tool governance
  memory/ · storage/       Working and long-term memory, persistence, conversation audit
  tickets/                 Tickets and the notification outbox
  monitor/ · evaluation/   Runtime feedback and quality regression
backend/skills/            Scoped, reloadable SOPs
backend/tests/ · scripts/  Tests, retrieval evaluation, re-embedding, load testing
frontend/src/              The staff console
deploy/                    Nginx, metrics, logs, tracing and alert configuration
scripts/                   Local development, local CI, deployment, backup and restore
docs/                      Architecture, integration, evaluation and operations records
```

More: [architecture](architecture.md) · [code guide](code-guide.md) · [diagrams](diagrams/README.md).
