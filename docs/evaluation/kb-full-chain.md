# Knowledge answers through the full chat path

[README](../../README.md#retrieval) · [Retrieval with local vectors](retrieval-ca-factory.md) · [Retrieval with a remote embedding model](retrieval-remote-embedding.md) · [End-to-end evaluation](end-to-end.md)

The retrieval records stop at the ranking. This harness sends the same questions through the whole chat path, the way `/chat` answers them: intent vote, query rewriting, reranking, the corrective retrieval round, the specialist's answer and the answer guard. Then it checks the reply the customer would read. It judges only what can be checked mechanically and uses no judge model.

## Why the ranking is not enough

- The answer guard checks that a figure in a commitment appears in the retrieved sources (`AnswerPolicy._is_grounded` in [answer_policy.py](../../backend/app/core/answer_policy.py)). When the superseded delivery-fee document is retrieved, its old prices count as grounded too. With the remote embedding model the superseded document outranks the current one in 4 of 6 cases ([record](retrieval-remote-embedding.md)).
- The [end-to-end evaluation](end-to-end.md) scores dialog turns with a judge but has no reference answers, so whether the figures in a final reply were right had not been measured.

## What it judges

The cases are the 122 in [retrieval_cases.json](../../backend/data/eval/retrieval_cases.json), over the same 52 simulated documents.

| Group | Cases | Judged | How |
| --- | ---: | --- | --- |
| Answer is a figure or a code | 64 | Yes | The case's `answer.facts` lists what the reply must contain. Each item has the spellings that count, the first copied from the document. A case is right when every item is there. |
| Of those, a superseded version exists | 6 | Yes | `answer.stale` lists figures that appear only in the superseded document. A reply that contains one has quoted the old version. |
| No answer in the corpus | 14 | Yes | A reply with a number that appears nowhere in the corpus, the SOPs, the fixed replies or the question is counted as made up. Otherwise a reply that says the information isn't available, or that hands off, counts as declined. Anything else is listed for a person to read. |
| Answer is a statement (yes or no, a procedure) | 44 | No | Nothing to check mechanically, so these are not run. |

Matching ignores spacing and thousands separators, and folds range spellings (`7-10`, `7 到 10`, `7–10 business days`) into one form. Money is matched with its currency (`150美元`, `$150`), single digits with their unit (`3年`, `3 years`), and a number never matches inside a longer one (`150` is not in `1500`, `5:30` is not in `15:30`). The harness checks that every listed value is in the document it claims and that every superseded figure is absent from the current version, and refuses to run otherwise.

Recorded alongside, without counting towards right or wrong:

- hit@3 of the candidates the reranker received, in recall order, and of the three passages that reached the answer. On the direct path, which skips rewriting and reranking, the two are the same list.
- Whether a passage of the superseded document reached the answer.
- Numbers in a reply that appear in none of the sources above.
- Model calls per case, by purpose, counted from the turn's own event log.

## How it runs

[eval_retrieval.py](../../backend/scripts/eval_retrieval.py) `--full-chain` assembles the chain the way `api/main.py` does: the same classes, the model connection resolved the same way (the one saved from the console first, then `.env`), and `knowledge_search` registered with the same options. It calls `prepare_turn` and the orchestrator's `run`, the two functions `/chat` calls. It differs from a production turn in these ways, all to keep real data out of it:

- The knowledge base is a temporary store loaded with the corpus. Production's store, tickets, memory and event log are not touched.
- Memory is empty, so every case is a new customer's first message.
- There is no SQL agent delegation and the tool loop is off, as on the customer instance.
- One replica per agent type, and cases run one at a time.

When the model credentials are rejected the run stops; the cases after that are not attempted, and the report says how many ran.

## Reproduce

From `backend/`, with the model connection the service uses:

```bash
.venv/bin/python scripts/eval_retrieval.py --full-chain --embedding env --json /tmp/chain.json
.venv/bin/python scripts/eval_retrieval.py --full-chain --embedding env --only v-01,n-06
```

This calls the chat model. A case on the rewrite-and-rerank path takes four calls (intent, rewrite, rerank, answer) and two more when the reranker asks for the corrective round; a case the rules answer directly takes one. The 78 judged cases come to roughly 300–400 calls. `--embedding env` also embeds the corpus and every query with the remote embedding model; `--embedding local` keeps the dense leg offline but measures a different ranking from production's.

## Offline check

The tests in [test_eval_retrieval.py](../../backend/tests/test_eval_retrieval.py) run the mode against a fake model on the SDK transport. Its reranker puts the superseded document first and its answers copy the top passage, so the delivery-fee case comes back with the 2025 price, and the harness reports the missing current figure and the quoted old one, with the current document still in the top three before and after reranking. A second test rejects the credentials and checks that the run stops after the first case.

All 78 judged cases also ran once against the same kind of fake model, with no request leaving the process: 293 model calls, four per case on the rewrite-and-rerank path and one on the direct path. That run checks the wiring and the call count; its answers say nothing about answer quality.

## Limits

- The 52 documents and all questions were written by the same author. Real customer phrasing and policies will behave differently.
- A right figure in the reply does not make the reply right, and a missing one can be a fair answer: a specialist may decline to quote, or hand off. The handoff column separates those.
- "Declined" is read from wording and handoffs. A reply that states something the corpus does not support, without a number, lands in the list for a person, not in "made up".
- The 44 statement cases are not covered; only a judge or a person can score them.
