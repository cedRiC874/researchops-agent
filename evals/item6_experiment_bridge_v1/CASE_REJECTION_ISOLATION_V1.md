# Item6 case-rejection isolation v1

Protocol revision: `item6-case-rejection-isolation/1.0`.

This is a prospective, internal experiment protocol. It changes only the
post-rejection stopping behavior described below, for an explicit
`item6-experiment-freeze/1.1` carrying this revision and the exact SHA-256 of
`case_rejection_policy_v1.json`. It does not retrospectively change old runs,
authorizations, claims, schema files, or `CONTRACT.md`.

## Unchanged boundaries

The 16 developer-known synthetic tasks, 32 ordered business observations,
instructions, tool definitions and evidence, fixed FCC scorer and gold inputs,
provider/origin, budgets, deadlines, privacy checks, and single-host ownership
remain unchanged. This revision grants no execution authority by itself.
Offline-test capabilities and approvals cannot enter the live path. No Key,
approval token, exception body/args, raw response body, or reusable capability is
included in a rejection record. Depth-60 20/60 and the external STATUS/T7
milestones are unaffected.

## Eligible rejection: every condition is required

Only the Agent path may isolate an original
`item6_tool_before_design_or_refusal` rejection caused by `missing_design` or
`conflicting_design`. The frozen task must not satisfy the refusal predicate.
Its design list must respectively contain zero or multiple designs. This rule
applies by condition, never by an IC-11-specific task exemption.

The proposed action must be one of the original two tools, with valid argument
shape and scope/bundle/catalog preconditions. Eligibility uses only bound
frozen metadata for a pure check: it must not call `Case._read_tool`, execute a
tool, or move/relax the original rejection check. The original gate rejects
first; an ineligible rejection still stops the batch and never returns to tool
execution. Refusal requests, unknown tools, invalid or out-of-scope arguments,
unproved catalog prerequisites, multiple concurrent actions, or a case with
prior tool work are outside this revision.

The original Owner/source/deadline/case-identity and observed-state checks must
succeed before the original gate registers its rejection. Registration requires
zero previous tool executions, tool events, allowed evidence, side effects or
artifacts, and no final answer. The plan remains `not_executed`. This is not a
tool-start/tool-finish event and must not fabricate a function-call output.

## Non-transferable in-process association

The private experiment implementation associates the actual local exception
object with the current PID, Owner, Factory, Case, run and model-case identities,
freeze, original plan index/arguments, native call ID, accepted response/attempt,
reason, and single-use state. A JSON receipt or a matching error string cannot
reconstruct this association or authorize continuation.

The runner may recognize that same registered exception, or the exact locked
SDK `UserError` whose direct `__cause__` is that object. A new exception with the
same class/code, subclass, forged module, copied/serialized association,
cross-case/run/process binding, replay, deep cause/context, or missing evidence
does not qualify. `safe_error()` is only error attribution, not authority.

Registration immediately blocks further sends and tools in that case. There is
no retry, automatic clarification request, fallback, or replenishment of any
budget. `Owner.stop()` and timing halt/failed states are never cleared.

## Close before continuing

The only permitted sequence is:

1. Original tool gate rejects and persists the rejection event.
2. The SDK unwinds; no further SDK work runs for that case.
3. The actual Provider context exits normally. Cleanup failure, cancellation,
   timeout, or unknown completion is a batch stop, not an isolated refusal.
4. Reconcile every attempt, response, complete usage, settled charge, timing
   segment and audit event for this case. All attempts must be
   `response_accepted`, with the original native completion requirements and
   completed raw cleanup. No pending reservation, active attempt or attempt
   failure is allowed. The clock must not be halted/failed, and authorization,
   source, case/batch deadlines and budgets must remain valid.
5. Consume the real collector `seal_case()` result once: require
   `closure_eligible`, matched SDK/raw/request counts and complete nested
   indices. Merely returning without an exception is insufficient. Close the
   original case and persist its closed event and isolation-sealed event.
6. Recheck Owner/source/time before releasing the pending-isolation barrier.
   Only then may the next frozen business-plan entry use normal admission and a
   new Case/session/Agent/replay. No text, tool results or rejection authority
   are inherited.

A closure/audit error after a case has sealed or closed stops the batch. It does
not authorize a rollback, second seal, fabricated terminal, or suffix execution.
All request/token/cost observations remain counted. Transport/usage/privacy,
authorization/source, audit, unknown-result, expiry and other faults retain the
original whole-batch stopping rule.

## Auditable lifecycle

Four experiment-specific event types are added:

- `item6_business_case_started_v1`: every actually entered business case,
  including fixed cases with no model or tool work, before that work.
- `item6_case_tool_rejected_v1`: the actual original tool gate rejection.
- `item6_business_case_closed_v1`: a normally closed business case, linked to
  its start and a non-cyclic terminal observation projection.
- `item6_case_isolation_sealed_v1`: successful isolated closure, referencing
  the rejection and already-persisted closed event, never a future event or the
  final archive hash.

The rejected-case sequence is start, accepted response terminal, rejection,
closed, isolation-sealed, next start. Unexecuted cases cannot invent start or
closed events. Failures retain unmatched facts and cannot claim complete
collection. Any boundary-event write failure stops new work. The barrier stays
held between close and isolation-sealed even after `current` has been cleared.

Evidence binds the revision/policy, mode, run/case/model-case, freeze/source,
business/plan/attempt/response indices, safe error/reason/tool/argument hashes,
response-event/segment/completion/usage references, zero-execution counts and
real case reconciliation. Raw native call IDs are represented by safe hashes in
new rejection evidence. All writes retain the original privacy and byte limits;
the original 8 MiB archive cap is not enlarged. At most one rejection per Agent
case and sixteen per batch may be isolated.

Independent readback recomputes task eligibility, plan/argument identities,
event order and SQLite projections, per-case response/usage/timing and
reconciliation, budget and zero-tool facts. Complete collection requires all
32 starts/closed events in the frozen order and one-to-one rejection/sealed
pairs. The next start and any work event must follow the previous sealed event;
checking only the next model send would miss fixed-path work. This is local
producer-bound consistency evidence, not an external witness or recovered raw
provider body.

## Results and exit codes

An isolated case remains `execution_state=observed`, `status=failed`,
`completion=unknown`, `final_output=null`, with the original safe rejection
code and zero tool events. It is not replaced with a scripted clarification.
FCC inputs retain their original meaning; runtime rejection does not rewrite
FCC `unknown` into `fail` or turn a plan into an executed event.

The shared ledger/finalization lifecycle keeps `completed` / `failed`. The new
artifact and CLI additionally require `collection_status`:

- `complete`: all 32 observations collected, no isolated case rejection;
- `complete_with_case_rejections`: all 32 collected with one or more safely
  isolated case rejections;
- `stopped`: incomplete collection or a whole-batch stopping condition.

Actual run exit codes are respectively 0, 3, and 2. Exit 3 is a non-green
completed collection, not permission to retry. Exit 0 is not an FCC quality
claim. Complete collection also requires all 16 model-case reconciliations,
budget/phase/audit/cleanup/sealing checks; missing evidence means `stopped`.

The report separately counts planned, observed, not-executed, operationally
complete, failed-after-start, and isolated case rejections. `all_business_passed`
is true only with all 32 observed, zero operational failures or isolated
rejections, no scorer error, and all 32 FCC verdicts `pass`. Any fail, unknown,
not-scored, unexecuted or operational failure makes it false. Scorer errors or a
missing raw report are `not_scored`, not model unknowns. Scoring rules and
exception behavior are unchanged.

## Version and source binding

The new schema versions are freeze 1.1, artifact/archive 1.2. Original schema,
policy and contract files remain byte-preserved. Approval 1.0 still binds the
entire new freeze commitment; old approvals/claims and remaining windows or
budgets cannot be reused. New entrypoints accept only the explicit new freeze.
Old archives are interpreted by their fixed historical producer, not silently
upgraded by this entrypoint. Original finalization 1.0 and unfinished-failure
outer formats remain; their embedded artifact is strictly checked as 1.2.

The new normative Markdown and isolation test module are explicit v3 inputs;
new source and JSON schemas/policy are selected normally. The v3 domain,
algorithm, lineage, path/type/link/size bounds and exclusions do not change.
There is no self-hash: implementation/specification/policy/tests feed the
source manifest; workflows are separately Git-bound; commit/tree and freeze
then feed approval/claim and runtime evidence. Source-only continuation remains
an offline integrity operation, not execution permission.

No local synthetic validation, internal collection result or this protocol
completes the old external T6/T7 milestones or proves model generalization,
independent review, ranking, or production SLA.
