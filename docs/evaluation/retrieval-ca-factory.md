# Retrieval measurement on a simulated factory knowledge base

[English README](../../README.md#evaluation-and-current-limits) · [中文 README](../../README.zh-CN.md#评测与当前边界) · [Earlier 8-query smoke record](retrieval-smoke.md)

A deterministic comparison of recall strategies on a corpus large enough that each retrieval leg can no longer return everything. It does not run answer generation, query rewriting, or reranking.

This measurement changed how the service fuses the two legs when no embedding model is configured. The reciprocal rank fusion figures below are what the service did before; the "served" figures are what it does now.

## Conditions

- Backend source: the change set that adds this record, on top of commit `dde94bf`.
- Harness: [eval_retrieval.py](../../backend/scripts/eval_retrieval.py), default `--suite ca`.
- Corpus: 52 documents, 56 chunks. Six built-in documents from `knowledge_base._DEFAULT_DOCS` plus 46 in [sample_ca_factory_knowledge.json](../../backend/data/demo_docs/sample_ca_factory_knowledge.json). The 46 are **simulated** policies for a California cabinet factory, written for testing. Prices, fees, and time limits in them are invented.
- Cases: 122 in [retrieval_cases.json](../../backend/data/eval/retrieval_cases.json); 108 have an expected document and 14 have no answer in the corpus.

  | Type | Cases | What it probes |
  | --- | ---: | --- |
  | `direct` | 35 | Chinese question sharing key terms with the document |
  | `paraphrase` | 30 | Colloquial Chinese with little literal overlap |
  | `code` | 11 | Model codes and proper names (`W3036`, `CDTFA-230`, `Net 30`) |
  | `cross` | 22 | English question over a Chinese document |
  | `multi` | 6 | Two documents must both reach the top three |
  | `version` | 4 | A current document competes with a superseded one |
  | `unanswerable` | 14 | Nothing in the corpus answers it |

- Dense: local character n-gram hash vectors, 256 dimensions, cosine similarity. No remote embedding calls.
- Lexical: the project's tokenization (CJK bigrams, ASCII runs), scored by pure-Python BM25 on SQLite and by `ts_rank_cd` over a `simple` `tsvector` on PostgreSQL.
- RRF: reciprocal rank fusion of the two legs with `k=60`, ten candidates per leg, deduplicated by title and content.
- Served: the ranking `KnowledgeBase.search_async` returns. With local vectors that is lexical results in their own order, followed by passages only the dense leg returned. With a real embedding model it is RRF. The harness compares this column with `search_async` on every query and reports any difference; none was reported.
- SQLite: fresh temporary file. PostgreSQL: 16.15 from `pgvector/pgvector:pg16` in a throwaway container, tables created in a temporary schema and dropped afterwards.
- A case counts as a hit when an expected document is in the first three results, matching the three passages the chat path passes to the agent. `multi` cases need both documents there.

## Reproduce

After installing `backend/requirements.txt` into `backend/.venv`, run from the repository root:

```bash
backend/.venv/bin/python backend/scripts/eval_retrieval.py
```

```bash
backend/.venv/bin/python backend/scripts/eval_retrieval.py --backend postgres \
  --dsn postgresql://USER:PASSWORD@127.0.0.1:PORT/DBNAME
```

`--embedding local` is the default and ignores `EMBEDDING_*` in `backend/.env`. `--verbose` lists every case; `--json PATH` writes the per-case ranks.

## Observed results

hit@3 over the 108 answerable cases. Dense results are identical on both backends because both compute exact cosine over the same vectors.

| Type | Cases | Dense | Lexical, SQLite | RRF, SQLite | Served, SQLite | Lexical, PostgreSQL | RRF, PostgreSQL | Served, PostgreSQL |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `direct` | 35 | 10 | 33 | 28 | 33 | 31 | 28 | 31 |
| `paraphrase` | 30 | 2 | 22 | 14 | 22 | 22 | 16 | 22 |
| `code` | 11 | 8 | 11 | 10 | 11 | 11 | 10 | 11 |
| `cross` | 22 | 2 | 7 | 7 | 7 | 7 | 7 | 7 |
| `multi` | 6 | 0 | 3 | 1 | 3 | 3 | 1 | 3 |
| `version` | 4 | 2 | 4 | 2 | 4 | 3 | 2 | 3 |
| **All** | **108** | **24** | **80** | **62** | **80** | **77** | **64** | **77** |

| Strategy | MRR, SQLite | MRR, PostgreSQL |
| --- | ---: | ---: |
| Dense | 0.212 | 0.212 |
| Lexical | 0.673 | 0.659 |
| RRF | 0.475 | 0.489 |
| Served | 0.676 | 0.662 |

Other observations from the same runs:

| Observation | SQLite | PostgreSQL |
| --- | ---: | ---: |
| Lexical had the document in the top three and RRF lost it | 22 | 17 |
| RRF had it and lexical did not | 4 | 4 |
| Neither leg returned it within ten candidates | 20 | 18 |
| Served ranking hit the document but not the chunk holding the answer | 0 | 0 |
| Superseded document ahead of the current one in the served ranking, of 6 cases that have one | 3 | 2 |

Highest cosine among the three served results, which is what the score-threshold fallback in `ToolManager._evidence_ok` looks at:

| Group | SQLite | PostgreSQL |
| --- | --- | --- |
| Expected document is in the top three | 80 cases, 0.014 to 0.295, median 0.140 | 77 cases, -0.015 to 0.295, median 0.127 |
| It is not (no answer, or not retrieved) | 42 cases, -0.018 to 0.248, median 0.133 | 45 cases, -0.018 to 0.275, median 0.134 |
| AUC between the two groups | 0.564 | 0.518 |

## Reading the results

- **With local n-gram vectors, RRF is worse than lexical retrieval alone at this size.** RRF ranks a candidate that appears in both lists above one that tops a single list, on the premise that the two lists are independent judgements. The n-gram vectors measure character overlap, as the lexical leg does, only more noisily. Passages that overlap loosely with the question land in both lists and push the lexical first place down. The six-chunk smoke set could not show this: each leg returned the whole corpus.
- **The service therefore no longer lets local vectors vote.** With local vectors it serves lexical results first and uses dense results only to fill the remaining places, which matters when a question shares no literal token with any document. This matches lexical retrieval's top-three hits on every case here and gives up the four cases RRF rescued. RRF remains the fusion whenever a real embedding model is configured.
- **English questions over Chinese documents mostly miss.** The seven hits are the cases where the document contains an English token from the question (`will-call`, `CARB`, `Frameless`, `Rush Order`, `Trade Account`, `W3036`, `ADU`). For the other fifteen the lexical leg returned nothing from the expected document.
- **Paraphrases are the lexical leg's weak spot**: 22 of 30, against 33 of 35 on SQLite for direct questions.
- **PostgreSQL ranks slightly worse than the SQLite fallback on the lexical leg** (77 against 80). `ts_rank_cd` does not weigh how rare a term is in the corpus; BM25 does.
- **Nothing in retrieval prefers a current document over a superseded one.** Both versions describe the same thing with different figures, and either can come first.
- **The n-gram cosine does not indicate whether the evidence is sufficient.** The two groups overlap almost completely. No observed score reaches the default `RAG_EVIDENCE_MIN_SCORE=0.5`, so wherever the threshold is consulted (cross-encoder reranking, or three or fewer candidates) every search is judged insufficient and triggers the corrective retrieval round.

## Limits

- The 46 documents and all questions were written by the same author for this measurement. Real customer phrasing and real policy documents will behave differently.
- The fusion rule for local vectors was chosen after looking at these results, so the served figures are not an independent test of it.
- This measures recall and rank only. The full chat path rewrites the question into sub-queries and reranks with a generative model, which can recover documents that sit at ranks four to ten here; that path was not run.
- No remote embedding model was measured. After configuring one, rerun with `--embedding env` and set `RAG_EVIDENCE_MIN_SCORE` from the score distributions the harness prints.
- One deterministic run per backend.
