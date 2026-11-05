# Design decisions

[English](design.md) | [简体中文](design.zh-CN.md)

| Engineering problem | Implementation | Source |
| --- | --- | --- |
| Business language differs from column names | Editable YAML descriptions, aliases, and metric definitions shared by retrieval, planning, and metric explanations | [semantic](../src/ai_sql_agent/semantic) |
| Large schemas exceed prompt budgets | Database → table → column retrieval; BM25 + vectors, RRF fusion, and reranking; trim SchemaGraph by relevance while prioritizing join keys | [retrieval](../src/ai_sql_agent/retrieval) · [schema budget](../src/ai_sql_agent/pipeline/schema_budget.py) |
| One SQL JOIN cannot span independent databases | Bind each step to a database, inline intermediate results as filters, check plan/database consistency, and reject references outside the step's database | [pipeline](../src/ai_sql_agent/pipeline/text2sql_pipeline.py) |
| Valid SQL can return the wrong answer | Annotate JOIN cardinality, prompt for de-duplication and primary-key projection, then check empty results, cardinality, and semantic consistency | [graph_builder](../src/ai_sql_agent/retrieval/graph_builder.py) · [validation](../src/ai_sql_agent/validation) |
| Generated SQL exceeds the user's permissions | Filter model-visible schema; analyze tables/columns/scopes with SQLGlot, expand views to their base tables, and deny unparseable SQL; then a read-only guard on the SQLGlot AST, a default-deny SQLite authorizer, and read-only connections | [AST analysis](../src/ai_sql_agent/auth/sql_analysis.py) · [views](../src/ai_sql_agent/auth/views.py) · [execution](../src/ai_sql_agent/execution) |
| SQL execution or model calls fail | Localized SQL repair, at most 2 attempts by default; on model failure, try the same user's previously successful SQL for the same question, reauthorize it, and explicitly report degradation | [repair](../src/ai_sql_agent/repair) · [fallback logic](../src/ai_sql_agent/pipeline/text2sql_pipeline.py) |
| Failures are hard to reproduce | Link SQL, databases/tables, latency, repairs, and token usage through `trace_id`; export user feedback as eval cases and use confirmed queries as generation examples | [audit](../src/ai_sql_agent/audit) · [feedback](../src/ai_sql_agent/feedback) |

Default execution limits are **1,000 rows, 10 seconds, and 8 MiB per result** (SQLite also caps a single value at 8 MiB), configurable through [PipelineConfig](../src/ai_sql_agent/pipeline/objects.py) and the `AI_SQL_MAX_*` variables; see [Execution limits](deployment.md#execution-limits). Result checks surface risk; they do not guarantee business correctness.

## Further reading

[Project walkthrough (中文)](walkthrough.zh-CN.md) · [Deployment](deployment.md) · [Authorization model (中文)](authz-model.md) · [Least privilege (中文)](least-privilege-runbook.md) · [Schema retrieval](modules/retrieval.md) · [Execution](modules/execution.md)
