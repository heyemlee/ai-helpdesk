# ai_sql_agent.execution

MCP routing and execution.

## Layout

```text
src/ai_sql_agent/execution/
├── __init__.py
├── objects.py                # Execution request and result
├── sqlite_executor.py        # In-process SQLite executor (default)
├── mcp_protocol_executor.py  # Real MCP stdio protocol executor
└── router.py                 # Routes by database name to an executor
```

## Execution governance

Both executors share the same constraints:

- **Read-only**: only SELECT / WITH are allowed (mirroring a read-only account);
- **Row cap**: at most `max_rows` rows per query (default 1000); beyond that the
  result is truncated and `truncated` is set;
- **Byte budget**: all values of one result together may not exceed
  `max_result_bytes` (default 8 MiB). A result cut by it is `truncated`, and
  `row_limit` is the number of rows actually returned; a first row over the budget is
  an error. SQLite rows are fetched one at a time, so peak memory is the budget plus
  one row. PostgreSQL only bounds what is returned (the driver has already received its
  batch);
- **Value length** (SQLite): `max_value_bytes` (default 8 MiB) is installed as
  `SQLITE_LIMIT_LENGTH`. The progress handler below cannot interrupt a single function
  call, so `hex(randomblob(150000000))` is stopped here, before it allocates;
- **Execution timeout**: after `max_execution_seconds` (default 10s) a progress
  handler aborts the query;
- **Cancellation** (inside a run, see [runs](runs.md)): a cancelled run never reaches
  the database. A statement already running is interrupted at once: SQLite through the
  same progress handler, PostgreSQL through a server-side cancel request
  (`cancel_safe()`). The error reads "查询已取消。", so it is never mistaken for a timeout;
- **Database-layer authorizer** (SQLite, read-only executors): a default-deny
  `set_authorizer` that allows only `SELECT` / `READ` / `RECURSIVE` and non-denied
  functions. `ATTACH`, `VACUUM INTO`, `PRAGMA`, writes, `sqlite_*` / `pragma_*` /
  `dbstat` are refused by the engine, which `mode=ro` alone does not do. It is installed
  on the executor's own connection only: schema loading and fingerprints open theirs.

The API service fills these from `AI_SQL_MAX_ROWS`, `AI_SQL_MAX_EXECUTION_SECONDS`,
`AI_SQL_MAX_RESULT_BYTES` and `AI_SQL_MAX_VALUE_BYTES` (through `PipelineConfig`) for the in-process executors. The MCP protocol path does not
read them: the stdio server keeps the defaults above.

## Two execution paths

```text
Default (fast, offline):
  Pipeline → MCPRouter → SQLiteMCPExecutor → SQLite

MCP protocol (PipelineConfig(use_mcp_protocol=True)):
  Pipeline → MCPRouter → MCPProtocolExecutor
    → MCP stdio tool call → ai_sql_agent.mcp_server.execute_sql → SQLite
```

## Request format

```json
{
  "database": "erp_db",
  "sql": "SELECT ..."
}
```
