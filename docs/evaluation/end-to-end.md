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

## The model in the report

Reports now record the chat model that answered and judged (`model`, and `baseline.model` in the `/eval/run` response). When the baseline was produced by a different model, or before the field existed, every regression line and the first recommendation name both: a score change across models is not a code regression. The 2026-09-01 baseline predated the field. It was replaced on 2026-11-06 by a run of `/eval/run` on the staff instance (release `5894aeb`, `gpt-6-luna` answering and judging): 16 of 17 items passed, intent accuracy 87.8% (Macro-F1 0.859), mean judge scores relevance 0.936, accuracy 0.971, completeness 0.888, helpfulness 0.883. The one failure is `dialog_8`, the explicit request for a person, which handed off as designed and was scored 0.625, as in the run above. 12 of the 16 dialog turns handed off, against 3 in the 2026-09-01 baseline. `dialog_8` went to a person by rule; in the other 11 the specialist declared `[[handoff:yes]]` while the keyword check said no, and the declaration wins, which drops self-assessed confidence to 0.65, under the 0.70 gate. That is this model's behaviour, noted in the run above, not the keyword rule misfiring. The judge scores do not include handoff rate, so the regression check now tracks it separately (next section).

## Handoff rate

The judge passes an answer that stays within policy and hands the customer to a person, so a rise in handoffs leaves the judge scores where they were. Reports therefore carry the handoff rate on its own, outside `avg_scores`: `handoffs` turns out of `handoff_turns` dialog turns, and `handoff_rate` (in the `/eval/run`, `/eval/baseline` and `/eval/promote` responses as well). Every handoff counts, the rule handoff for an explicit request as much as a specialist's `[[handoff:yes]]`. A turn whose model call failed is left out of both numbers: nobody decided anything about it. With no dialog turns the rate is `null`.

The regression check compares the rate with the baseline in the other direction from the scores: a rise of more than 10 percentage points is a regression, for example `handoff_rate: 75.0% (12/16) → 87.5% (14/16)`. The tolerance is absolute rather than relative so that a baseline of 0 still compares; with 16 turns it lets one extra handoff through and reports two. A fall is not reported. When the baseline came from another model, the line names both models, as the score lines do.

Baselines saved before these fields existed get their rate from their own results, which record `handoff` per turn: the 2026-11-06 baseline reads as 12 of 16 (75.0%). A baseline whose results do not record it either has no rate, and the check skips that comparison instead of reporting one.

## Limits

- One run. The judge is the model under test, and LLM-judge scores are not calibrated against people.
- 90 intent messages and 16 turns, all in Chinese and written by the same author.
- No live order data and only six knowledge documents: this measures routing, policy and answer quality, not retrieval depth or the A2A path. Retrieval has its own records, and ai-sql-agent measures its side with [its own benchmark](../../ai-sql-agent/benchmark/README.md).
