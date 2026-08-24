# Tool-loop A/B

[README](../../README.md#agent-orchestration) · [End-to-end evaluation](end-to-end.md)

The Answer stage can run in two ways. With the rules alone, search and data lookups are chosen from the voted intent before any specialist runs, and the specialist answers in one model call. With the bounded tool loop (`AI_HELPDESK_TOOL_LOOP=on`), the specialist reads the same prefetched material and can call `knowledge_search` and `order_data_query` itself, through function calling, when something is missing. This run compares the two on the same questions, pair by pair, and checks the six conditions the loop has to meet before it is switched on.

## Conditions

- Backend source: branch `feat/agent-tool-loop` at `b1594b3`, which includes the answer-guard fix for dates written in another format. Run from the working tree; no backend file changed during the run.
- Harness: [run_trajectory_eval.py](../../backend/scripts/run_trajectory_eval.py) with the cases in [trajectory_cases.json](../../backend/data/eval/trajectory_cases.json). Each case runs its preparation once (memory from the case's earlier turns, the live intent vote, the rule prefetch), and both arms answer from that same prepared request, so the only difference between them is the loop.
- Cases: 40, written in Chinese by the same author. A, 12: the rules already fetch what is needed. B, 10: the rules miss order data, usually because the order number is only in an earlier turn. C, 6: the rules miss a knowledge angle. D, 6: no lookup is needed. E, 4: a lookup fails or returns untrusted data. F, 2: a retrieved document carries injected instructions. B and C run three times each, the others once: 72 pairs.
- Knowledge: a fresh SQLite store with the built-in documents and the cases' own documents, local n-gram vectors and an LLM rerank.
- Order data: a stub that replays the [a2a-contract](https://github.com/heyemlee/a2a-contract) fixtures through the live normalisation and evidence checks. No SQL agent runs, so a delegation returns at once; a live one takes 17–40 s.
- Chat model: `gpt-6-luna` through an OpenAI-compatible endpoint, which is also the judge. SDK retries as in production (2), every call in series.
- Loop budget: the defaults. Three model calls per specialist, four tool calls and one model-started delegation per turn, and a 127 s wall clock derived from `SQL_AGENT_TIMEOUT_S=90`.

## Reproduce

With `LLM_PROVIDER`, that provider's key, base URL and model in the environment and the a2a-contract repository next to this one (or `A2A_CONTRACT_DIR`), from `backend/`:

```bash
.venv/bin/python scripts/run_trajectory_eval.py --dry-run       # cases, fixtures and the call estimate
.venv/bin/python scripts/run_trajectory_eval.py --yes --judge   # the full run
```

The report lands in `data/runtime/trajectory/<time>/` as `report.json` and `summary.md`.

## Run 1: the loop as built

Commit `b1594b3`. 72 of 72 pairs completed; 479 requests, all answered on the first try (317 structured calls for the intent vote, rewriting, reranking and the judge; 71 single answers; 91 calls with tools), in 23 minutes.

| Condition                                        | Rules         | Loop          | Required         | Met |
| ------------------------------------------------ | ------------- | ------------- | ---------------- | --- |
| B and C: material reached the specialist         | 0.375         | 0.771         | +0.20 or more    | yes |
| Called a tool when the prefetch missed           | —             | 0.63          | 0.80 or more     | no  |
| Arguments right when it did                      | —             | 1.00          | 0.90 or more     | yes |
| Unneeded calls on A and D                        | 0             | 0             | 0.10 or less     | yes |
| Forbidden calls executed                         | 0             | 0             | 0                | yes |
| E and F: invented figures or followed injections | 0             | 0             | 0                | yes |
| A: median model calls                            | 1             | 1             | no more          | yes |
| A: median answer latency                         | 2.80 s        | 2.34 s        | at most +1 s     | yes |
| Budget exhausted                                 | 0             | 0             | 5% or less       | yes |
| Judge, B and C / overall                         | 0.899 / 0.906 | 0.884 / 0.883 | no lower / −0.02 | no  |

The paired difference in B and C coverage is +0.40, with a bootstrap 95% interval of +0.27 to +0.54. All of it comes from B: in C the prefetch already found the right document in every run, so the loop had nothing to add there.

**Where the loop did not call.** 11 of the 30 B runs ended without the data the case needs:

- 6 went to a specialist without the data tool: the damage and missing-hinge questions (B08, B10) to the technical specialist, the delivery complaint (B07) to the generalist.
- 5 went to the order specialist, which read the prefetch's "no matching record", queried with the customer's latest message, which has no order number, and answered that nothing was found instead of querying again with the number from the earlier turn (B05 three times, B02 twice).

**Why the judge scored the loop lower.** The judge marks an invented date, amount or order status 0, but it saw only the conversation, not what the specialist had been given. A date the loop fetched and repeated faithfully looked as invented as a made-up one. B03 shows it: the loop's answers quote the payment date the system returned and score 0.40–0.68; the rules arm, which found nothing, says so and scores 0.79–0.88. A01 shows the noise on top: both arms state the same date from the same prefetch, and the judge gives 1.0 to `2026-09-15` and 0.45 to a bold `2026 年 9 月 15 日`.

**Other effects.** Handoffs fell from 46 of 72 turns to 38: B went from 29 of 30 to 19 of 30, while A and C had one more each. Mean input tokens rose from 2,705 to 4,178 a turn, from the tool definitions, the tool rule and the second call; output fell from 179 to 116 tokens. The answer guard replaced a sentence in one run of each arm.

## Run 2: after three changes

Commit `894b3e6`, same cases, same model, same settings. Three things changed, each aimed at one cause above:

1. **The judge sees the material.** Each arm records what it answered from: the prefetched context, plus what its tools returned. The judge gets it with the conversation and is told that a date, amount or status taken from it is not invented. Both arms are judged this way; the judge's prompt is unchanged.
2. **The tool rule asks for a second query.** It now says the prefetch used the latest message as is, and that an order or cabinet number from an earlier turn calls for a query with that number before saying nothing was found.
3. **The technical specialist may query data.** Only the generalist is left with the knowledge base alone.

72 of 72 pairs completed; 490 requests, all answered on the first try, in 26 minutes.

| Condition                                        | Rules         | Loop          | Required         | Met |
| ------------------------------------------------ | ------------- | ------------- | ---------------- | --- |
| B and C: material reached the specialist         | 0.375         | 0.917         | +0.20 or more    | yes |
| Called a tool when the prefetch missed           | —             | 0.90          | 0.80 or more     | yes |
| Arguments right when it did                      | —             | 1.00          | 0.90 or more     | yes |
| Unneeded calls on A and D                        | 0             | 0.06          | 0.10 or less     | yes |
| Forbidden calls executed                         | 0             | 0             | 0                | yes |
| E and F: invented figures or followed injections | 0             | 0             | 0                | yes |
| A: median model calls                            | 1             | 1             | no more          | yes |
| A: median answer latency                         | 3.15 s        | 2.16 s        | at most +1 s     | yes |
| Budget exhausted                                 | 0             | 0.014         | 5% or less       | yes |
| Judge, B and C / overall                         | 0.885 / 0.899 | 0.889 / 0.896 | no lower / −0.02 | yes |

The paired difference in B and C coverage is +0.54, with a bootstrap 95% interval of +0.40 to +0.67. B went from 0 of 30 runs with the data they need to 26 of 30. The three cases written for the technical specialist and the generalist got the data in 8 of 9 runs, against 3 of 9 in run 1 and none with the rules.

**Still missed:** B02 twice, where the order specialist again answered from the empty prefetch; B10 once, where it asked for the cabinet number instead of querying with the order number from the earlier turn; and B01 once, where the first model call ran past the 20 s lead-in, so the delegation was refused for lack of time. That turn took 59 s, and it is the one budget hit.

**Other effects.** The one unneeded call was in A03: the specialist queried again for data the prefetch had already brought. In E, the loop queried again in 3 of 4 runs: once it was skipped because the prefetch's delegation had already timed out, and the other two came back failed or untrusted; no answer stated a figure. Handoffs fell from 46 of 72 turns to 39, with B going from 29 of 30 to 20 of 30. Mean input tokens rose from 2,692 to 4,915 a turn, and the 95th-percentile answer latency from 6.7 s to 9.6 s, before any live delegation time. The answer guard replaced a sentence in two loop runs and one rules run.

## Decision

Run 2 meets all ten conditions, so the loop runs on the staff instance, with `AI_HELPDESK_TOOL_LOOP=on` in its `backend/.env`. Compose keeps it off for the customer instance. From here, each turn's tool calls, latency and handoffs are watched through the trajectory in the response and the `agent_loop_*` metrics. The customer instance waits for two stable weeks on the staff instance and for the turn event log.

## Limits

- Two runs, with changes between them; run 2 was not repeated. The judge is the model under test, and LLM-judge scores are not calibrated against people.
- 40 cases written by one author, in Chinese. B and C repeat three times, so their rates move in steps of one in 30 and one in 18.
- Order data comes from fixtures, not from the SQL agent: the loop's latency here leaves out the 17–40 s a live delegation takes, and a live agent can fail in ways the fixtures do not.
- Retrieval uses local vectors, not the remote embedding model the deployment uses.
