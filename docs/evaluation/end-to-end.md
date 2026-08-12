# End-to-end evaluation

[README](../../README.md#agent-orchestration) · [Retrieval with a remote embedding model](retrieval-remote-embedding.md)

One run of the built-in evaluation against the models the deployment uses: the router's intent vote on labelled messages, and full turns through the orchestrator scored by an LLM judge. An upstream connection error cut one dialog turn short; the results below keep what completed apart from what did not.

## Conditions

- Backend source: commit `40e9547`, exported with `git archive` into an empty directory, so no local `.env` was read.
- Harness: [run_eval.py](../../backend/scripts/run_eval.py), which builds the same objects as `POST /eval/run` (intent recognizer, orchestrator, answer policy, SOPs) and runs them in process. Knowledge comes from a fresh SQLite store seeded with the six built-in documents. There is no Redis, PostgreSQL or SQL agent, so order questions get no live data. One replica per agent type, four cases at a time.
- Chat model: `gpt-6-luna` through an OpenAI-compatible endpoint. The same model is the judge.
- Embeddings: `Qwen/Qwen3-Embedding-0.6B`, 1024 dimensions, with the query prefix.
- Cases, in [evaluator.py](../../backend/app/evaluation/evaluator.py): 90 labelled intent messages, and 12 dialog cases with 16 turns between them. All are in Chinese.
- A dialog turn passes when the judge's overall score, the mean of relevance, accuracy, completeness and helpfulness, is at least 0.75.
- The report was compared with the promoted baseline in `backend/data/eval/baseline.json` and not promoted.

## Reproduce

With `LLM_PROVIDER`, that provider's key, base URL and model, and the `EMBEDDING_*` settings in the environment, from `backend/`:

```bash
.venv/bin/python scripts/run_eval.py
```

## Results

### Intent vote: complete

| Metric   | Value           |
| -------- | --------------- |
| Accuracy | 77 / 90 (85.6%) |
| Macro-F1 | 0.798           |

Every one of the 90 calls returned a classification with its reasoning. The intent cases finish before any dialog case starts, so the connection error later in the run did not reach them. The promoted baseline records 81.1% and 0.775, but not the model that produced them.

### Dialog turns: 15 of 16 completed

| Outcome       | Turns | Detail                                                                                                                                                                      |
| ------------- | ----: | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Passed        |    14 | Overall scores from 0.787 to 1.000                                                                                                                                          |
| Failed        |     1 | `dialog_8`, an explicit request for a person: it went straight to handoff and opened a ticket, as designed, and the judge scored the fixed handoff reply 0.675              |
| Not completed |     1 | `dialog_11_turn_1`: its intent call and its answer both failed with a connection error, so the service returned its "not answered" reply, and the judge call failed as well |

Mean judge scores over the 15 completed turns: relevance 0.883, accuracy 0.970, completeness 0.817, helpfulness 0.797. The harness's own averages include the failed judge call's default of 0.5 and are not used here.

## Other observations

- **The specialists hand off often with this model.** In 11 of the 14 answered turns that did not ask for a person, the specialist declared that the case needs one; its self-assessed confidence dropped to 0.65, under the 0.70 gate, and a ticket was opened. The judge still passed those turns because the answers stayed within policy, but in production they would all land in the ticket queue.
- **The answer guard fired once,** on the case written to trigger it (`dialog_7`, delivery this Friday): the date promise was removed.
- **Routing is not scored.** The dialog cases carry no expected agent. Read against the domain grouping in the source, 12 of the 14 turns that went to an agent reached the expected one; the countertop-material question (`dialog_4`) and the cost question that closes the colour-change case (`dialog_11_turn_2`) went to the generalist.

## Limits

- One run. The judge is the model under test, and LLM-judge scores are not calibrated against people.
- 90 intent messages and 16 turns, all in Chinese and written by the same author.
- No live order data and only six knowledge documents: this measures routing, policy and answer quality, not retrieval depth or the A2A path. Retrieval has its own records, and ai-sql-agent measures its side in its [own repository](https://github.com/heyemlee/ai-sql-agent).
