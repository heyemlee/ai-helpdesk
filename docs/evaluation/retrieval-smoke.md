# Retrieval smoke measurement

[English README](../../README.md#evaluation-and-current-limits) · [中文 README](../../README.zh-CN.md#评测与当前边界)

This is a small, deterministic comparison of recall strategies. It does not run answer generation, query rewriting, or reranking.

## Conditions

- Backend source: commit `d7fdb8c0be47354cfeeb0526784835b398e3adac` (backend files unchanged at measurement).
- Harness: [eval_retrieval.py](../../backend/scripts/eval_retrieval.py); eight bundled Chinese queries and their expected document titles.
- Corpus: six chunks seeded from `knowledge_base._DEFAULT_DOCS`; fresh temporary SQLite storage.
- Dense: local character n-gram hash vectors, 256 dimensions, cosine similarity.
- Lexical: the SQLite fallback's pure-Python BM25 over the project's tokenization.
- Hybrid: reciprocal rank fusion with `k=60`, deduplicated by chunk ID.
- No remote embedding or generative model calls; retrieval environment variables explicitly override the backend `.env`.

## Reproduce

After installing `backend/requirements.txt` into `backend/.venv`, run from the repository root:

```bash
EMBEDDING_BASE_URL='' EMBEDDING_API_KEY='' EMBEDDING_DIM=256 \
  backend/.venv/bin/python backend/scripts/eval_retrieval.py
```

## Observed ranks

| Query | Expected document | Dense | Lexical | Hybrid |
| --- | --- | ---: | ---: | ---: |
| 补件需要提供什么材料 | 缺件漏发与补件流程 | 4 | 1 | 2 |
| 柜门关不严怎么办 | 安装与常见现场问题 | 3 | 1 | 1 |
| 标准交期是几周 | 交期说明 | 1 | 1 | 1 |
| 五金件质保多久 | 保修范围与除外情形 | 4 | 1 | 2 |
| 已经排产了还能改尺寸吗 | 报价构成与变更加价 | 2 | 4 | 2 |
| 量尺需要什么条件 | 定制流程与节点 | 1 | 1 | 1 |
| 台面拼缝多宽算正常 | 安装与常见现场问题 | 1 | 2 | 1 |
| 定金交了多久开始排产 | 定制流程与节点 | 3 | 1 | 1 |

| Strategy | MRR | hit@3 |
| --- | ---: | ---: |
| Dense | 0.583 | 6/8 |
| Lexical | 0.844 | 7/8 |
| Hybrid | 0.812 | 8/8 |

MRR averages the reciprocal rank of the expected document. hit@3 counts whether that document appears in the first three results, matching the chat path's default three knowledge passages.

Hybrid improves top-three coverage on this set, while lexical retrieval ranks the expected document first more often and has the higher MRR. The corpus and query set are small, Chinese-only, and domain-specific. These results do not establish PostgreSQL search behavior, remote embedding quality, grounded-answer accuracy, or production capacity. A deployment needs evaluation against its own corpus, query distribution, and chosen models.
