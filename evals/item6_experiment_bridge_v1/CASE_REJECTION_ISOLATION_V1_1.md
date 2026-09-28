# Item6 case-rejection isolation v1.1

This prospective revision is `item6-case-rejection-isolation/1.1`. It requires
freeze 1.2 and the exact new `case_rejection_policy_v1_1.json`; outputs use
artifact/archive 1.3 and case-rejection record 1.1. Approval remains 1.0 and binds
the entire new freeze. No old approval, remaining window, claim or budget is
reusable. Historical v1.0 documents, policies, schemas, producer commits and
failed runs remain unchanged; old archives use their original producer.

## Narrow semantic delta

The original `Case.call` gate still rejects a tool request when the frozen task
requires refusal or lacks a unique design. Its location, error, assertions and
zero-tool behavior do not change. This revision changes only whether that
already-rejected case may be sealed as failed before the next frozen case.

In addition to `missing_design` and `conflicting_design`, the new reason
`policy_refusal` is eligible when the existing finite `paths.refusal(task)`
predicate is true. This is a frozen business-language predicate, not a task-ID
exemption, an arbitrary model refusal, or a generic safety-error catch. When
both refusal and design ambiguity hold, refusal takes classification priority.
The predicate, input, and resulting reason are independently recomputed during
archive readback. A model-produced reason string is not authority.

All original tool/argument/scope/catalog checks still apply before isolation:
`inspect_sources` must exactly name the frozen scope; `read_aggregate` must
exactly name the provided frozen bundle, present in the frozen scope catalog.
No tool runs to discover eligibility. Unknown tools, extra/out-of-scope arguments
and missing catalog preconditions remain batch stops, including refusal cases.

The original in-process exception association must be present and single-use;
same-code copies, foreign cases/runs/PIDs, subclass/deep-cause/context wrappers
or serialized receipts cannot authorize continuation. Previous tool execution,
tool audit events, evidence, side effects, artifacts or a final answer exclude
isolation. The original plan stays `not_executed`.

## Close-before-continue invariants

The v1.0 association, barrier and four-event lifecycle remain unchanged:
actual start, accepted response, actual tool rejection, case close, isolation
seal, then next start. After rejection there is no same-case send/tool/retry.
SDK unwinding and actual Provider cleanup must complete. Every native response,
usage, timing segment, attempt, charge and audit projection must reconcile with
the actual collector seal result. Pending reservations, unknown outcomes,
incomplete usage, source/authority/expiry/clock/budget failures, privacy or
audit I/O failures, cancellation, cleanup and sealing failure stop the batch.
No stop state is cleared, no allowance replenished, and no failed case resumed.

The durable closed and isolation-sealed events must both be acknowledged and
validated before the pending barrier is released. A seal write failure leaves
the case unisolated and prevents even a next fixed-workflow case from starting.
Each case permits at most one isolated rejection, sixteen per batch.

## Truthful outputs and unchanged scope

A safely isolated refusal remains `observed`, `status=failed`,
`completion=unknown`, `final_output=null`, with the original rejection code,
`reason_kind=policy_refusal`, zero executed tools and real usage/cost retained.
No scripted refusal/clarification, fake tool output or substitute answer is
produced. FCC input/measurement semantics are unchanged; unknown is not rewritten
to fail. Normal fixed-workflow refusal behavior is unchanged.

Exit 0 means complete collection without isolated rejections; exit 3 means
complete collection with failed, isolated cases; exit 2 means stopped/incomplete.
Neither exit 0 nor 3 is a quality claim. `all_business_passed` still requires
all 32 observations, no operational failure or isolation, and all FCC verdicts
pass. The full 16-task/32-observation denominator and order remain mandatory.

Task text, instructions, tools, evidence, FCC bytes, scope, provider/model,
budgets, timing, privacy/size limits, single-host claim store and Key path do
not change. This protocol authorizes no live execution. New execution source,
policy, schemas, normative text and tests must be source-bound before any fresh
online authorization. The source manifest excludes itself; workflows remain
separately Git-bound. An offline fixture manifest is not a live commitment.

The previous IC-15 failure remains a valid stopped result under v1.0, not a
bug retrospectively repaired by this revision. The original 20/60 and external
STATUS/T7 remain unchanged. No claim of independent review, unknown-task
generalization, model ranking or production SLA follows from this revision.
