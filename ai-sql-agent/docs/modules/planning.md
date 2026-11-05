# ai_sql_agent.planning

Chain-of-thought planning, sitting between schema retrieval and SQL generation.

## Layout

```text
src/ai_sql_agent/planning/
├── __init__.py
├── objects.py             # Plan data structures
├── prompt_builder.py      # Prompt assembly
├── thinking_client.py     # Thinking-model calls, streaming supported
└── cot_planner.py         # Planning entry point: plan and plan_stream
```

The streaming demo lives in `examples/cot_planning.py`.

## Running

```bash
python examples/cot_planning.py
```

With no environment variables set, the demo streams from a mock.

To use a real model:

```bash
export DASHSCOPE_API_KEY="your-api-key"
export DASHSCOPE_COT_MODEL="qwen-plus"

python examples/cot_planning.py
```
