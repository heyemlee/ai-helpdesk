# Customer questions: scoring revisions

## Revision 3 (current)

Frozen before the second November 6 follow-up model run. Only c11 gains an
explicit alternative: `description, service_status` from the same service-ticket
rows, in addition to `issue_type, service_status`. Either form must preserve the
complete one-to-one association between each problem and its status. Statuses
alone, swapped statuses, omitted or duplicated problems remain incorrect. The
existing row, column, preview, risk and contradiction checks are unchanged.
Replay the previous full run before new provider calls and report its changed
score separately from model behavior improvements.

c03 keeps its original planned-date reference. The data provides a planned
delivery date, not a live ETA; subtracting today's date from an overdue plan
does not answer how long arrival will actually take. The application should
return the recorded plan without inventing a new arrival estimate. Explicit
date-difference requests remain supported and must specify a reference date.

c23 also keeps its reference. When the cause of an unshipped order is not yet
known, both production blockers and delivery blockers may be relevant evidence.
A NULL production note cannot justify omitting a delivery cause. Single-domain
cause questions and simple status questions must retain their narrower scope.

## Revision 2

Frozen before the November 6 follow-up model run. The 24 questions and business
records are unchanged. Their languages are Chinese (20) and English (4).

The evaluator continues to require a one-to-one row match, all requested values,
and at most one additional identifying column. Extra output is a shape failure,
not by itself evidence that the requested fact is false. The 150-question SQL
gate retains its existing references and final-step scoring.

Only c24's answer reference changes. “Has the replacement part … been shipped?”
asks a Boolean question. The primary reference computes `is_shipped` as 1 or 0
from `shipped_date`. The original actual shipping date remains an explicitly
accepted alternative. Both reference queries read the same record; no reference
value is learned from model output. Every alternative must match as a complete
result set; values cannot be mixed across alternatives. An equivalent final-step
answer takes precedence over an earlier-step answer. Earlier matches still need
the existing risk, preview visibility and contradiction checks.

For c02/c04/c05/c12/c16/c23, the requested facts in the first run were present but
the output was too wide. Keep the column budget and narrow the query plan instead
of accepting arbitrarily many unrelated fields. For c10, the question asks for
the missing-parts service ticket's processing status: returning two replacement
detail rows alongside the service row does not satisfy that result shape. For
c18, the order's current overall state is sufficient; absent downstream records
must not prompt an unnecessary traversal through production and delivery. A
question explicitly asking for those details still requires them.

Validation uses the original captured responses first, with no new API calls,
then new responses on the same frozen revision. Report reference corrections
separately from changes in model behavior. A single rerun of these development
questions does not establish general accuracy or meet the release acceptance
gate. Persist dataset, source and rubric hashes with each run.
