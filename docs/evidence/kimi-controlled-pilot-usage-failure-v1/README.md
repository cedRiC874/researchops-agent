# Kimi controlled synthetic pilot — usage-stage fail-closed evidence

## Outcome

One separately authorized post-lock synthetic compatibility pilot stopped during the first Chat
response's usage-validation stage.

```text
status                         failed
error_code                     kimi_chat_usage_invalid
G4 decision                    planned_not_registered
scenarios completed            0 / 3
model requests                 1 / 8
network attempts / calls       1 / 1
tool executions                0
usage observations             0
usage complete                 false
local reserved estimate        CNY 0.313600
actual billed cost             unknown / null
candidate result created       false
retry / resume authorized      false / false
```

The authorization was consumed. No retry or fallback was attempted, and this evidence does not
authorize another run. Kimi remains unregistered; Provider registration remains `1/2` and private
remains `0/50`.

Candidate v6 was locked before this observation at commitment
`57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641`.
The post-lock failure is not inherited into that candidate, Pilot Pack v7, any historical result or a
model-quality claim.

## Usage, cost and timing boundary

The local receipt contains zero accumulated input/output token counters because no usage object passed
the frozen validator. Those zeros are not evidence that the Provider consumed zero tokens. This public
projection therefore reports actual input/output tokens as `null` and actual billed cost as `null`.

`CNY 0.313600` is only the pre-request all-cache-miss local reservation under the pinned 20/100 CNY
per-million rates. It is not a Provider invoice or hard Provider-side bill cap.

The run wall clock from local checked/terminal timestamps was 15.921 seconds. No explicit Provider
latency survived the fail-closed projection, so Provider latency remains `null` and no SLA claim is
allowed.

## Offline postmortem

The frozen parser accepts only this terminal ordering:

```text
choices[0].finish_reason chunk
        ↓
choices=[] + usage-only chunk
        ↓
[DONE]
```

The current official Kimi streaming example instead places `choices[0].finish_reason` and top-level
`usage` in the same final data chunk before `[DONE]`.
[Official Chat Completions documentation](https://platform.kimi.com/docs/api/chat)

This confirms a parser/official-documentation contract mismatch. It does **not** prove that the live
response used that exact shape: the observed stable error was `kimi_chat_usage_invalid`, whereas the
current parser's synthetic same-chunk reproduction yields `kimi_chat_response_invalid`. Because raw
Provider bodies are deliberately not persisted, the specific live payload shape and unique causal
root cause remain:

```text
undetermined_with_confirmed_parser_contract_mismatch
```

Possible code-level sources of `kimi_chat_usage_invalid` include terminal ordering, duplicate usage,
an exact-field mismatch, a non-object usage value or invalid token arithmetic. None can be selected as
the live root cause without the prohibited raw body.

## Publication boundary

The original local receipt, checkpoint and event chain passed the offline projection verifier,
artifact sanitizer and independent hash-chain recomputation. They are not copied here because they
contain an authorization hash and authorization-derived binding hashes that could be linkable if the
original authorization ID were low entropy.

This public bundle contains only:

- [public receipt projection](public_receipt_projection.json);
- [opaque artifact commitments](artifact_commitments.json);
- [public source commitments](public_source_commitments.json).

It excludes the authorization hash and all authorization-binding/attestation hashes, Key, headers,
request/completion IDs, raw prompts, responses, reasoning, tool arguments/results, paths, email,
account/project/participant IDs and free-text Provider errors.

## Claim boundary

This evidence supports only that one controlled synthetic attempt failed closed during usage
validation and that the sanitized local artifact chain verifies. It does not establish:

- Kimi Chat, tool or error-semantics compatibility;
- actual token usage, price, bill or Provider latency;
- model quality, planning accuracy or unknown-distribution generalization;
- a formally registered second Provider;
- private or non-synthetic support;
- a causal claim that Kimi returned an invalid response.

Any future attempt requires a versioned successor design, new commitment and entirely new Key/budget/
one-time authorization. Candidate v6 and this consumed authorization must not be reused.

