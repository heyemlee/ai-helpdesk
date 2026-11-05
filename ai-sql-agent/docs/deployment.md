# Deployment and operations

[English](../README.md) | [简体中文](../README.zh-CN.md)

Written for an internal single-instance deployment: no multi-tenancy, the service
verifies caller identity itself.

## 1. First deployment

```bash
# 1. Generate the API key configuration
cp deploy/api_keys.example.json deploy/api_keys.json
python -c "import secrets; print(secrets.token_urlsafe(32))"   # one per caller
# Edit deploy/api_keys.json: replace the keys and adjust roles / policies

# 2. Configure model credentials
cat > .env <<'ENV'
AI_SQL_MODEL_PROVIDER=openai          # or anthropic
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.openai.com/v1
# ANTHROPIC_API_KEY=sk-ant-xxx
# ANTHROPIC_BASE_URL=https://api.anthropic.com
POSTGRES_PASSWORD=use-a-strong-password
ENV

# 3. Start the stack
docker compose --project-directory . -f deploy/docker-compose.prod.yml up -d --build

# 4. Verify
curl -s localhost:8000/health
curl -s localhost:8000/readyz | python -m json.tool
```

`deploy/api_keys.json` is listed in `.dockerignore`, so it never enters the image;
it is mounted read-only instead. Rotating a key means editing the file and running
`docker compose --project-directory . -f deploy/docker-compose.prod.yml restart app`.

## 2. Configuration

| Environment variable | Production value | Notes |
| --- | --- | --- |
| `AI_SQL_MODEL_PROVIDER` | `openai` | `openai` = OpenAI-compatible protocol, `anthropic` = Messages protocol |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | per vendor | Credentials and endpoint when provider=openai |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | per vendor | Credentials and endpoint when provider=anthropic |
| `AI_SQL_MODEL_SETTINGS_ENABLED` | `true` for one instance, `false` for several replicas | Lets an admin change the model connection from the console; see "Changing the model connection from the console" in §8.2 |
| `AI_SQL_MODEL_SETTINGS_PATH` | `runtime_data/model_settings.json` | Where a connection saved from the console is kept. Contains the key in plain text, mode 0600 |
| `AI_SQL_AUTH_MODE` | `api_key` | `disabled` lets every request through — local development only |
| `AI_SQL_API_KEYS_FILE` | `/run/secrets/api_keys.json` | Keys and data-permission policies |
| `AI_SQL_SEMANTIC_DIR` | `semantic/` | Business metadata for your databases. **Required for real deployments** — see §2.6 |
| `AI_SQL_FEEDBACK_ENABLED` | `true` | Collect right/wrong verdicts from users |
| `AI_SQL_CERTIFIED_SQL_ENABLED` | `true` | Reuse confirmed-correct queries as reference examples |
| `AI_SQL_PGVECTOR_DSN` | internal PG | Shared by schema vectors, long-term memory and audit |
| `AI_SQL_MAX_ROWS` | `1000` | Row cap per query; rows beyond it are dropped and the step is marked `truncated`. See "Execution limits" below |
| `AI_SQL_MAX_EXECUTION_SECONDS` | `10` | Per-query timeout, aborted on the database side |
| `AI_SQL_MAX_RESULT_BYTES` | `8388608` | Bytes of all values in one result; rows beyond it are dropped and the step is marked `truncated`. See "Execution limits" below |
| `AI_SQL_MAX_VALUE_BYTES` | `8388608` | SQLite only: longest single string / BLOB / row (`SQLITE_LIMIT_LENGTH`); a longer value aborts the statement |
| `AI_SQL_HTTP_MAX_RETRIES` | `2` | Backoff retries on model 429/5xx |
| `AI_SQL_DEFAULT_RATE_LIMIT_PER_MINUTE` | `60` | Default when a key has no override |
| `AI_SQL_DEFAULT_DAILY_TOKEN_QUOTA` | per budget | 0 means unlimited; set a cap to protect the model bill |
| `AI_SQL_SCHEMA_REFRESH_SECONDS` | `300` | Schema-change poll interval; 0 disables it |
| `AI_SQL_RUNS_ENABLED` | `true` | One run record and numbered event log per request (`GET /runs/{id}/events`). It goes to PostgreSQL when `AI_SQL_PGVECTOR_DSN` is set, otherwise to `AI_SQL_RUNS_DB_PATH` (default `runtime_data/runs.db`). Losing it loses history only |
| `AI_SQL_RUNS_RETENTION_DAYS` | `7` | Runs and their events are deleted this many days after they finish |
| `AI_SQL_CANCEL_ON_DISCONNECT` | `true` | Cancel a run at the next checkpoint when the caller disconnects (A2A contract v1.13 §5.4). `false` lets the query run to the end, as under v1.12; the console's stop button cancels through `/runs` either way |
| `AI_SQL_RUN_DEADLINE_SECONDS` | `0` | Hard cap on one run, enforced at the next checkpoint; 0 = none. Keep it above the caller's own limit |
| `AI_SQL_LOG_FORMAT` | `json` | For log collectors |

### Execution limits

`AI_SQL_MAX_ROWS` and `AI_SQL_MAX_EXECUTION_SECONDS` are handed to every database
executor when the API starts, and the `api.startup` log line prints the values in
force. Unset, they are 1000 rows and 10 seconds.

- **Upgrading: check what your own configuration says.** Earlier versions only logged
  these two variables: queries always ran with 1000 rows / 10 s, whatever was
  configured. The compose files in this repository used to set `10000` / `30`. A copy of
  them, or an `.env` that still carries those numbers, takes effect on the next restart
  after upgrading.
- **The byte limits bound result size, which the row cap cannot.** One row of 5 MB
  is still one row. `AI_SQL_MAX_RESULT_BYTES` caps the sum of all values returned
  (a cut result is marked `truncated`, and its `row_limit` is the number of rows
  actually returned); a first row that is already over it is an error, not an empty
  page. `AI_SQL_MAX_VALUE_BYTES` is SQLite's `SQLITE_LIMIT_LENGTH`: it stops
  `hex(randomblob(150000000))`-style single calls (which the timeout cannot
  interrupt) before they allocate. Rows already stored longer than it cannot be
  read, so raise it before storing larger values rather than after.
  PostgreSQL has no per-value session setting: there the budget bounds what is
  returned, not what the driver receives (use `left()` or a role that cannot see the
  column).
- **SQLite read-only executors also install an authorizer** that denies everything
  except reading data: `ATTACH`, `VACUUM INTO`, `PRAGMA` and `sqlite_*` / `pragma_*` /
  `dbstat` are refused by the engine even if the application layer is bypassed.
- **Raising the row cap is not free.** Every returned row travels in the `/query`
  response and the A2A data part, and the whole result is stored in conversation memory
  and handed to the memory summarizer. Raise it for a reason, and watch response size
  and the memory database afterwards.
- Both values must be positive. `0` is rejected at startup; it does not mean
  "unlimited".
- The Data page reads through the same executors. When the cap is lower than the
  requested page size, pages shrink to the cap, and the `page_size` in the response is
  the size actually used.
- The MCP stdio server (`python -m ai_sql_agent.mcp_server.server`, also used by
  `PipelineConfig(use_mcp_protocol=True)`) does not read these variables. It keeps a
  fixed 1000 rows / 10 s.
- Verified on SQLite. On PostgreSQL the same values become the fetch size and
  `statement_timeout`; that branch is covered by unit tests against a stubbed driver and
  has **not been run against a real PostgreSQL database**.

## 2.5. Where the model runs: public API or self-hosted vLLM

The project speaks two protocols — **OpenAI-compatible**
(`POST /chat/completions` + `Authorization: Bearer`) and **Anthropic Messages**
(`POST /v1/messages` + `x-api-key`) — selected by `AI_SQL_MODEL_PROVIDER`. Service
addresses and model names all come from configuration, so where the model runs and
whose model it is stays transparent to application code: switching vendor or
deployment shape **requires no code change**, only environment variables.
Self-hosted inference servers (vLLM, SGLang, Ollama) all use the OpenAI-compatible
path.

### The two shapes compared

| | Public API | Self-hosted vLLM |
|---|---|---|
| Compose file | `deploy/docker-compose.prod.yml` | `deploy/docker-compose.vllm.yml` |
| Data leaving the network | Questions and schema are sent to an external service | **Nothing leaves the network** |
| Hardware cost | None | Needs a GPU (~32GB VRAM for a 14B model in bf16) |
| Per-request latency | Subject to the public internet and vendor queueing | Depends on the local GPU, usually steadier |
| Model capability | Latest commercial models available | Limited to models that can be self-hosted |
| Fits when | Data is not sensitive and you do not want to run GPUs | Customer data, cost prices and the like must stay internal |

The deciding factor is **data compliance, not performance**. If contract amounts,
customer and supplier information, or batch unit costs are not allowed to leave
the network, self-hosting is the only option.

### Switching to self-hosted vLLM

```bash
# 1. Start vLLM (an OpenAI-compatible server)
docker compose --project-directory . -f deploy/docker-compose.vllm.yml up -d

# 2. Or, for an existing deployment, change only these variables
AI_SQL_CHAT_URL=http://vllm-chat:8000/v1/chat/completions
AI_SQL_EMBEDDING_URL=http://vllm-embed:8000/v1/embeddings
AI_SQL_MODEL_PROVIDER=openai                  # vLLM is an OpenAI-compatible server
OPENAI_API_KEY=sk-internal-vllm               # whatever vLLM's --api-key is set to
AI_SQL_CHAT_MODEL=Qwen2.5-14B-Instruct        # must match --served-model-name
AI_SQL_CODER_MODEL=Qwen2.5-Coder-14B-Instruct
AI_SQL_EMBEDDING_MODEL=bge-m3
AI_SQL_EMBEDDING_DIMENSIONS=1024
```

### Things to watch

- **The model name must match vLLM's `--served-model-name` exactly**, or vLLM
  returns 404.
- **`--max-model-len` has to cover the token budget.** A single query here budgets
  around 8k, so 16384 leaves a comfortable margin.
- **Embedding dimensions must match the index.** If a new embedding model changes
  the dimension, the vector index must be rebuilt
  (`POST /admin/schema/reload`) or the mismatch raises an error.
- **Reranking is not part of the OpenAI protocol** — it uses a vendor-specific
  format that vLLM does not implement. Leaving `AI_SQL_RERANK_URL` empty degrades
  column-level reranking to local lexical-overlap scoring: usable, but weaker than
  a cross-encoder. Restoring it means self-hosting a rerank service and adding a
  format adapter.
- **Timeouts can be tightened.** Local inference has none of the public internet's
  jitter, so `AI_SQL_HTTP_TIMEOUT_SECONDS` fits better at 45 than at 60.

### Compatibility test

`tests/test_openai_compatible_endpoint.py` stands up a minimal OpenAI-compatible
server as the endpoint under test and verifies four things: that the configuration
switches over as a whole, that chat requests and responses follow the OpenAI format
with the right auth header, that embedding does the same, and that **the whole
Text2SQL chain running against a self-hosted endpoint produces executable SQL and
real results**. Run it after touching anything in the model integration path.

```bash
python -m pytest tests/test_openai_compatible_endpoint.py -v
```

## 2.6. Business semantics: the metadata the model reads

This is the largest piece of work when connecting real databases, and it is not a
coding task. Someone who knows the business has to write down what each table and
column means, what people actually call them, and how the databases relate.

The service ships with metadata for four demo databases. Connect real ones and that
metadata no longer applies: retrieval falls back to matching column names literally,
so `safety_stock` stops resolving for "we're running low on stock". The symptom is
"the model is not accurate", but the model was never told what the business means.

```bash
python -m ai_sql_agent.semantic export semantic/     # export demo metadata as a template
# edit semantic/*.yaml, one file per database, plus _shared.yaml
python -m ai_sql_agent.semantic validate semantic/   # compare against the live schema
python -m ai_sql_agent.semantic show                 # which metadata is currently active
```

Each database file carries the table and column descriptions, aliases, semantic
roles and sample values. `_shared.yaml` carries what spans databases: join keys
(these systems have no foreign keys between them, so the planner can only split
steps correctly if you declare them), metric disambiguation (which field "purchase
amount" actually means), and metric definitions (how "over budget" is computed).

`validate` connects to the real databases and reports both directions:

| Finding | Why it matters |
| --- | --- |
| Documented table or column that does not exist | Retrieval offers it to the model, the generated SQL fails |
| Table that exists but is undocumented | Invisible to the model; those questions can never be answered |
| Cross-database link pointing at an unknown column | Cross-database planning silently breaks |
| Connection string written into the file | The file goes into version control; use `dsn_env` instead |

It exits non-zero on errors, so it can gate a deploy. Run it again after every
schema change, alongside `POST /admin/schema/reload`.

Mount the directory read-only in production:

```yaml
volumes:
  - ./semantic:/app/semantic:ro
environment:
  AI_SQL_SEMANTIC_DIR: /app/semantic
```

### The same metadata feeds the console's Data page

`GET /catalog` joins this metadata with the databases' real table structure and trims
it to what the caller's policy allows, so people can see what there is to ask about
before asking. A table that is documented but missing from the database is shown as
missing, and a database that cannot be reached is shown as unavailable — never as an
empty list.

Names and descriptions for people are optional extra keys, accepted on a database, a
table or a column. They are **not** sent to the model; retrieval keeps reading
`description` and `aliases`.

```yaml
name: erp_db
label_en: Orders & Purchasing        # or `label:` for one name in every language
label_zh: 订单与采购
description_en: Orders, suppliers, purchasing and supplier payments.
tables:
  supplier:
    label_en: Suppliers
    columns:
      on_time_rate:
        description_en: Share of deliveries that arrived by the agreed date.
        business_usage_en: Find suppliers that deliver late.   # the column's `business_usage`
        unit: ratio
```

The shared file takes the same kind of addition for glossary terms and cross-database
links. The keys of `metric_definitions_en` are the terms of `metric_definitions`; there
is still one definition, this is how it reads in English:

```yaml
# _shared.yaml
metric_definitions:
  准时率: 准时率是供应商按约定交期到货的批次占比。
metric_definitions_en:
  准时率:
    term: On-time rate
    definition: Share of a supplier's deliveries that arrived by the agreed date.
cross_database_links:
  - left: erp_db.supplier.supplier_id
    right: wms_db.batch.supplier_id
    meaning: ERP 供应商编号与入库批次的供应商编号同域
    meaning_en: Same set of suppliers in both systems.
```

Without them the page falls back, in order, to the first alias in the reader's language
and then to the technical name spelled out; databases named after a common system type
(`erp_db`, `pm_db`, `mes_db`, `wms_db`) get a default business title. A description that
exists only in another language is shown as written rather than translated.

The suggested questions on the Ask page are assembled from this catalog too; the page
carries no question of its own. A `filter` or `dimension` column whose `value_range`
declares its values as a list (`open / shipped / closed`) gives "count by" and "list
where" questions, a numeric `metric` gives "top 10" and "average by", and a `time` column
gives "per month" — each worded with the table and column labels above. Totals are never
suggested: the dictionary does not say whether a metric can be added up, and a rate or a
unit price cannot. Tables whose columns have no roles get no suggestions, and a database
that cannot be read is left out. A suggestion is only text placed in the question box for
the person to edit and send. With the catalog turned off there are no suggestions; asking
still works.

Limits for the read-only preview are `AI_SQL_CATALOG_*` (see `.env.example`);
`AI_SQL_CATALOG_ENABLED=false` turns the Data page off without affecting questions —
the console takes the caller's identity from `GET /whoami`, which stays available.

What the preview does and does not do:

- Browsing has its own rate bucket (`AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE`), so paging
  through a table does not use up a caller's question budget. A key whose own
  `rate_limit_per_minute` is lower is held to the lower value.
- Values are bounded in SQL: text is cut at 2,000 characters (the response says when a
  cell was cut) and binary columns return their size only. Filters and sorting still use
  the full value.
- Business terms are filtered like tables: a definition that names a field the caller
  cannot see is withheld.
- The charts on the Data page come from `POST /catalog/{database}/{table}/profile`:
  record count, how many values each field has, value breakdowns and records per month.
  It returns aggregates only, covers exactly the columns the preview shows, takes the
  same search and filters, and shares the browsing rate bucket. A field's breakdown
  ignores that field's own filter, so a chosen value can be swapped for another. Each
  aggregate is a full scan bounded by `AI_SQL_CATALOG_PREVIEW_TIMEOUT_SECONDS`, and one
  request stops starting new scans after twice that; breakdowns that did not finish are
  reported as skipped rather than shown as empty. Results are cached for 15 seconds per
  caller and user. `AI_SQL_CATALOG_PROFILE_ENABLED=false` turns the statistics off and
  leaves the catalog and the preview as they are.
- Table or field names that the read-only safety check rejects (it scans the whole
  statement for write keywords, quoted identifiers included — `update`, `Last Update`,
  `set`) are listed in the catalog but marked as not previewable. Questions that touch
  such a column are refused by the same check.
- A database file that is missing, or present but empty (0 bytes), shows as unavailable.
  Loading the schema no longer creates the file when it is missing.

## 2.7. Feedback: how the system gets better with use

The console shows a "was this right?" prompt under every answer. That verdict is the
only data source that does not depend on us writing the test questions ourselves.

- **Wrong** goes on a to-fix list. Most wrong answers trace back to missing
  metadata, and the user's comment usually names it directly.
- **Correct** becomes a certified query: the next similar question gets it as a
  reference in the generation prompt, so your house style for joins and metrics
  carries forward instead of being re-guessed each time.

```bash
python -m ai_sql_agent.feedback stats                    # live accuracy and silent-error rate
python -m ai_sql_agent.feedback wrong                    # what to fix, with the user's comment
python -m ai_sql_agent.feedback export-dataset new.json  # turn wrong answers into eval cases
python -m ai_sql_agent.feedback resolve <trace_id>       # mark one handled
```

`export-dataset` writes a draft: the question, the SQL the model produced, and the
user's comment. A person still has to supply the correct SQL — the model got it
wrong, so nothing in the system knows the right answer. Merge the completed entries
into `benchmark/dataset.json` and rerun the gate. Questions real users got wrong
then become regressions that cannot silently come back.

Two numbers matter operationally, both from `feedback stats`:

| Metric | Reading |
| --- | --- |
| Live accuracy | Correct over decided verdicts. Compare against the benchmark; when they diverge, trust this one |
| Live silent-error rate | Wrong answers that carried no risk warning. The expensive kind — the user had no reason to double-check |

Feedback is scoped per user: people can only judge their own queries, and certified
examples are only reused for the user who confirmed them, since a query carries
table names that the permission layer may not grant to everyone.

## 3. Permissions

The `policies` block in `api_keys.json` decides which tables each role can see. It
takes effect in two places: during retrieval (unauthorized tables never enter the
prompt, so the model cannot see them) and before execution (SQL referencing an
unauthorized table is rejected and never reaches the database).

```json
"policies": {
  "analyst": {
    "allow_tables": ["erp_db.*", "pm_db.*", "mes_db.*", "wms_db.*"],
    "deny_columns": ["*.*.id_card_no"]
  },
  "hr": { "allow_tables": ["hr_db.*"] }
}
```

- `deny` wins over `allow`;
- a key with several roles gets the union of their permissions;
- `can_impersonate: true` is for server-side callers only, such as a BI portal.
  Never enable it on a personal key — otherwise changing `user_id` in the request
  body reads someone else's personal knowledge base.

## 4. Probes

| Endpoint | Purpose | Orchestrator setting |
| --- | --- | --- |
| `/health` | Liveness: process only, no dependency checks | livenessProbe |
| `/readyz` | Readiness: memory store, audit store, pgvector | readinessProbe |
| `/metrics` | Prometheus scrape | ServiceMonitor / scrape_config |
| `/` | Web console: Ask, Data, History (static files under `/static`, no build step) | Reachable directly on the network |

When a dependency wobbles, `/readyz` returns 503 and traffic is pulled, while
`/health` still returns 200 — that keeps the orchestrator from restarting the
process in a loop.

Kubernetes snippet:

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 15
  periodSeconds: 20
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
```

Scale out with replicas, not with uvicorn workers: the pipeline and the schema
index are shared inside the process, and every worker would build its own copy.

## 5. Metrics

`GET /metrics` exposes the following in Prometheus text format, implemented
in-project with no external dependency:

| Metric | Type | Purpose |
| --- | --- | --- |
| `ai_sql_requests_total{method,path,status}` | counter | QPS and error rate |
| `ai_sql_request_duration_seconds` | histogram | P95/P99 latency |
| `ai_sql_stage_duration_seconds{stage,status}` | histogram | Which stage is slow (retrieval/planning/generation/execution/repair) |
| `ai_sql_model_calls_total{component,status,error_category}` | counter | Model failure rate, rate-limit hits |
| `ai_sql_model_tokens_total{component,kind}` | counter | Token consumption and cost |
| `ai_sql_sql_executions_total{database,status}` | counter | SQL success rate, permission violations |
| `ai_sql_sql_repairs_total{result}` | counter | What the repair loop actually recovers |
| `ai_sql_access_denied_total{database}` | counter | Permission blocks |
| `ai_sql_limit_rejections_total{reason}` | counter | Rate-limit and quota rejections |
| `ai_sql_degraded_responses_total{reason,failed_stage}` | counter | Degradations caused by model unavailability |
| `ai_sql_schema_reloads_total{result}` | counter | Schema index rebuilds |
| `ai_sql_circuit_state{component}` | gauge | Circuit state: 0=closed, 1=half-open, 2=open |
| `ai_sql_circuit_trips_total{component}` | counter | Circuit trips |
| `ai_sql_runs_total{entry,status}` | counter | Finished runs by entry (`http_query`, `a2a_send`, `a2a_stream`) and status (`completed`, `failed`, `cancelled`, `interrupted`) |
| `ai_sql_run_cancellations_total{reason}` | counter | Cancel requests: `api`, `client_disconnected`, `deadline`, `shutdown` |
| `ai_sql_run_cancel_latency_seconds{reason}` | histogram | From a cancel request to the run actually stopping, i.e. how coarse the checkpoints really are |
| `ai_sql_run_events_dropped_total` | counter | Events that could not be stored. The queries themselves are unaffected |

Useful PromQL:

```promql
# P95 query latency
histogram_quantile(0.95, sum(rate(ai_sql_request_duration_seconds_bucket{path="/query"}[5m])) by (le))

# SQL execution success rate
sum(rate(ai_sql_sql_executions_total{status="ok"}[5m]))
  / sum(rate(ai_sql_sql_executions_total[5m]))

# Share of failures the repair loop recovers
sum(rate(ai_sql_sql_repairs_total{result="repaired"}[1h]))
  / sum(rate(ai_sql_sql_repairs_total[1h]))

# Token consumption today
sum(increase(ai_sql_model_tokens_total[24h])) by (component)
```

Alerts worth setting: model failure rate > 5%; `/query` P95 > 10s; a spike in
`ai_sql_limit_rejections_total` (the quota is too tight, or someone wrote a loop);
`/readyz` failing repeatedly; and `ai_sql_circuit_state > 0` for five minutes,
which means the model side is genuinely down.

## 6. Debugging a failed query

Every response carries a `trace_id`, both as a body field and as the `X-Trace-Id`
header. Logs and audit records are both searchable by it:

```bash
# 1. Pull the whole chain out of the logs (use jq with JSON format)
docker compose --project-directory . -f deploy/docker-compose.prod.yml logs app | grep <trace_id>

# 2. See what this query actually did, from the audit table
docker compose --project-directory . -f deploy/docker-compose.prod.yml exec postgres \
  psql -U ai_sql -d ai_sql -c \
  "SELECT created_at, caller, user_id, query, route, databases, sqls, success, error
     FROM ai_sql_audit_log WHERE trace_id = '<trace_id>'"
```

The logs show each stage: `stage.retrieval` (which databases and tables matched),
`stage.planning` (how many steps), `stage.sql_generation` (the SQL produced),
`stage.execution` (success and row count), `repair.attempt` (the repair process)
and `model.call` (model latency and retries).

The same story, in order and without grepping, is in the run's event log. The audit
row's `run_id` column (or the `X-Run-Id` header, or an A2A task id) leads to it:

```bash
curl -s http://localhost:8000/runs/<run_id>/events -H 'X-API-Key: …' | jq '.events[] | [.seq, .type, .stage, .step]'
```

Each event is one of `run.started`, `stage.started` / `stage.finished`, `model.call`,
`tool.sql`, `run.cancel_requested` and `run.finished`. Prompts, model output and
result rows are never stored in events; SQL text is kept up to 8 KiB.

## 7. Everyday operational queries

```sql
-- Today's usage per user (this is the table the quota check reads)
SELECT user_id, COUNT(*) AS queries, SUM(prompt_tokens + completion_tokens) AS tokens
FROM ai_sql_audit_log
WHERE created_at >= date_trunc('day', now())
GROUP BY user_id ORDER BY tokens DESC;

-- Recent failures
SELECT created_at, user_id, query, error
FROM ai_sql_audit_log
WHERE NOT success ORDER BY id DESC LIMIT 20;

-- Permission violations: who is trying to query tables they cannot see
SELECT created_at, caller, user_id, query, denied_tables
FROM ai_sql_audit_log
WHERE jsonb_array_length(denied_tables) > 0 ORDER BY id DESC;

-- Frequently asked questions, worth turning into fixed reports
SELECT query, COUNT(*) FROM ai_sql_audit_log
WHERE created_at > now() - interval '7 days'
GROUP BY query ORDER BY 2 DESC LIMIT 20;
```

## 8. After a schema change

With `AI_SQL_SCHEMA_REFRESH_SECONDS` set there is nothing to do: a background task
compares fingerprints on a cycle and rebuilds when they differ (look for
`schema_watcher.change_detected` → `schema.reloaded` in the logs).

To skip the wait, or to check the current state:

```bash
curl -H "X-API-Key: $ADMIN_KEY" localhost:8000/admin/schema/status
curl -XPOST -H "X-API-Key: $ADMIN_KEY" localhost:8000/admin/schema/reload
```

Both endpoints require a key with `"is_admin": true`. To force a full
re-embedding:

```sql
DELETE FROM ai_sql_vector_meta;
```

## 8.2. What happens when the model is unavailable

Not a 5xx. When the model fails during planning, generation or repair, the system
replays the SQL that last succeeded for the same question from the same user — the
data is still queried live and permissions are re-checked. With no replayable history
it returns an explicit degradation notice instead. Both the `degraded` field in the
response and the `degraded` column in the audit table record this.

If only the semantic self-check could not run (the query itself succeeded), the answer
is returned with a `self_check_not_run` risk finding, so `has_risk` is true: a result
that was not checked is not reported as one that passed.

```promql
# A spike in the degradation rate means something is wrong on the model side
sum(rate(ai_sql_degraded_responses_total[5m])) by (reason, failed_stage)
```

```sql
-- Recent degraded queries
SELECT created_at, user_id, query, degraded FROM ai_sql_audit_log
WHERE degraded ORDER BY id DESC LIMIT 20;
```

### When the credential itself is rejected

A rejected key is different from a flaky service: retrying, or letting the next stage
try, only sends more requests that are bound to fail. The single entry point for model
calls remembers it, per **service address + credential** (the key is kept only as a
salted fingerprint, never in plaintext, never in logs):

| The service says | What stops | Until |
| --- | --- | --- |
| 401, or 403 naming the key as invalid / disabled | every call with that key to that service | restart, or admin reset |
| 402, or a body that explicitly says quota / balance is exhausted | the same | restart, or admin reset |
| 403 saying the key may not use this model | that model only | restart, or admin reset |
| 429 with a `Retry-After` longer than the in-request backoff limit | that model, for `Retry-After` (capped at 5 minutes) | expires by itself |
| 403 that explains nothing | that model, for 60 seconds | expires by itself |

An ordinary 429 is still retried in place, a bounded number of times, waiting as long
as `Retry-After` says. Other keys, other services and other models are not affected.

Once a key is known to be rejected, a question costs zero model calls instead of two or
three, and the answer is the usual degraded one. `GET /readyz` lists the known faults
under `model_access` and stops reporting `model_credentials: ok`; it never probes the
model to find out. To recover, fix the key or quota and restart — or, without a restart,
call the existing admin endpoint `POST /admin/circuit/reset` (admin key required), which
also clears these. Nothing is retried automatically, and the reset does not verify the
key: the next real request does. Replacing the key from the console (next section) clears
them as well.

What this cannot do: requests already in flight when the first rejection arrives have
been sent. With N concurrent requests, up to N rejected calls go out before the rest
are stopped.

### Changing the model connection from the console

An administrator can replace the main model connection — protocol, endpoint address, key,
planning model and SQL model — under **Settings → Advanced settings → Model connection**,
without a restart. Endpoints: `GET /admin/model` (status, never the key),
`POST /admin/model/test`, `PUT /admin/model`, `DELETE /admin/model` (restore).

- **Who.** With `AI_SQL_AUTH_MODE=api_key`: an admin key, like every other `/admin`
  endpoint. With sign-in disabled: only requests made directly on the machine the service
  runs on — a loopback peer, a loopback `Host`, no proxy-forwarding headers, and a
  same-origin `Origin` for browser requests. Everyone else gets a read-only summary.
  A reverse proxy on the same machine that rewrites `Host` to a loopback name and adds no
  forwarding header cannot be told apart from a local client: enable sign-in before
  exposing an instance, or switch the feature off. Inside a container, requests from the
  host's browser arrive from the bridge network, not loopback — use an admin key there.
- **What it changes.** The saved connection outranks the environment and `.env` until
  someone uses *Restore server configuration*. The chat endpoint is always rebuilt from the
  saved address plus the standard path; an `AI_SQL_CHAT_URL` set on the server is not
  reused. Rerank keeps the server's credentials and endpoint; embedding has its own
  section (below).
- **The key** is write-only: no response, log line or error message contains it; the
  status shows whether one is set and its last four characters. A key is only sent to the
  host it was entered for — moving the address to another host requires entering the key
  again (each protocol keeps its own key). It is stored in plain text, like `.env`, in
  `AI_SQL_MODEL_SETTINGS_PATH` (mode 0600; in the compose stack that file lives on the
  `ai_sql_runtime` volume).
- **Test connection** really calls the model: one very short request per model name. It
  saves nothing and does not touch the rejection state of the connection in use. Saving
  does not test by itself; the console runs one test right after a save.
- **Applying** clears the circuit breakers and recorded rejections and rebuilds the parts
  that picked "model or rules" at startup (intent routing, data Q&A, conversation
  summaries, keyword extraction). Questions already in flight finish on the old connection.
- **Trace.** Each change logs `model_settings.saved` / `model_settings.cleared` (who,
  protocol, endpoint host, models, whether the key changed — never the key). While a saved
  connection is in use, `/readyz` shows `model_settings` and the startup line says
  `model_settings=page`. A saved file that cannot be read is reported there and ignored.
- **Several replicas** each keep their own file, so a save reaches only one of them. Set
  `AI_SQL_MODEL_SETTINGS_ENABLED=false` and keep configuring through the environment.

#### Embedding for schema retrieval

Schema retrieval recalls databases, tables and columns by keyword and by vector. The
vectors are local hashes by default (`AI_SQL_EMBEDDING_BACKEND=local`): no service call,
but no semantics either, so everyday phrasing depends on keywords and on the glossary
mapping done during keyword extraction. `AI_SQL_EMBEDDING_BACKEND=remote` switches to an
OpenAI-compatible `/embeddings` endpoint (`AI_SQL_EMBEDDING_URL`, `_MODEL`, `_DIMENSIONS`,
`_API_KEY`, which falls back to `OPENAI_API_KEY`). The same can be set under
**Settings → Advanced settings → Embedding**, backed by `POST /admin/model/embedding/test`,
`PUT /admin/model/embedding` and `DELETE /admin/model/embedding`.

- **Saving rebuilds first.** Every table and column description is embedded with the new
  model before anything is saved; the new index replaces the old one only once it is
  complete. A failed test (HTTP 422), a failed rebuild (HTTP 502) or an unwritable file
  leaves the index in use and the saved file untouched. Restoring rebuilds the index with
  the server configuration.
- **Dimensions** default to 1024, which OpenAI `text-embedding-3-*` and DashScope
  `text-embedding-v4` both produce on request and which matches existing pgvector tables.
  A service that answers with another size fails the test (`dimension_mismatch`).
- **Keys.** The embedding section stores its own key. Left blank, the key already
  configured for the same host is reused — the embedding key in use, or the chat key when
  both point at the same OpenAI host; a different host requires the key again.
- **When the service fails.** At startup or on schema reload, a remote embedding that
  cannot build the index falls back to local vectors; the service starts, and
  `/admin/model` (`embedding.fallback`) and the log line `schema.embedding_fallback` say
  why. During a question, all keywords are embedded in one request and cached; if that
  request fails, the question continues on keyword recall alone and the client stops
  calling the service for 30 seconds (`retrieval.embedding_unavailable`).
- **What leaves the machine.** Table and column descriptions (not sample values) when the
  index is built, and the keywords of every question.

#### OpenAI reasoning models

For gpt-5 and later and the o-series, requests carry no temperature (these models only
accept the default), use `max_completion_tokens` (`AI_SQL_MAX_COMPLETION_TOKENS`, default
8192, which also covers the reasoning) and `reasoning_effort` (`AI_SQL_REASONING_EFFORT`,
default `low`; empty sends nothing). If an endpoint rejects one of these parameters by
name, the request is sent again without it and the adjustment is remembered for that
endpoint and model. Intent routing asks for a strict JSON schema on OpenAI-compatible
endpoints. Output that hit the length limit (`finish_reason=length`) is treated as a
failed call rather than parsed as a complete plan or SQL statement.

## 8.3. Read-only accounts on the business databases

The third layer of read-only enforcement is the database account itself. A
misconfigured account is invisible in normal operation, so the service audits it at
startup and complains in the logs (`readonly_audit.violation`); `/readyz` surfaces
the same thing under `business_db_accounts`.

```bash
# Create a least-privilege read-only role
python -m ai_sql_agent.ops provision \
  --admin-dsn "postgresql://postgres:***@db:5432/yourdb" \
  --database yourdb --host db

# Audit it (reads system catalogs only, writes nothing; a non-zero exit code means
# it failed, so this can run in CI)
python -m ai_sql_agent.ops audit --dsn "$AI_SQL_BUSINESS_DSN"
```

The role carries `statement_timeout`, `idle_in_transaction_session_timeout` and
`default_transaction_read_only = on`, so the database still holds the line if the
application-level configuration is wrong.

## 9. Backups

What needs backing up is one PostgreSQL database (schema vectors, long-term memory
vectors, audit) plus `runtime_data` in the container volume (the SQLite store of
raw memory content):

```bash
docker compose --project-directory . -f deploy/docker-compose.prod.yml exec postgres \
  pg_dump -U ai_sql ai_sql | gzip > ai_sql_$(date +%F).sql.gz
```

The memory store is `runtime_data/memory.db` under the directory the API starts in,
unless `AI_SQL_MEMORY_DB_PATH` says otherwise. Two instances on one machine need a
path each: with a shared file, a question asked on one instance is answered on the
other as "the earlier result". The file named by the setting is opened as it is —
nothing is moved or cleared when the setting changes, so pointing an instance at a
new path starts it with an empty history and leaves the old file where it was.
