# ai_sql_agent.pipeline

End-to-end Text2SQL orchestration. Runnable demos live in `examples/`.

## Layout

```text
src/ai_sql_agent/pipeline/
├── __init__.py
├── objects.py                # Pipeline data structures
├── text2sql_pipeline.py      # End-to-end orchestration
├── dynamic_service.py        # Intent-routed entry point (DynamicSqlAgentService)
├── routing.py                # Intent classification: model, with rule fallback
├── short_path.py             # Short path: replay a certified statement for the same question
├── conversation_service.py   # Memory-aware pipeline wrapper
├── data_qa.py                # Data Q&A path: explain results, define metrics
├── schema_budget.py          # Three-tier schema trimming to the token budget
├── token_budget.py           # Token estimation and budget allocation
├── provenance.py             # Databases and tables a statement touched
├── local_clients.py          # Local mock keyword extraction and embedding
└── demo_data.py              # Demo database creation and business schema metadata

examples/
├── sql_execution.py          # Minimal MCP SQL execution demo
├── end_to_end.py             # Full Text2SQL demo
└── memory_end_to_end.py      # Dynamic routing with short- and long-term memory
```

## 1. Create the demo database

`examples/sql_execution.py` creates it on first run; from Python:

```python
from ai_sql_agent.pipeline.demo_data import create_erp_demo_database

create_erp_demo_database("runtime_data/erp_demo.db")
```

Produces:

```text
runtime_data/erp_demo.db
```

The seed SQL lives in:

```text
src/ai_sql_agent/resources/demo_sql/create_erp_demo.sql
```

## 2. Test the execution path only

```bash
python examples/sql_execution.py
```

This demo calls no model; it only exercises MCP routing and SQLite execution.

## 3. Run the end-to-end pipeline

```bash
python examples/end_to_end.py
```

## 4. Run dynamic routing with short- and long-term memory

```bash
python examples/memory_end_to_end.py
```

`DynamicSqlAgentService` is the single entry point:

- `database_query`: runs schema retrieval, planning, SQL generation and execution;
- `data_qa`: explains results, defines metrics, answers follow-ups about past
  results, and handles analysis and general business questions — no SQL is run;
- short-term context is loaded before routing, and long-term memory is recalled
  only if the user has enabled it;
- with a model API key configured, routing is model-classified, falling back to
  rules when no model is available or its output is malformed;
- the routing decision is written into the assistant message's memory metadata.

Example:

```python
from ai_sql_agent.memory import ConversationMemoryService
from ai_sql_agent.pipeline import Text2SQLPipeline, DynamicSqlAgentService

memory = ConversationMemoryService()
service = DynamicSqlAgentService(Text2SQLPipeline(), memory)

response = service.run(
    user_id="user-001",
    session_id="session-001",
    query="list suppliers whose on-time rate is below 0.9",
)

print(response.decision.route.value)
print(response.decision.reason)
print(response.queried_database)
print(response.answer)
```

Routing tests on their own:

```bash
python tests/test_routing.py
```

## 5. Run the memory-enabled pipeline via script

```bash
make demo-memory
```
