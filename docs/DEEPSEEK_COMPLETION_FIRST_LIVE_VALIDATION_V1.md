# DeepSeek completion telemetry first-live validation v1

Status: **contract confirmed / offline implementation complete / not run / fresh authorization required**.

This contract is the bootstrap step between the validated direct `/responses` probe and a future
campaign runtime binding. It is not an evaluation and can never close the open completion-telemetry
defect by itself. Its only successful claim is narrow: the exact DeepSeek Adapter path produced the
two preregistered native completion shapes and persisted their sanitized records through the real
ledger integration. Even then, equivalence is limited to `status` and `incomplete_details`
presence/value projections; it does not establish full request-kwargs, transport or backend-version
equivalence.

The confirmed design contract remains immutable at commitment
`ddff10f30031faf77d6417dd695dd61dae4c6a45334efae7388ab4f2adc4a5bc`.
The implementation successor is separately committed as
`187bb6b537f2bdffb4bf77581550ea0ca81d02fb52d44d110b22a450ae0dce10`.
Neither value authorizes a request. The current source-integrity successor is Depth-60 v5,
commitment `8a5474db1e9ad59d501bf109d4a7ecbf616f40599763a20188581e336d379bd7`,
with `online_execution_authorized=false`, `network_calls=0` and `model_calls=0`.

## Why this is a separate run

The current registry deliberately has `runtime_binding_allowed=false` for every Provider. DeepSeek
has live direct-probe evidence, but that probe did not use the Adapter's SDK-built request kwargs.
Promoting the direct probe without observing the Adapter path would turn an unverified equivalence
assumption into runtime authority. The first-live validation therefore precedes—and cannot perform—
registry promotion.

The validation uses `DeepSeekProvider.open_model` and the wrapped
`OpenAIResponsesModel._fetch_response` path directly. It does not invoke an Agent Runner, tools,
task scoring, a frozen evaluation task, or any repo-local holdout.

## Locked scenarios and limits

The machine contract fixes two public synthetic inputs in order:

1. a normal response with `max_output_tokens=256`, expected to map to `completed`;
2. a forced-cap response with `max_output_tokens=16`, expected to map to `incomplete_length`.

There are at most two network attempts, two network calls and two model requests, with concurrency
one and zero SDK or HTTP retries. A validation-only HTTP request hook counts and validates each
actual `POST https://api.deepseek.com/responses` send; invocation counters are not reused as network
observations. Resume, fallback and tools are disabled. A coroutine wall timeout bounds each request
at 120 seconds independently of HTTPX stage timeouts, and the request phase is capped at 300
seconds. Each raw response gets a five-second cleanup bound (at most two), as does each of the two
post-request transport resources; the whole process has a 330-second wall limit. Input UTF-8 bytes
are bounded before Key access; token and cost limits are observed after each response and are not
Provider billing guarantees.

The 330-second limit is not extended by receipt writing. The implementation reserves its final 30
seconds inside that same limit for bounded transport cleanup, evidence finalization and the terminal
receipt; it does not grant an additional 30-second online or authorization window.
This is an in-process deadline under a responsive local filesystem: Python cannot preempt a kernel-
blocked synchronous `fsync`. A hard deadline against that host-level failure requires an external
process supervisor. This residual limitation does not authorize a run or weaken any network cap.

Pricing values are intentionally absent from the design artifact. A fresh official DeepSeek pricing
attestation at the exact allowlisted URL
`https://api-docs.deepseek.com/zh-cn/quick_start/pricing/` must be bound to the eventual one-time
authorization before executable code may load a Key. The local observed-cost stop is CNY 1.00.
Provider-side retention and the actual bill remain unverified.

## Authorization and receipts

The contract does not authorize itself. A future executable successor must require a new
authorization ID, UTC expiry, exact design and v5 commitments, exact merged executable-successor
`main` commit, explicit acceptance of all locked caps and `--confirm-online`. The local clean `HEAD`
and local `refs/remotes/origin/main` must both equal that authorized commit; a clean feature branch
cannot satisfy the gate. It must exclusively create a consumption receipt before reading
`DEEPSEEK_API_KEY`; success, failure, timeout, cancellation or
`outcome_unknown` consumes the authorization and forbids retry.

The authorization also carries an external
`expected_authorization_binding_sha256`. Binding v2 uses the hashed authorization ID plus the exact
expiry, design/implementation/source commitments, execution commit, pricing snapshot and locked
limits. Calculate it offline before issuing the authorization:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli `
  deepseek-completion-first-live-bind-authorization `
  --authorization-id <new-id> `
  --authorization-expires-at-utc <UTC-expiry> `
  --expected-contract-commitment <design-commitment> `
  --expected-source-integrity-commitment <v5-commitment> `
  --expected-execution-commit <merged-main-commit> `
  --pricing-snapshot-date <YYYY-MM-DD> `
  --pricing-source-url https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ `
  --input-price-per-million-cny <price> `
  --output-price-per-million-cny <price>
```

An independently retained expected copy of the returned binding must remain outside the artifact
and be supplied to both run and verify with `--expected-authorization-binding-sha256`. The same
computed hash is also persisted for reconciliation, but neither command may read its expected value
back from the receipt. The calculator is offline, does not consume the grant, does not load a Key
and does not authorize execution. The calculator, run gate and artifact verifier all apply the same
locked worst-case CNY 1 reservation; a zero-request failure receipt cannot bypass that reservation.

Only canonical, sanitized consumption and terminal receipts may be written. Raw response bodies,
message text, repeated input text, system prompts, tool arguments, credentials, exception text,
absolute paths and raw Provider request IDs are forbidden. The artifact root and exact filenames are
fixed by the machine contract; the authorization-hash directory must not already exist.
Verification materializes the authorized Git commit with lazy fetch and replacement objects
disabled, rejects unsafe archive members, and runs the complete v5 plan/component validator against
that committed tree. It does not infer execution identity from the plan JSON alone.

## Success is not closure

Success requires exactly two accepted response records, complete SDK response/usage reconciliation,
a valid audit chain, `live_adapter_write` provenance, native-status truncation signals and the locked
state order `completed`, `incomplete_length`. Any `unmapped`, `not_provided`, rejected response,
cleanup failure, missing attempt or count mismatch fails the validation.
`usage_complete=true` additionally requires an exact bijection between every started attempt,
observed HTTP send and persisted complete-usage record. If any sent attempt lacks usage—for example,
the first request succeeds and the second times out—or any observed usage breaches a locked cap—the
top-level input/output token counts and cost remain `null`; retained accepted records are partial
forensic evidence, not a complete bill.

Even a successful validation must publish `closure_claim_allowed=false` and
`automatic_registry_promotion_allowed=false`. Promotion requires a later evidence PR, mapping review
and a new registry successor. The final telemetry closure run then needs a separate, externally
preregistered new/unseen task and a second one-time authorization.

The `main@20ad1da…` identity in this design is only the design base. It is not executable authority:
implementation changes will require a new committed source-integrity successor and an exact future
`main` binding before any authorization can be consumed.

## Offline verification and implementation boundary

The public run API and CLI expose no API-Key, prompt, model, origin, clock, Git resolver, artifact
root or transport override. Test seams exist only on a private implementation function. A dedicated
`first_live_validation` authority is rejected by ordinary campaign ledger sessions and generic
Phase 6. The Adapter accepts only the exact validation session type for DeepSeek.

Offline status can be inspected without a Key or network request:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli deepseek-completion-first-live-validate
```

MockTransport tests exercise the real OpenAI SDK request builder and wrapped Adapter path. They
cover both locked completion shapes, pre-consumption gates, per-response token/cost stops, timeout,
cancellation, raw-response cleanup failure, single-use behavior, partial evidence, privacy scans,
manifest tampering and self-consistent event-hash rewrites. The offline verifier requires an exact
top-level file set, exact JSON field sets, an exact ledger run lifecycle/event vocabulary/order,
cross-artifact projections, and independently recomputed token/cost caps. Full and manifestless
paths share the same exact event parser, including terminal payload, binding, error and
`outcome_unknown` semantics. Terminal Key-loaded and raw-cleanup flags are derived from the ledger
stage rather than accepted as free booleans. Known outer runtime errors must satisfy a code-specific
trace predicate; unknown downstream codes are normalized to an observed stage instead of being
persisted verbatim. Phase/whole-run timeouts and external cancellation intentionally stop at the
ledger-status boundary: asynchronous cleanup can mask any already-terminal body path, so those
outer signals do not authorize a narrower causal claim. A complete, strictly parsed event stream
also requires `write_failed=false`. A
manifestless failure hashes each partial artifact in the terminal receipt, permits only an exact
prefix of the fixed writer order, validates every present partial JSON schema and cross-projection,
and never rewrites an observed network count as zero. No Provider call or Key load occurred during
implementation.

Machine contract:
[`deepseek_responses_adapter_validation_contract_v1.json`](../evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_contract_v1.json)

Implementation successor:
[`deepseek_responses_adapter_validation_implementation_v2.json`](../evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v2.json)
