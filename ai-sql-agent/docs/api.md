# API and repository map

[English](api.md) | [简体中文](api.zh-CN.md)

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /query` | Answer, per-step SQL/results, risk flags, and trace information |
| `POST /a2a` | A2A JSON-RPC: `message/send` returns a finished task; `message/stream` sends stage events over SSE, then the same fields as `/query` except `reason` |
| `GET /.well-known/agent.json` · `GET /a2a/databases` | A2A agent card, and the business databases it currently serves |
| `GET /history` | Query history; own records by default |
| `GET /runs/{run_id}` · `GET /runs/{run_id}/events` | One request's run: status, entry, usage and outcome, and its numbered event log (stages, model calls, SQL executions); own runs only, administrators excepted |
| `POST /runs/{run_id}/cancel` | Stop a run that is still going; `409` once it has finished, `404` for a run that is not yours |
| `GET /whoami` | The identity this request resolves to (same resolution as `/query`) and what the service supports |
| `GET /catalog` | Data catalog the caller may see: domains, tables, fields, units, business terms |
| `POST /catalog/{database}/{table}/rows` | Read-only, paginated preview with search / filter / sort; no SQL accepted |
| `POST /catalog/{database}/{table}/profile` | Aggregates only, never rows: record count, how many values each field has, value breakdowns, records per month; same filters as the preview |
| `POST /memory/save` | Save personal long-term memory |
| `POST /feedback` · `GET /feedback/stats` | Answer feedback and administrator statistics |
| `GET /health` · `GET /readyz` | Liveness and readiness |
| `GET /metrics` | Prometheus-format metrics |
| `POST /admin/schema/reload` | Administrator-triggered schema index rebuild |
| `/admin/model` · `/admin/model/embedding` | Administrator: test, save or clear the model and embedding connections used by the console's settings |

`POST /query` includes `answer`, `has_risk`, `degraded`, `trace_id`, and per-step execution information.

Every `/query` and A2A request is one run. `/query` returns its id in the `X-Run-Id` header, and an A2A task id is the run id. The run's event log goes up to `GET /runs/{run_id}/events?after=<seq>&limit=<n>`. A cancel takes effect at the next checkpoint:

- SQL stops at once.
- A model call already in flight is let finish (at most the model timeout), its result is discarded, and no further call is made.
- The audit row is still written, with status `499`, and it still counts toward the daily quota.

When the caller disconnects, the run is cancelled at the next checkpoint (A2A contract v1.13 §5.4). This is on by default; set `AI_SQL_CANCEL_ON_DISCONNECT=false` to let a query run to the end instead.

On A2A, a run cancelled while the caller is still connected ends as `canceled`: the stream's final frame and the task returned by `message/send` both carry `state: "canceled"` and `metadata.error` such as `cancelled: api` (contract §4.4). Rate limits, quotas and a full stream admission answer HTTP `429` with JSON-RPC code `-32029` and a `Retry-After` header, before any stream opens (contract §3.2 / §5.5).

The catalog endpoints reuse the caller identity and table/column policy of `/query`. A preview is built server-side from an allowlist of real, visible columns, binds every value as a parameter, and runs through the same read-only executor and authorization gate as generated SQL. What it fetches is bounded in the database, not afterwards: long text is cut at 2,000 characters and binary fields return only their size. The profile behind the Data page's charts follows the same rules and returns aggregates only; a field's breakdown ignores that field's own filter, so a chosen value can be swapped for another.

## Example

On the built-in demo databases:

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","session_id":"s1","query":"list suppliers whose on-time rate is below 0.9"}'
```

With `AI_SQL_AUTH_MODE=api_key`, add `-H 'X-API-Key: …'`; keys and their table and column policies are described in [deployment](deployment.md#3-permissions).

## Repository map

```text
src/ai_sql_agent/
  api/ · catalog/ · a2a/ · mcp_server/   Web console, HTTP API, data catalog, and agent interfaces
  pipeline/ · normalization/             Routing, question normalization, orchestration
  semantic/ · retrieval/                 Business semantics and SchemaGraph retrieval
  indexing/ · memory/                    Vector indexes, sessions, long-term memory
  planning/ · generation/                Cross-database plans and SQL generation
  execution/ · repair/                   Guarded execution and SQL repair
  validation/ · feedback/                Result checks and feedback loop
  auth/ · audit/ · core/ · ops/          Permissions, audit, runtime, read-only role tools
  runs/                                  Per-request run records, event log and cancellation
tests/ · benchmark/                      Tests, datasets, baselines, release gate
examples/ · tools/                       Runnable examples and simulated ERP data (sim_erp, simple_erp)
deploy/                                  Docker image and Compose stacks
docs/                                    Deep dives, architecture diagram, screenshots
```
