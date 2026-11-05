# ai_sql_agent.retrieval

Column-level schema retrieval, with runnable demos.

---

## Layout

```text
src/ai_sql_agent/retrieval/
  __init__.py

  # Core structures and schema handling
  objects.py                     # Core data structures
  sqlite_loader.py               # SQLite schema parsing
  document_builder.py            # Column-level document construction
  graph_builder.py               # Schema subgraph construction

  # Keyword retrieval
  tokenizer.py                   # Mixed Chinese/English tokenizer
  bm25.py                        # BM25 keyword index
  retriever.py                   # Keyword retrieval entry point

  # Vector retrieval
  keyword_extractor_client.py    # Model-driven keyword extraction
  embedding_client.py            # Embedding client
  vector_index.py                # In-memory vector index

  # Fusion and reranking
  rrf_fusion_client.py           # RRF fusion
  rerank_client.py               # Reranking
```

Demos live in `examples/`: `keyword_recall.py`, `vector_recall.py`, `hybrid_recall.py`, `hybrid_rerank_schema.py` and `hybrid_schema_retrieval.py`.

---

## Environment variables

The demos run end to end against a local mock with nothing configured.

To call a real model:

```bash
export DASHSCOPE_API_KEY="your-api-key"
```

Reranking needs one more:

```bash
export DASHSCOPE_WORKSPACE_ID="your-workspace-id"
```

---

## Commands

### 1. Keyword retrieval

```bash
python examples/keyword_recall.py
```

### 2. Vector retrieval

```bash
python examples/vector_recall.py
```

### 3. Hybrid retrieval

```bash
python examples/hybrid_recall.py
```

### 4. Hybrid retrieval + rerank + SchemaGraph

```bash
python examples/hybrid_rerank_schema.py
```

---

## Clearing caches

```bash
rm -rf src/ai_sql_agent/retrieval/__pycache__
```
