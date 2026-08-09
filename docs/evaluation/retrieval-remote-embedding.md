# Retrieval measurement with a remote embedding model

[README](../../README.md#retrieval) · [Same suite with local vectors](retrieval-ca-factory.md)

The [simulated factory suite](retrieval-ca-factory.md) rerun with a real embedding model in place of the local character n-gram vectors. Everything else is unchanged: the same corpus, cases, harness and hit rule. It measures recall and rank only; it does not run answer generation, query rewriting, or reranking.

## Conditions

- Backend source: commit `40e9547`, exported with `git archive` into an empty directory, so no local `.env` was read.
- Harness: [eval_retrieval.py](../../backend/scripts/eval_retrieval.py) `--suite ca --embedding env`.
- Corpus: 52 documents, 56 chunks: the six built-in documents plus the 46 **simulated** policies in [sample_ca_factory_knowledge.json](../../backend/data/demo_docs/sample_ca_factory_knowledge.json).
- Cases: the 122 in [retrieval_cases.json](../../backend/data/eval/retrieval_cases.json); 108 have an expected document and 14 have no answer in the corpus.
- Dense: `Qwen/Qwen3-Embedding-0.6B`, 1024 dimensions, through an OpenAI-compatible `/embeddings` endpoint. Queries carry the instruction prefix from the model card (`EMBEDDING_QUERY_PREFIX`); documents are embedded as they are. Each run embedded 178 texts: 56 chunks and 122 queries.
- Lexical: unchanged; pure-Python BM25 on SQLite, `ts_rank_cd` over a `simple` `tsvector` on PostgreSQL.
- Served: with a real embedding model, the service fuses both legs with reciprocal rank fusion (`k=60`, ten candidates per leg). The harness compared this column with `KnowledgeBase.search_async` on every query and reported no difference.
- SQLite: fresh temporary file. PostgreSQL: 16.15 from `pgvector/pgvector:pg16` in a throwaway container, tables created in a temporary schema.
- A case counts as a hit when an expected document is in the first three results; `multi` cases need both documents there.

## Reproduce

Set `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIM` and `EMBEDDING_QUERY_PREFIX` in the environment, then run from `backend/`:

```bash
.venv/bin/python scripts/eval_retrieval.py --embedding env
.venv/bin/python scripts/eval_retrieval.py --embedding env --backend postgres \
  --dsn postgresql://USER:PASSWORD@127.0.0.1:PORT/DBNAME
```

## Observed results

hit@3 of the served ranking over the 108 answerable cases, next to the local-vector figures from the [earlier record](retrieval-ca-factory.md#observed-results):

| Type         |   Cases | Local vectors, SQLite | Qwen3, SQLite | Local vectors, PostgreSQL | Qwen3, PostgreSQL |
| ------------ | ------: | --------------------: | ------------: | ------------------------: | ----------------: |
| `direct`     |      35 |                    33 |            34 |                        31 |                33 |
| `paraphrase` |      30 |                    22 |            28 |                        22 |                28 |
| `code`       |      11 |                    11 |            11 |                        11 |                11 |
| `cross`      |      22 |                     7 |            21 |                         7 |                21 |
| `multi`      |       6 |                     3 |             2 |                         3 |                 4 |
| `version`    |       4 |                     4 |             4 |                         3 |                 4 |
| **All**      | **108** |                **80** |       **100** |                    **77** |           **101** |

Each leg on its own with the remote model:

| Strategy     | hit@3, SQLite | MRR, SQLite | hit@3, PostgreSQL | MRR, PostgreSQL |
| ------------ | ------------: | ----------: | ----------------: | --------------: |
| Dense        |           100 |       0.839 |               100 |           0.834 |
| Lexical      |            80 |       0.673 |                77 |           0.659 |
| RRF (served) |           100 |       0.855 |               101 |           0.844 |

Other observations from the same runs:

| Observation                                                            | SQLite | PostgreSQL |
| ---------------------------------------------------------------------- | -----: | ---------: |
| Served ranking hit the document but not the chunk holding the answer   |      0 |          0 |
| Superseded document ahead of the current one, of 6 cases that have one |      4 |          4 |

Highest cosine among the three served results, which is what the score-threshold fallback in `ToolManager._evidence_ok` looks at:

| Group                                       | SQLite                                             | PostgreSQL                                  |
| ------------------------------------------- | -------------------------------------------------- | ------------------------------------------- |
| Expected document is in the top three       | 100 cases, 0.197 to 0.872, median 0.559            | 101 cases, 0.197 to 0.872, median 0.558     |
| It is not (no answer, or not retrieved)     | 22 cases, 0.229 to 0.702, median 0.443             | 21 cases, 0.229 to 0.702, median 0.451      |
| AUC between the two groups                  | 0.703                                              | 0.706                                       |
| At the default `RAG_EVIDENCE_MIN_SCORE=0.5` | 65 of 100 judged sufficient, 13 of 22 insufficient | 66 of 101 sufficient, 12 of 21 insufficient |

## Reading the results

- **English questions over Chinese documents go from 7 to 21 of 22.** Lexical retrieval can only match the English tokens a document happens to contain; the embedding model matches across languages.
- **Paraphrases go from 22 to 28 of 30**, the other case where the question shares few literal tokens with the document.
- **With a real embedding model, RRF is the better fusion.** The dense leg alone reaches 100 of 108; fusing it with the lexical leg keeps that total (100 and 101) and raises MRR. This is the opposite of the local-vector result, where both legs measured character overlap and RRF fell below lexical retrieval alone.
- **The score now separates the two groups somewhat (AUC about 0.70, against 0.52 to 0.56 with local vectors), but not well enough to change the default threshold.** At 0.5 a third of the answerable searches are still judged insufficient and trigger the corrective retrieval round.
- **Superseded documents still come first in four of six cases.** Nothing in retrieval prefers the current version; a better embedding does not change that.

## Limits

- The 46 documents and all questions were written by the same author for testing. Real customer phrasing and real policy documents will behave differently.
- One run per backend, against a hosted endpoint. The embedding service may change the model behind the same name.
- This measures recall and rank only. The full chat path rewrites the question into sub-queries and reranks with a generative model; that path was not run.
