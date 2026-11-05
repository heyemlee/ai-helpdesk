# ai_sql_agent.repair

Log-driven SQL repair loop.

## Flow

```text
Execution fails
  ↓
ExecutionLogParser   parse the error log → structured error (class + objects involved)
  ↓
SqlClauseLocalizer   split the SQL on top-level keywords → locate the suspect clause
  ↓
Regenerate that clause
  - With model credentials: the coder model rewrites only the offending clause
  - Offline: heuristic near-name substitution (difflib against schema table/column names)
  ↓
Re-execute; on failure keep going, up to max_attempts times
```

## Error classes covered

`no_such_table` / `no_such_column` / `ambiguous_column` / `syntax_error` /
`no_such_function` / `timeout` / `misuse_aggregate` — the SQLite error family,
extensible per dialect.

## How it plugs in

Enabled by default in the pipeline
(`PipelineConfig(enable_repair=True, max_repair_attempts=2)`). The repair process
is recorded in `StepExecutionLog.repair_attempts`, and the statement that finally
executed cleanly is recorded in `StepExecutionLog.final_sql`.

## Tests

```bash
python -m unittest tests.test_repair -v
```
