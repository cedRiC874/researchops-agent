# Kimi Candidate v7 controlled Pilot v2 — response-validation failure

## Outcome

One separately authorized post-lock Candidate v7 synthetic compatibility attempt failed closed during
local response validation on the first required-tool request.

```text
status                         failed
error_code                     kimi_chat_response_invalid
event description              local_response_validation_failure
causal root cause              undetermined_without_raw_provider_payload
G4 decision                    planned_not_registered
scenarios completed            0 / 3
model requests                 1 / 8
network attempts / calls       1 / 1
trusted Provider tool calls    0
tool executions                0
usage observations             0
usage complete                 false
local reserved estimate        CNY 0.313600
actual billed cost             unknown / null
candidate result created       false
retry / resume authorized      false / false
```

The authorization was consumed. No retry or fallback was attempted. Kimi remains unregistered;
Provider registration remains `1/2` and private remains `0/50`.

Candidate v7 was locked before this observation at commitment
`2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5`.
The post-lock failure is not inherited into Candidate v7, Pilot Pack v8, a model-quality claim or any
historical result.

## Exact local state transition

The verified four-event state machine was:

```text
run_authorized
  → request_started
      scenario=KIMI-SYNTH-TOOL-001
      state=tool_request_in_flight
  → request_failed
      state=tool_request_failed
      error=kimi_chat_response_invalid
  → run_terminal
      state=failed
```

There was no `request_completed`, `tool_batch_completed`, `scenario_completed` or invalid-request
probe event.

## Root-cause boundary

`kimi_chat_response_invalid` is a broad local parser taxonomy. It can represent response metadata,
media type/encoding, SSE framing or size, UTF-8/strict JSON, exact field sets, ID/object/model/index,
delta/reasoning/content ordering, terminal ordering, role/content completion or decoding failures.

Raw Provider headers and bodies were deliberately not persisted, so the unique live payload shape and
causal root cause cannot be selected. This evidence does **not** claim that Kimi returned a malformed
or invalid response, and it does not establish a new official-documentation mismatch.

No usage record passed validation. The first required-tool request did not finish parsing, so the raw
stream may or may not have contained partial tool fragments; no tool call became trusted and no tool
executed. The third-scenario HTTP 400 probe was never reached. Therefore usage, tool and error-semantics
compatibility all remain unverified.

## Usage, cost and timing boundary

The local receipt's zero input/output counters mean only that zero validated usage was accumulated.
Actual Provider input/output tokens must be reported as `null/unknown`, not zero.

`CNY 0.313600` is the pre-request all-cache-miss local reservation for the 8,000/1,536 request caps,
not a Provider invoice. Usage-based cost and actual billed cost remain `null/unknown`.

The checked-to-terminal wall clock was 21.378 seconds. The artifact has no explicit Provider latency,
so Provider latency remains `null` and no SLA claim is allowed. `outcome_unknown=false` means only that
the local FSM reached a known failed terminal state; it does not make Provider tokens or billing known.

## Publication boundary

The original receipt, checkpoint, event chain and cross-version tombstone passed strict JSON, hash,
FSM, Candidate binding, tombstone and sanitizer verification. They are not copied here because they
contain a potentially enumerable authorization hash, authorization-derived binding hashes, exact
times and internally linkable chain values.

The legacy tombstone file commitment is also omitted because the same value is copied into an
authorization-bound receipt field. The remaining receipt, checkpoint and event-chain file commitments
are opaque but intentionally linkable.

This public bundle contains only:

- [public receipt projection](public_receipt_projection.json);
- [opaque artifact commitments](artifact_commitments.json);
- [public source commitments](public_source_commitments.json).

It excludes the authorization ID/hash, authorization/attestation/tombstone binding values, event-chain
head, Key/header/request IDs, paths, email/account/project/user IDs, raw prompts/responses/reasoning,
tool arguments/results and free-text Provider errors.

## Claim boundary

This evidence proves only that one Candidate v7 attempt failed closed during local response validation
and that its local sanitized artifacts verify. It does not establish:

- Kimi Chat, usage, tool or error-semantics compatibility;
- actual tokens, cost, Provider latency or SLA;
- model quality, planning accuracy or unknown-distribution generalization;
- a registered second Provider;
- private or non-synthetic support;
- causal Provider fault.

The consumed authorization must not be retried. The frozen parser, prompt, scorer, tool schema,
scenarios, Candidate v7 and task selection must not be adjusted using this observation. Any later
attempt requires another versioned successor, new commitment and entirely new authorization.
