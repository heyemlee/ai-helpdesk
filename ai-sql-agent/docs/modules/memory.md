# Short- and long-term memory

This module depends on no web framework; the API layer, the dynamic router and
the Text2SQL pipeline all call it directly.

## Short-term memory

- Full raw messages are persisted in SQLite;
- a sliding window is maintained by both recent-message count and estimated token
  count;
- messages that fall out of the window are compacted incrementally on a
  background thread;
- summaries carry a version and a `through_message_id` cursor, so nothing is
  duplicated or skipped;
- a summarization run reads the last completed version, so it never blocks the
  current request;
- summaries preserve the user's goal, business definitions, filters, SQL results,
  analytical conclusions and task status;
- the model summarizer reads a bounded excerpt of each message, not the stored
  payload: up to 4,000 characters of message text and, for a query result, up to
  3,000 characters holding the question, each step's database, executed SQL, row
  count, `truncated` / `row_limit`, column names and as many leading rows as fit.
  When rows are left out the prompt says so. The limits are per message
  (`SummaryBudgetConfig`); the stored message is never shortened.

With model credentials configured (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`), the
service summarizes using the project's existing model client. Without a key it
falls back to offline extractive summarization, which keeps local runs working.
Any other `MemorySummarizer` can be injected into `ConversationMemoryService`.

### Verification and remaining limits (2026-10-06)

The closeout integration passed 798 offline tests with 23 skipped tests and
33 passing subtests, including the pinned A2A contract v1.12. Model clients were
fake or local mock endpoints; external network connections were blocked and no
live model credentials or shared databases were used. Existing vector-scoring
runtime warnings and docstring deprecation warnings remain.

The limits apply per message, not to the entire summarization call. A backlog
can still include up to `summary_batch_size` messages (50 by default), and the
previous summary is not bounded by `SummaryBudgetConfig`. The explicit
save-to-knowledge-base path (`summarize_memory`, `POST /memory/save`) still reads
the full content and metadata. Storage is unchanged.

Real-model summary quality over multi-turn conversations, real PostgreSQL
behavior and successful Helpdesk/SQL Agent integration remain unverified. The
benchmark release gate does not exercise conversation memory and cannot replace
the summary-quality check. This closeout did not run a model benchmark, restart
a service or deploy an image.

## Long-term memory

- Nothing is written unless the save method is called explicitly;
- structured and unstructured entries are stored the same way — retrieval summary
  plus raw content plus metadata plus vector;
- structured metadata covers database, tables, columns, filters, plan and SQL;
- recall is strictly isolated by `user_id`, with optional type and metadata
  filters;
- reranking is an optional injected capability; without it, results are ordered
  by vector similarity;
- the personal knowledge base is only searched when `enable_long_term=True`.

By default SQLite holds the raw content, metadata and local vectors, which is
enough for the demo. Setting `AI_SQL_PGVECTOR_DSN` (or
`MemoryServiceConfig(pgvector_dsn="...")`) switches on a pgvector index that
shares one Postgres instance with schema retrieval, so the project maintains a
single vector stack. Retrieval first filters and recalls in pgvector by `user_id`
and memory type (HNSW, cosine distance), then reads the raw content from SQLite
for metadata filtering and optional reranking. If the connection fails it falls
back to scoring the full set in SQLite.

## Basic usage

```python
from ai_sql_agent.memory import ConversationMemoryService

memory = ConversationMemoryService()

context = memory.begin_user_turn(
    user_id="user-1",
    session_id="session-1",
    query="What was the on-time rate we looked up last time?",
    enable_long_term=True,
)

# Hand context.to_prompt_context() to the chat model or the query pipeline.
memory.record_assistant_message(
    user_id="user-1",
    session_id="session-1",
    content="The result was 3.5%",
    message_type="sql_result",
    payload={"on_time_rate": 0.87},
)

# Only in response to an explicit "save to my knowledge base" action.
memory.save_structured_result(
    user_id="user-1",
    query="Look up supplier on-time rates",
    result={"on_time_rate": 0.87},
    database="erp_db",
    tables=["supplier"],
    columns=["on_time_rate"],
    sql="SELECT supplier_name, on_time_rate FROM supplier ...",
)
```

Wiring it into an existing Text2SQL setup:

```python
from ai_sql_agent.pipeline import Text2SQLPipeline, DynamicSqlAgentService

service = DynamicSqlAgentService(Text2SQLPipeline(), memory)
response = service.run(
    user_id="user-1",
    session_id="session-1",
    query="And what about the purchase orders for those?",
    enable_long_term=True,
)

# Call this only after the user confirms the save:
service.save_result_to_personal_knowledge_base(
    user_id="user-1",
    result=response.pipeline_result,
)
```

## End-to-end demo

As a plain Python file:

```bash
python examples/memory_end_to_end.py
```

Or as a module:

```bash
python examples/memory_end_to_end.py
```

The demo walks through a first Text2SQL turn, the short-term window, a
user-initiated save into long-term memory, long-term recall on the second turn,
and the final context once async summarization completes. The memory file
defaults to `runtime_data/memory_demo.db`.

The demo also exercises dynamic routing: the first turn fetches new data and goes
to `database_query`; the second turn follows up on a past result and goes to
`data_qa`, without touching the database again.

## Verifying

```bash
python -m unittest discover -s tests -v
```
