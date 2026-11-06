# benchmark

150 execution-accuracy cases, a 40-case holdout set, and a release gate that
enforces the thresholds a tool has to clear to be usable in production.

[English](README.md) | [简体中文](README.zh-CN.md)

## Four headline metrics and their thresholds

The question production has to answer is "can the business trust this tool",
not "does the score look good". Each of the four headline metrics maps to a
failure mode that would get the tool abandoned, and each is paired with a
**guardrail** that closes off the cheap way to inflate it. Thresholds live in
`thresholds.json`, which `gate.py` reads directly.

| # | Metric | Threshold | Measured | Guardrail | What failing it costs |
|---|---|---|---|---|---|
| 1 | Column recall (**holdout set**, 40 cases) | **≥ 85%** | **99.2% [97.5, 100]** ✅ | Average share of columns in the graph ≤ 40% (36.8% ✅) | If a column never reaches the schema graph, no amount of generation accuracy can answer the question |
| 1' | Column recall (evaluation set, in-prompt) | ≥ 95% | **100%** ✅ | same | A regression floor, not evidence of passing |
| 2 | Single-database execution accuracy | **≥ 85%** | **100%** ✅ | — | Every result needs manual review, and the review eats the time the question saved |
| 3 | Cross-database execution accuracy | **≥ 90%** | **94.7% [92.0, 96.0]** ✅ | — | Users fall back to querying each database themselves and stitching the results, which makes the capability pointless |
| 4 | Silent-error rate | **≤ 3%** | **0.9% [0.7, 1.3]** ✅ | Risk-warning false-alarm rate ≤ 20% (2.2% ✅) | A wrong number gets copied into a weekly report as fact; once is enough to get the tool switched off |
| — | Retrieval vs in-prompt recall gap | **≤ 3pp** | **0pp** ✅ | — | Retrieval found it, the model never saw it — widening recall here only makes things worse |

> **The numbers and their sample sizes have to be read together:**
>
> | Number | Source | Sample |
> |---|---|---|
> | 100% single-database, 94.7% cross-database, 0.9% silent, 2.2% false alarm, 99.1% overall | [2026-10-16 gate rounds](baseline/2026-10-16/README.md) on commit 5897af9, `gpt-6.1-sol`, remote embeddings, built-in demo metadata | All 150 cases, **three complete rounds**, mean with min–max interval |
> | 99.2% holdout recall, 100% evaluation-set recall, 0pp gap, 36.8% column share | The same three rounds | 150 + 40 cases, three rounds |
> | Earlier, with `claude-sonnet-5`: 93.3% single, 85.3% cross, 5.3% silent, 5.6% false alarm; 43.6% column share | [2026-09-09 measurement](baseline/2026-09-09/README.md) and [2026-09-08 baseline](baseline/2026-09-08/README.md) | 150 (+ 40) cases, three runs |
>
> Two of the 2026-10-16 rounds come from a `--repeat 3` run whose third round the
> model service cut off after question 50; the third is a separate `--repeat 1`
> round. Every line is met in each round and on their mean, but they are not one
> `--repeat 3` verdict.
>
> ⚠️ **Two caveats before quoting any of this.** The runs use the built-in demo
> metadata, not the deployment semantic layer under `tools/sim_erp/semantic`, so
> they measure the pipeline on the standard fixture rather than the current
> deployment. And the column-share line depends on the remote embedding: with
> local vectors the column budget widens, and three rounds the same day sent
> 43.4–44.2% of the columns, over the line. Both are spelled out in the
> measurement record.

### The citable form: mean and range over `--repeat 3`

**A single run of an execution-accuracy metric does not count.** The same code
over the same 25 cross-database cases has been measured anywhere between 16% and
36%; one case is 4pp. The rules:

- **Any number quoted externally** (docs, reports) must be the mean over
  `--repeat 3`, with the range attached;
- **To decide whether a change helped**, the difference has to exceed the spread
  of the range;
- **The release gate** runs `python -m benchmark.gate --repeat 3`, and warns when
  run in single-round mode. The gate prints the range alongside each metric — a
  mean above the line with the low end of the range below it means the run got
  lucky, not that the metric passes;
- **Numbers from a single run** must be labelled as such and never quoted as a
  stable level.

### How each threshold was set

**Column recall ≥ 85% (holdout set)**
Recall is a hard prerequisite: miss one gold column and everything downstream is
wrong, and usually wrong in a specific way — the model substitutes a
semantically adjacent column, which is the main source of silent errors.

The gate uses the holdout set rather than the evaluation set because the phrase
tables, column aliases and enum vocabularies were all built against the
evaluation set. There is no way to say how much of that 100% was tuned in. The
holdout set uses entirely different colloquial phrasings and takes no part in
tuning, which makes it the estimate for "what happens when real users show up".

85% means "at most one miss in six colloquial phrasings"; the rest of the time a
user who rephrases still gets an answer. It is not set at 95% because semantic
gaps like "we're running low on stock" ↔ `safety_stock` are closed by adding
aliases over time. That is operational work driven by business feedback, not
engineering that can be finished in one pass.

**Average share of columns in the graph ≤ 40% (guardrail)**

> ⚠️ This line was re-baselined from 30% to 40% on 2026-08-20. The original came
> from the **offline rule-based keyword extraction** path (24.6%), but the gate
> runs the production path (model extraction plus the business glossary), which
> measures 34–42%.
> **The baseline was defined against the wrong path — this is not a threshold
> loosened because it could not be met.** The same code genuinely produces
> different numbers on the two paths, and gating the production path with an
> offline number gates "is an API key configured", not "has retrieval lost
> focus".

Dumping all 86 columns into the schema graph guarantees 100% recall, but it makes
three-stage retrieval pointless and dilutes the prompt with irrelevant columns.
Irrelevant columns measurably act as distractors: the more columns there are, the
more often the model picks the wrong one. The two numbers must always be reported
together — a recall figure on its own says nothing.

**Single-database execution accuracy ≥ 85%**
Below this line the user has to manually review every result and the tool has no
net benefit. 85% is also consistent with published levels: top models reach
85–90% on Spider, and this project works in a closed domain with hand-maintained
metadata, so it should not come in below the public leaderboard.

**Cross-database execution accuracy ≥ 90%**

The line is set at 90%, meaning **cross-database should not be worse than
single-database**. A user should not get a visibly worse answer merely because
the question spans two systems; otherwise they route around the capability and
it may as well not exist.

An earlier derivation produced 78% on the assumption that cross-database work is
inherently discounted:

- **Working back from user behaviour**: cross-database is harder, but it cannot
  be much harder. Once the gap is obvious, users conclude that "asking across
  databases is worse than querying twice myself", and the capability is dead.
  With the single-database line at 85% and roughly 8pp allowed for multi-step
  compounding, that gives **78%**;
- **Working back from the chain**: a cross-database query has to get four things
  right in a row — recognize that it needs splitting, route each step to the
  right database, emit the correct join key in step one, and inline step one's
  result as a literal in step two. Four steps compounding need about 94%
  per-step reliability to give 0.94⁴ ≈ **78%**.

That derivation treated the four-step compounding loss as an irreducible physical
constraint. The ablations do not support that assumption: once the measurement
defects and the budget trimming were fixed, cross-database went from 56% straight
to 76–80%. The gap was **mostly engineering defects, not chain length**. Given
that, the line should demand parity with single-database rather than reserving a
discount.

Measured at 94.7% [92.0, 96.0] over the three 2026-10-16 rounds (`gpt-6.1-sol`, remote embeddings), above the line. Earlier, with `claude-sonnet-5`, it was 73.3% on 2026-09-08 and 85.3% on 2026-09-09; the JOIN-cardinality annotation closed most of that gap by removing missed de-duplication after one-to-many JOINs.

**Silent-error rate ≤ 3%**
The only metric of the four where lower is better, and the most expensive class
of error. The cost of trust is asymmetric: when a query fails, the user goes and
looks it up; when it says "uncertain, please verify", the user verifies. Only a
result that **ran cleanly, produced a number, carried no warning, and is wrong**
gets copied straight into a report and a decision.

3% is roughly one case in 33. The earlier line of 5% was "the ceiling acceptable
for internal analysis"; tightening to 3% follows from the measurement that
**pushing the silent-error rate down does not necessarily push the false-alarm
rate up**: the static shape checks took silent errors from 8.7% to 6.7% while
false alarms only moved from 1.6% to 4.0%, against a guardrail of 20%. With that
much headroom left, there was no reason to stop at 5%.

There is a real cost, just not in false alarms. The static checks rely on
heuristics such as "does the question contain an aggregation word", and a growing
pile of rules gets hard to maintain and starts catching edge cases it should not.
The actual ceiling is discussed in the next section.

**Risk-warning false-alarm rate ≤ 20% (guardrail)**
The silent-error rate can be driven to zero by flagging every result as
suspicious, and that is its only cheat. Once more than one in five correct
answers carries a warning, users start ignoring warnings, and the warning may as
well not exist.

### How column recall is judged: in-prompt, not retrieved

A gold column counts as recalled only if it appears in the schema context
**after token-budget trimming** (`PipelineResult.prompt_columns`), not in the
retrieval output (`retrieved_columns`).

This distinction is not pedantry; it was learned the hard way. The old budget
step cut the rendered schema text at the character level, and anything over
budget simply vanished. One measured cross-database query retrieved 46 columns,
**10 of which were trimmed away**, and recall still reported 100% — while the
planning model wrote, in that same call:

```
Target: work_order table (column: job_no; the schema does not provide a column
for "current production step" or the full table structure)
```

and then generated `SELECT job_no, NULL AS current_production_process`.
**The metric said every column was recalled and the model said it could not see
them. Neither was lying — the metric was measuring the wrong stage.** Scoring
recall on the retrieval output gets more dangerous the higher it goes, because
what it hides is exactly the real cause of failure: the model never got the
columns.

The report emits both:

| Measure | Columns counted | Purpose |
|---|---|---|
| `column_recall` | In-prompt | **What the gate enforces** — only what the model can use counts |
| `retrieval_recall` | Retrieved | Diagnostic only: a gap between the two means the bottleneck is budget allocation, not retrieval |

The accompanying fix is in `PromptBudgetManager.fit_schema_sections`: the schema
budget is now **allocated per database** instead of truncating the assembled text
from the end. Truncating the whole block amounts to cutting the last database
entirely — and a cross-database query depends on precisely that second database.

### The four checks that catch silent errors

Driving the silent-error rate down is about **increasing detection coverage**,
not about making the model more accurate. The four checks are ordered by cost.
The first two are static — the SQL's structure plus plan text, no model call, no
look at the results — so they cost almost nothing and can run on every single query:

| Check | Cost | What it catches |
|---|---|---|
| `aggregation_unrequested` | Static | The question asked for no aggregation, but the SQL **collapses detail into a single number** |
| `shape_contradicts_plan` | Static | The plan says "several rows", the SQL can structurally only return one (a missing GROUP BY) |
| `empty_result` / `cardinality_deviation` | Reads results | Empty results, or a row count an order of magnitude above the plan's expectation |
| `semantic_mismatch` | 2 model calls | What the SQL actually computes does not match the question (final step and intermediate steps compared separately) |

**Why the static checks matter most**: the semantic self-check is precise (1.7%
false-alarm rate) but expensive and catches little. The static checks are cheap
enough to run everywhere, trading precision for coverage — measured, they took
the silent-error rate from 8.7% to 6.7%.

**`aggregation_unrequested` only reports whole-table collapse, never grouped
aggregation.** Of 10 measured false alarms, 8 were grouped aggregation: the model
grouped by the entity the user mentioned, which is usually what they wanted.
Dropping those raised warning precision from 38% to 71% and lost exactly one true
positive. The trade-off has a clear basis — **once warnings are treated as noise,
the real silent errors slip through with them** — so under-reporting is the safer
side. On the cross_db subset the false-alarm rate reached 21–30% at one point,
crossing the 20% guardrail, which is the empirical case for that risk.

### Ablation switches

Every change that affects accuracy has its own switch, so results can be
attributed after the fact instead of "changing a pile of things and watching the
total":

| Switch | Default | Controls |
|---|---|---|
| `enable_keyword_glossary` | on | Attach the business glossary (colloquial → schema term) during keyword extraction |
| `enable_cross_db_fewshot` | on | Inject cross-database examples into the planning prompt |
| `enable_plan_db_check` | on | Step/database consistency check plus replanning on conflict |
| `enable_intermediate_self_check` | on | Run the semantic self-check on intermediate steps too |
| `enable_cross_db_guard` | on | Regenerate SQL that references a database outside its step |
| `enable_short_path` / `enable_repair` / `enable_validation` | on | Short path / repair loop / soft-anomaly validation |

```bash
# One arm per process: reusing a process across arms accumulates SQLite
# connections and measurably exhausts the handle limit
python -c "
from benchmark.runner import BenchmarkRunner
from ai_sql_agent.pipeline import Text2SQLPipeline
from ai_sql_agent.pipeline.objects import PipelineConfig
r = BenchmarkRunner(pipeline=Text2SQLPipeline(
    PipelineConfig(enable_cross_db_fewshot=False))).run(categories=['cross_db'])
print(f'{r.correct}/{r.total}')"
```

### False alarms: the full-set number hides the distribution

The `false_alarm_rate ≤ 20%` guardrail is computed over the **full set**, where
it measures 2.2% (2026-10-16, three rounds) and passes comfortably. Broken down by category, though, every
one of those false alarms is a cross_db answer: the cross_db subset runs at 12.5 / 13.0 / 16.7% across the
three rounds, and it crossed 20% repeatedly in the earlier ablations — 21.1 / 30.0 / 15.8 / 27.8 / 21.1%
across five arms.

**Cross-database questions trigger risk warnings at a visibly higher density.** A
user who mostly asks cross-database questions sees roughly one in four answers
flagged "needs verification", which feels nothing like the full-set number. The
guardrail passes as measured, but this difference should not be buried by the
average, and it is the empirical reason the static checks cannot be loosened
further.

### How silent errors are judged

A case counts as a silent error only if **all three** hold:

1. the result set is not equivalent to gold (the answer is wrong);
2. the final step executed successfully and did not go through the degradation
   path (the UI presents it as a normal answer);
3. no step's soft-anomaly validation produced a finding (the user received no
   warning at all).

The following do **not** count as silent errors — still wrong, but visibly so:

| Situation | What the user sees |
|---|---|
| Syntax error / unknown column (still failing after repair) | An explicit error |
| Empty result | "0 rows returned, the filter may be wrong" |
| Row count an order of magnitude off the plan | A cardinality warning |
| Semantic self-check disagrees | A semantic-mismatch warning |
| Model unavailable | A degraded answer plus `degrade_reason` |

The denominator is **all cases**, not just the wrong ones. Users care about "out
of 100 questions, how many quietly mislead me"; using wrong cases as the
denominator inflates the ratio as accuracy improves, mixing two directions of
change into one number.

**This metric can only be measured offline.** Production has no gold results, so
there is no way to tell whether an answer is right and no way to compute the rate
directly. What is observable in production are its two proxies:
`ai_sql_result_anomalies_total` (how many warnings were raised) and the
correction rate in user feedback. The metric itself is enforced before release by
the gate; in production the backstop is the design rule that **warnings must be
shown to the user**. That makes `has_risk` / `risk_findings` in the response and
the UI a precondition for this metric to mean anything, not an optional extra.

## A defect in the evaluation set itself: shape ambiguity

The release gate rests on the evaluation set. When the foundation is ambiguous,
the metric measures "did the model guess the author's habits", not "is the answer
right" — and it puts a ceiling on accuracy that no amount of optimization can
pass.

Cross-database tuning was stuck at 52–64% until a case-by-case comparison against
gold showed the problem was not in the system:

| Case | Question | Gold's shape | What the model produced |
|---|---|---|---|
| q101 | **Total purchase amount** for over-budget projects | `GROUP BY job_no` (per project) | A single `SUM` |
| q115 | **Total inbound quantity** for suppliers with on-time rate below 0.8 | A single `SUM` (no grouping) | `GROUP BY supplier_id` |

**The same "<filter> → <entity> → <aggregate>" phrasing, with two gold answers
pointing in opposite directions.** The model got the shape wrong both times, but
the questions gave it nothing to go on — both readings are valid. Cases like this
measure luck, not capability.

The fix is to rewrite the **question** so only one reading survives, **taking gold
as authoritative** (q101 → "total purchase amount per project", q115/q116 →
"combined total inbound quantity"). Rewriting gold to match the model's output
would be gaming the score.

Two rules:

- **The rule is applied to the whole set uniformly**, not only to the cases the
  model got wrong. Three cases matched that phrasing and all three were rewritten
  — they happened to all be failures, but they were selected by phrasing, not by
  correctness;
- **Numbers from before and after disambiguation are not comparable**, and any
  citation has to say which side it comes from.

`tests/test_dataset_quality.py` freezes this as a lint: a new case matching that
phrasing without stating its output shape fails the test outright. That is worth
more than fixing the three cases — it blocks the same mistake from recurring.

## Gold annotation conventions (revised once, now locked by tests)

The second defect in the evaluation set: **gold was not internally consistent**,
so semantically correct answers were scored wrong — and because the SQL ran
without warnings, they counted toward the most expensive metric of all, the
silent-error rate. Correct answers were being recorded as quiet failures.

### Convention 1: gold without aggregation must not return duplicate rows — 2 cases fixed

`q033` ("job numbers of purchase orders with payment status paid") had gold
returning 8 rows containing only 6 distinct job numbers (`J24001` appeared
twice); `q045` ("the workshop a work order belongs to") returned 3 rows, all
"workshop 1". Asking "which X" and getting duplicates is a by-product of join
fan-out and carries no information. **`DISTINCT` was added to gold**; the model
had it already and was being marked wrong for it.

The judgment is **data-driven**: execute each gold statement, and flag any that
returns duplicate rows without aggregation. Not guessed by regex, and not
inferred from what the model answered. A full scan matched 2 cases and both were
fixed.

### Convention 2: "per X" groups by primary key — **not changed**

Five cases in the set are of this kind, and gold consistently groups by `job_no`
/ `supplier_id` / `project_code` / `work_order_id` / `material_code` — which is
**both internally consistent and correct**: grouping by name risks collisions,
grouping by primary key does not. The model switching to `material_name` is a
**genuine error, not an ambiguity**.

**The question must not be simplified just to let it pass.** Simplifying the test
makes the number look better while hiding a real defect.

### On q057: the earlier judgment was wrong, gold is right

Rows in `payment` with `pay_status='partial'` also carry a `paid_amt` (79000,
371000) — money that has actually been paid. "Total amount paid" is therefore
`SUM(paid_amt)` over the whole table; the model's added `WHERE pay_status='paid'`
undercounts by dropping partial payments. Filing this under "gold is debatable"
was a mistake, corrected here.

Both conventions are locked by `tests/test_dataset_quality.py`; a new case
violating either fails the test.

## The dataset

`dataset.json`, generated by `dataset_builder.py`:

- `single_table`: 25 cases, single-table filter and projection
- `multi_join`: 25 cases, multi-table joins within one database
- `aggregation`: 25 cases, grouped aggregation
- `having`: 25 cases, post-aggregation filtering
- `time_range`: 25 cases, time ranges
- `cross_db`: 25 cases, two-step cross-database

Each case holds a Chinese-language query and a gold SQL statement. Gold SQL uses
ATTACH aliases (`erp.xxx` / `pm.xxx` / `mes.xxx` / `wms.xxx`) and runs through
`GoldExecutor` on a connection with all four databases attached.

## Judging criterion

Execution accuracy: the result set from the pipeline's final step is compared
against the gold result set after normalization — row order ignored, column names
ignored, floats kept to 6 decimal places.

## Running

```bash
# Full run: needs OPENAI_API_KEY / ANTHROPIC_API_KEY
# (the mock model only covers the smoke subset)
python -m benchmark.run_benchmark --output runtime_data/benchmark_report.json

# Anything you intend to quote: 3 rounds, take the mean — a single run is not reliable
python -m benchmark.run_benchmark --repeat 3

# The release gate uses 3 rounds too: mean for the verdict, range printed alongside
python -m benchmark.gate --repeat 3

# Offline smoke subset
python -m benchmark.run_benchmark --smoke

# Release gate: non-zero exit if any metric misses its threshold, so it can run in CI
python -m benchmark.gate
```

### When the model stops working mid-run

Before and after **every item**, the benchmark and the gate look at the model-access
faults this process already knows about (key rejected, quota exhausted, model not
permitted — local state only, no request is sent). After every item they also check
whether it got a **degraded answer** (`degraded`). Server-side failures are not recorded
in that state — a relay answering 404 "no account in this group supports the model",
5xx after the retries, a dropped connection — and a degraded answer is not the model's
answer, so scoring it would turn one outage into a run of wrong answers. Either way,
they stop at once:

- the item that hit it is not counted, the remaining items are not run, and **nothing is
  re-run or retried**;
- what was completed is written to disk marked `incomplete: true` (the `--output` path;
  `--partial-output` for the gate; otherwise `runtime_data/*_incomplete_<time>.json`);
- `abort_reason` is `model_access_unavailable` (credential) or `model_unavailable`
  (service). For the latter, `abort_detail` names the item and the time; the model's own
  error is in the `pipeline.model_unavailable` log line at that time;
- the exit code is **3**. 1 means "finished, below the line", 2 means "could not run"
  (no credentials), 3 means "did not finish, no verdict";
- the gate prints neither a pass nor a fail. Numbers from an unfinished round cannot be
  compared with the thresholds or averaged with complete rounds.

### The bilingual business cases (bilingual_cases.json)

The 150 evaluation cases and the 40 holdout cases are all asked in Chinese.
`bilingual_cases.json` covers the other direction: **the same business question asked
once in Chinese and once in English should get the same answer**. Four scenarios,
eight cases, one gold SQL per pair — supplier on-time rate, unpaid purchase balance,
cross-database order progress, and latest stock with its unit.

```bash
python -m benchmark.run_benchmark --dataset benchmark/bilingual_cases.json --repeat 3
```

It is **not part of the gate** — eight cases are too few to set a meaningful line. Its
job is to show, after a change that touches language (routing word lists, the
generation prompt, the aggregation cues in the shape check, the semantic self-check),
whether the English phrasing does worse than the Chinese one. Judging is the same as
for the evaluation set. `tests/test_bilingual_cases.py` locks that every gold SQL
returns rows on the built-in demo data; those tests check the fixture only and are no
substitute for a model run.

### Questions forwarded from customer service (customer_cases.json)

When a customer asks the helpdesk about an order, the helpdesk forwards the customer's
own words to this agent, and the rows that come back count as a source for the answer.
If the agent looks up the wrong order or the wrong field, the helpdesk repeats it. The
150 evaluation cases are all phrased like an analyst ("list the … of …"); none is phrased
like a customer. `customer_cases.json` fills that gap: 24 questions (19 Chinese, 5
English) in the words customers use, such as "where is my order", "how much longer",
"why hasn't it shipped", "when did it arrive", "has the replacement part gone out".
Every question names its order number.

```bash
python -m benchmark.run_benchmark --customer --output runtime_data/customer_report.json
```

It differs from the evaluation set in three ways:

- **It runs on the simplified business databases production uses** (`tools/simple_erp`:
  orders, production, warehouse, delivery and after-sales). `--customer` builds them
  from the fixed seed in a temp directory. The generator is deterministic: order
  statuses, dates, amounts and line items match production's copy, only the customer
  and supplier names are spelled differently, so every question is asked by order
  number. The schema index stays in memory even when `AI_SQL_PGVECTOR_DSN` is set,
  so the run never rebuilds the shared index tables a live service on the same
  database is using.
- **It records the routing decision too.** Production's entry point first decides
  "query the database or answer from the conversation"; questions routed to the second
  path never reach a database. Each item records how production would route it
  (`route`), but the pipeline still runs and is still scored against the gold SQL.
- **It is not part of the gate.** The report shows it on its own line (`customer`).
  It is not folded into single-database accuracy, and `gate.py` and `thresholds.json`
  never read it.

It is scored on **every step**, not just the last one. The helpdesk receives the rows of
all steps, and a customer question is often planned as orders → production → delivery;
when the answer sits in an earlier step, last-step-only scoring marks a right answer
wrong. An item counts as correct when any successful step matches the gold result under
the evaluation set's rule, and `matched_step` records which step (the 150 evaluation
cases and the gate still compare the last step only). An earlier-step match only counts
when the helpdesk can use it: the run carries no risk finding (with `has_risk` the
helpdesk's evidence gate uses none of the result), and the step fits inside the result
preview embedded in `answer` (about 1,500 characters), which is all the helpdesk reads,
and no other step contradicts it (the same column with a value outside the gold result).
Otherwise the item is wrong and flagged `unusable_match`; `mismatch` still describes the
last step, since that is what the helpdesk repeats. The report also says why each wrong
answer is wrong (`mismatch`): empty result, wrong row count, wrong values, or right
values with too many columns. Only the first three can be repeated to a customer as a
wrong fact, so the line counts silent errors with wrong values or row counts
separately. Without model credentials the run uses the mock model and rule routing:
that only shows the set runs and gets its own line, and the score means nothing.
`tests/test_customer_cases.py` locks the fixture (every order exists, every gold SQL
returns rows from one database, gold outputs only the facts asked for) and the offline
run.

## Regenerating the dataset

```bash
python -m benchmark.dataset_builder
```

## How the metrics relate

| Metric | What it measures | Needs a real model |
|---|---|---|
| **Column recall** | Retrieval: did every gold column reach the schema graph | No, measurable offline |
| **Execution accuracy** | End to end: result set equivalent to gold, ignoring row and column order | Yes |
| **Silent-error rate** | Trustworthiness: when it was wrong, was the user told | Yes |

The first two are independent: without recall, generation accuracy is irrelevant;
with recall, generation can still be wrong. Keeping them apart is what localizes
the bottleneck to retrieval or to generation.

The third is **not the same thing as accuracy**: accuracy dropping 1pp while every
error gets caught by a warning is a better outcome in practice — which is why it
is reported separately rather than folded into accuracy.

Offline, with the mock model, column recall is fully measurable while execution
accuracy only covers the smoke subset.

## Repair-loop gain

Run the same evaluation set twice with the switch flipped; the difference in
execution accuracy is the gain:

```bash
python -m benchmark.run_benchmark              # repair on
python -m benchmark.run_benchmark --no-repair  # repair off
```

## The holdout set (holdout.json)

Recall on the evaluation set gets pushed up by tuning — the phrase tables, column
aliases and enum vocabularies were all built against it. `holdout.json` asks the
same business questions in **entirely different colloquial phrasings**, which
quantifies how much of the number was tuned in, and serves as the actual basis
for the column-recall metric in the gate.

**40 cases, not 15.** With the gate at 85%, 15 cases make one case worth 6.7pp:
13/15 (86.7%) passes and 12/15 (80%) fails, so a single case flips the verdict. A
gate like that neither stops real regressions nor survives noise. At 40 cases one
case is 2.5pp, and the expansion also brought in columns that were previously
never asked about at all (`plan_qty`, `unit_cost`, `locked_qty`, the whole
`capacity_stat` table, and others).

The 25 added cases were **written once and frozen**: go through the schema table
by table, pick columns that were not covered, phrase a colloquial question, and
commit it — without looking at retrieval results, and without going back to
reword based on what did or did not match. Rewording turns the holdout set into a
second training set.

```bash
python -m pytest tests/test_recall_generalization.py -v
```

**These phrasings must never be fed back into the phrase tables or the alias
list**, or the holdout set loses its meaning and simply relocates the
overfitting.

Measured gap, same retrieval stack:

| Keyword extraction | Evaluation set | Holdout set | Threshold |
|---|---|---|---|
| Rule-based (offline mock) | 100% | 46.7% | — |
| Model-based (production path) | — | 66.7% | ≥ 85% |

The gap comes mainly from the semantic distance between colloquial phrasing and
schema metadata ("we're running low on stock" ↔ `safety_stock`, "the share that
comes out defective" ↔ `yield_rate`). The mitigation is to keep adding column
aliases — work that follows business feedback in a real deployment, not something
that can be finished in one pass.
