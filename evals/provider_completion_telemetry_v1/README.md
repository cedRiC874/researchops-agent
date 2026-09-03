# Provider completion telemetry mapping v1

Status: `executable_offline_mapping / fixture_verified / runtime_sanitizer_validator_implemented / zero_adapter_changes`

This package fixes and executes the initial Provider-to-`normalized_completion_state` mapping,
publishes sanitized fixtures, and implements the purpose-built in-memory sanitizer plus strict
record/artifact validators needed before an event-chain write. It does not change a Provider
Adapter, execute an evaluation task, write a run ledger, register a Provider, or support a
model-quality claim.

## Persisted record contract v1 and offline runtime controls

[`provider_completion_record_contract_v1.json`](provider_completion_record_contract_v1.json) and
[`schemas/provider_completion_record_v1.schema.json`](schemas/provider_completion_record_v1.schema.json)
fix the payload contract for a future append-only `model_response_telemetry_recorded` event. The
record keeps Provider-native completion metadata separate from normalized state, represents
`provided` JSON null, `not_provided`, `not_persisted`, and `unmapped` without collapsing them, binds
the Adapter and mapping versions plus the exact mapping SHA-256, and carries usage and the inputs
needed to audit any token-cap fallback. The contract also fixes the write order: allowlist,
request-ID hashing, purpose-built sanitization and byte limits, exact surface-keyed mapping, strict
validation, then one event containing both completion metadata and usage.

The schema now has a purpose-built sanitizer, strict record validator, and strict artifact
validator, but no Provider Adapter, runner, audit-ledger writer, or online execution path consumes
the record yet. The live builder and validator require an opaque runtime binding produced by the
strict surface loader; an arbitrary binding or callable resolver is accepted only by the explicitly
offline API, whose records use `record_provenance=offline_validation` and cannot satisfy closure.
A green contract test therefore does not show that response metadata is being persisted and cannot
close the open telemetry defect.

The mapping key for runtime persistence is the exact triple
`(provider_id, api_surface, transport_id)`, not Provider ID alone. The v2 surface registry now
represents DeepSeek Responses explicitly, but all v2 entries remain runtime-disabled: its two live
shapes came from a minimal direct probe rather than Adapter-built kwargs. OpenAI and Kimi also lack
Adapter-path live validation, while Anthropic additionally lacks proven pre-conversion native
metadata through LiteLLM. Runtime authority therefore cannot currently be created for any Provider;
future promotion requires a new verified registry successor rather than flipping a v2 boolean.

The closing gate is intentionally denominator-aware without pretending that a tool-using Agent has
a knowable exact response count before execution. The runtime plan preregisters the case IDs,
`max_turns` per case, total model-request cap, zero SDK/HTTP retries, and the fixed
`transport-response-finalization-v1` derivation algorithm. Each transport attempt begins before the
network call and has exactly one terminal outcome. Only actually observed Provider responses receive
a dynamic `response_index`; the exact response denominator is derived after the run and is explicitly
marked `derived_after_run=true` and `exact_response_count_preregistered=false`.
Closure additionally requires every planned case to reconcile, every attempt to be terminal, SDK
`raw_responses` and usage-request counts to match the derived transport counts, exactly one
schema-valid event for every derived response index, a valid event chain,
matching Adapter/mapping commitments, recognized non-null native terminal metadata, and
`truncation_signal_source=native_status` for every response. `unmapped`, `not_provided`,
`not_persisted`, a rejected telemetry write, or green CI by itself forbids the claim. The contract
does not retroactively recover Kimi or Depth-60 attribution, authorize a rerun of seen tasks, or
change the locked Depth-60 result of `20/60`; any new accuracy number still requires a
preregistered replacement evaluator on new unseen tasks.

## Provenance tiers

| Tier | Source | Applies to | Confidence |
| --- | --- | --- | --- |
| `live_capture` | Sanitized projection of a real response | DeepSeek | May support attribution for the two observed shapes only |
| `official_schema` | Literal enums in an official OpenAPI-generated SDK/type definition | OpenAI, Anthropic | Initial mapping only; first real call must validate value and key presence |
| `doc_prose` | Official narrative documentation | Kimi | Weakest; shape unverified and OpenAI-style compatibility is known incomplete |

Every fixture carries `provenance.tier`. Only an unmodified fixture with
`tier=live_capture`, `unverified_shape=false`, and `live_validation=validated` may be used as
observed-shape attribution evidence. Synthetic missing/unknown fixtures remain negative tests even
when their base fixture was captured live.

## Fixture envelope

The mapper may read only `response_projection`. It must not read the filename, `fixture_id`,
`scenario`, provenance, derivation, or expected result.

`response_projection` permits only:

- `status`
- `finish_reason`
- `stop_reason`
- `stop_sequence`
- `incomplete_details`
- `usage`
- `provider_request_id_sha256`
- `http_status`

Keys are intentionally absent in the missing-field fixtures; absence is not rewritten to `null`.
The original request ID is never persisted.

The package contains 29 fixtures: the original four scenarios for each Provider plus seven Anthropic
conditional-companion cases and six OpenAI missing/null/non-null/type consistency cases. The 13 added
cases are synthetic mutations with `official_schema` provenance and `unverified_shape=true`.

## Executable mapper

The pure interpreter is
[`researchops_completion_telemetry.mapping`](../../src/researchops_completion_telemetry/mapping.py):

```python
map_completion(response_projection, provider_id, mapping)
```

It performs no I/O and imports no Provider Adapter. It lives outside the `researchops` package, so
importing it loads no `researchops` module at all; this is enforced by a subprocess test rather than
by source inspection alone. Rules are discovered from the JSON and grouped by each rule's
`precedence_stage`; the interpreter walks `mapping_precedence`, never array order.
More than one match inside a stage fails closed with `mapping_rule_ambiguous`. Every materialized
rule has a stable `rule_id`, and every fixture expectation asserts the complete result tuple:
normalized state, signal source, preserved native value, and matched rule ID.

Before evaluating any response, the interpreter validates every materialized rule and every nested
condition against an exact per-operator field schema. A malformed lower-priority rule or a malformed
child hidden behind boolean short-circuiting therefore fails closed before a higher rule can return.

Missing keys use a dedicated sentinel during dotted-path resolution. A present JSON `null` is
therefore distinct from an absent key; `all_missing` and `is_null` cannot collapse into one branch.
The OpenAI nested-reason rules also require a present object parent, making the same-stage rules
mutually exclusive. A present non-object `incomplete_details` value is an explicit conflict and maps
to `unmapped`, rather than being mistaken for a missing nested `reason`.

Unknown native strings are preserved only through a rule-declared 64-byte UTF-8 cap. An over-limit
or non-string value fails closed; the mapper does not retain an unbounded Provider value while
waiting for a future Adapter sanitizer.

The token-cap-fallback stage has zero materialized rules in v1. The input projection does not carry
the persisted request cap or the counter-comparability metadata needed to evaluate that stage, so
the interpreter leaves it explicitly deferred instead of translating its prose into Python logic.

## Executed rule coverage

Fixture execution covers 29 of 55 materialized rules (`52.73%`):

| Provider | Materialized rules | Fixture-covered rules |
| --- | ---: | ---: |
| DeepSeek | 5 | 4 |
| OpenAI | 23 | 10 |
| Anthropic | 21 | 11 |
| Kimi | 6 | 4 |
| **Total** | **55** | **29** |

The other 26 rules are not selected by fixtures. Exactly two of them,
`deepseek-cc-absent-002` and `openai-resp-unknown-002`, are selected as the final matched rule by
targeted inline edge-case tests instead. The completion-mapper test suite therefore selects 31 of
55 rules as `matched_rule_id`, while 24 rules are never selected as the final match. Both figures
are published because the fixture-selected count alone understates what the suite selects, while
the word "some" would hide how many rules remain unselected. The `29/55` figure is fixture-selected
coverage and is not padded with extra fixtures. Only two rules are exercised by unmodified
validated DeepSeek live captures; every other selected branch is a schema/prose seed or a synthetic
mutation and is not Provider-shape attribution.
The exact fixture-selected, inline-only, combined-selected, and never-selected rule IDs are published in
[`rule_coverage_v1.json`](rule_coverage_v1.json).

## DeepSeek active v1 mapping

API surface: `openai_compatible_chat_completions`

| Native condition | Normalized state | Signal source | Evidence |
| --- | --- | --- | --- |
| `finish_reason == stop` | `completed` | `native_status` | Live capture |
| `finish_reason == length` | `incomplete_length` | `native_status` | Live capture with `max_tokens=1` |
| `finish_reason` missing | `not_provided` | `none` | Synthetic mutation from live capture |
| Any other value | `unmapped` | `native_status` | Synthetic unknown-value mutation |

DeepSeek officially lists `content_filter`, `tool_calls`, and
`insufficient_system_resource`, but none was observed in this capture. Mapping v1 records them as
known-but-unobserved and deliberately leaves them `unmapped`. Promotion requires a sanitized live
fixture, review, and mapping-version bump.

## OpenAI Responses initial mapping

Provenance: `official_schema`, OpenAI Python SDK `3.1.0`; shape is unverified.

| Native condition | Normalized state | Signal source |
| --- | --- | --- |
| `status == completed` | `completed` | `native_status` |
| `status == failed` | `error` | `native_status` |
| `status == cancelled` | `incomplete_other` | `native_status` |
| `status == incomplete` + `reason == max_output_tokens` | `incomplete_length` | `native_status` |
| `status == incomplete` + `reason == content_filter` | `incomplete_content_filter` | `native_status` |
| `status == incomplete` + missing/null details | `incomplete_other` | `native_status` |
| `status == queued` | `unmapped`; terminal write forbidden | `native_status` |
| `status == in_progress` | `unmapped`; terminal write forbidden | `native_status` |
| Status and completion details both missing | `not_provided` | `none` |
| Unknown status or unknown incomplete reason | `unmapped` | `native_status` |

A terminal status combined with contradictory non-null `incomplete_details` is `unmapped`, not a
status-priority nearest match. A missing or null `status` maps to `not_provided` only when
`incomplete_details` is also missing or null; non-null details without a status are contradictory
and map to `unmapped`. These combinations are expressed entirely inside the declared condition
AST—there is no out-of-tree guard for an evaluator to ignore.

Official OpenAI documentation confirms that Responses exposes `status`, `incomplete_details`,
`usage`, and `max_output_tokens`; this package does not treat that schema as a captured response.

## Anthropic Messages initial mapping

Provenance: `official_schema`, generated SDK files at commit
`691471837f19dd4ce50fd96eaf91993a0eb4d72a`; shape is unverified.

| `stop_reason` | Normalized state | Signal source |
| --- | --- | --- |
| `end_turn` | `completed` | `native_status` |
| `stop_sequence` | `completed` | `native_status` |
| `tool_use` | `completed` for the Provider response only | `native_status` |
| `max_tokens` | `incomplete_length` | `native_status` |
| `model_context_window_exceeded` | `incomplete_length` | `native_status` |
| `pause_turn` | `incomplete_other` | `native_status` |
| `refusal` | `incomplete_content_filter` | `native_status` |
| Missing final stop reason | `not_provided` | `none` |
| Unknown value | `unmapped` | `native_status` |

A null stop reason in an intermediate streaming event must never create terminal telemetry.
`tool_use=completed` does not claim that the Agent task is complete.

`stop_sequence` is a conditional companion field. It must be non-null when
`stop_reason=stop_sequence` and must be missing or null for every other recognized stop reason.
The reverse inconsistencies are also conflicts: a missing or null `stop_reason` cannot accompany a
non-null `stop_sequence`. Any non-null companion must also be a string; another JSON type is a
conflict. Conflict rules run before the ordinary mapping and produce `unmapped`.

## Kimi initial mapping

Provenance: `doc_prose`; all Kimi fixtures have `unverified_shape=true`.

| `finish_reason` | Normalized state | Signal source |
| --- | --- | --- |
| `stop` | `completed` | `native_status` |
| `length` | `incomplete_length` | `native_status` |
| `tool_calls` | `completed` for the Provider response only | `native_status` |
| Missing | `not_provided` | `none` |
| Unknown value | `unmapped` | `native_status` |

No successful Kimi handshake has been observed. These fixtures do not prove actual top-level field
presence, null-versus-absence behavior, usage placement, or OpenAI compatibility.

## Missing, unknown, and fallback rules

- Conditions use explicit operators such as `equals`, `all_missing`, `is_null`, and
  `default_unknown`. Strings such as `missing_or_null` are not native values and are forbidden.
- Every semantic predicate must live inside the condition AST; evaluator-visible sibling guards
  are forbidden. DeepSeek and Kimi each have one completion discriminator, while OpenAI couples
  `status` with `incomplete_details` and Anthropic couples `stop_reason` with `stop_sequence`, so the
  latter two require explicit consistency rules.
- Unknown native values always produce `unmapped`, preserve the bounded original value, and take
  precedence over token-cap fallback.
- `not_provided` means the current source exposed no completion metadata. `not_persisted` is
  reserved for historical records and is never substituted for `not_provided`.
- Token-cap fallback is allowed only when native completion metadata is absent and the persisted
  request cap is semantically comparable to, and exactly equals, the observed output-token count.
- Token-cap fallback cannot satisfy a claim that additional truncation was excluded.
- A run may claim that additional truncation was excluded only when every response has
  `truncation_signal_source=native_status` **and** a recognized terminal
  `normalized_completion_state`: `completed`, `incomplete_length`, `incomplete_content_filter`,
  `incomplete_other`, or `error`. Any `token_cap_fallback` or `none` forbids the claim, and so does
  any `unmapped`. `unmapped` also carries `native_status`, so the source test alone would be
  satisfied vacuously by a value the mapper could not interpret. This is a live path rather than a
  hypothetical one: DeepSeek's officially declared `content_filter`, `tool_calls`, and
  `insufficient_system_resource` are all `unmapped` under mapping v1.

The pure run-level function `may_claim_truncation_excluded(states_and_sources, mapping)` reads and
enforces this policy from the same JSON. An all-`unmapped` run, a run containing
`token_cap_fallback`, and an empty response set all return false; a non-empty run whose responses
are all recognized terminal states with `native_status` returns true. A runtime-policy mutation test
proves the result follows the supplied mapping rather than a duplicated Python constant.

Rule precedence is fixed: contradictory metadata, recognized non-null native values, unknown
non-null native values, token-cap fallback when native metadata is absent, then `not_provided` when
the fallback is not applicable. This keeps an unknown native value from being hidden by fallback.

## First-live validation constraint

OpenAI, Anthropic, and Kimi start from non-live evidence. On the first real call, the future Adapter
must compare observed native values and key presence against this table.

For every table miss it must:

1. emit `normalized_completion_state=unmapped`;
2. preserve the bounded native value;
3. emit a `provider_completion_mapping_unmapped` telemetry event;
4. forbid nearest-enum coercion and silent acceptance;
5. require a sanitized live fixture, review, and mapping-version bump before promotion.

`unmapped` is therefore an expected initial state for non-live Providers, not an exceptional path.

## Write-time sanitizer and runtime validators

[`researchops_completion_telemetry.sanitization`](../../src/researchops_completion_telemetry/sanitization.py)
is an I/O-free, Adapter-independent implementation of the persisted-record contract. It accepts
only the native capture allowlist, hashes a syntactically safe Provider request ID in memory, scans
every retained and omitted JSON leaf before projection, and emits the exact versioned record shape.
The scanner rejects API-key/token forms, Authorization or Cookie headers, Windows/UNC/POSIX
absolute paths, traceback forms, email addresses, and caller-supplied canaries. It never relies on
artifact reading to repair an unsafe value. `SanitizedCompletionCapture` stores canonical bytes,
locks normal attribute mutation, returns defensive projections, and gives the collector a separately
owned snapshot; changing the caller's capture after `append` cannot change the collected slot.

Mapping-critical `status`, `finish_reason`, `stop_reason`, and `incomplete_details.reason` values
are limited to 64 UTF-8 bytes and fail closed rather than being changed. `stop_sequence` may be
bounded to 64 UTF-8 bytes only with a matching `[TRUNCATED]` marker and flag. The raw
`incomplete_details` canonical size is measured before unknown child values are omitted; only
`reason`, the generated marker, and the omitted-child count survive. Usage retains sorted numeric
or null leaf counters only, with depth 4, 64-leaf, and 4 KiB limits; every omission or limit makes
`usage.complete=false`. Missing usage, explicit-null usage, and historical uncaptured usage remain
distinct through `native_value_state`.

Live record validation replays the frozen mapper inside an opaque
`VerifiedRuntimeCompletionBinding`, which can be created only from a strict-loader
`purpose=runtime_binding` selection. The binding fixes the exact
`provider_id + api_surface + transport_id`, actual schema and mapping hashes, Adapter version, and
output-counter semantics. Live APIs accept neither a caller-supplied resolver nor a caller-supplied
counter path. Explicit offline APIs still accept injected fixture resolvers, but always emit
non-closure-eligible `offline_validation` provenance. The mapper sees only `status`, `finish_reason`, `stop_reason`,
`stop_sequence`, and projected `incomplete_details`; usage, request ID, HTTP status, and request cap
are structurally unavailable to Provider mapping. Token-cap fallback is evaluated separately by the
record builder only after native mapping reports `not_provided`, using the binding-fixed output
counter and the persisted cap. Each record also binds the record-schema SHA-256. Runtime collection
uses a `VerifiedRuntimeDenominatorPlanBinding`: it binds case IDs and request ceilings rather than an
invented exact response count. The Adapter-facing case session exposes `begin_attempt()` plus exact
terminal methods; only `response_accepted` and `response_rejected` allocate response indices. The
runner seals each case with SDK raw-response and usage-request counts, then seals the campaign to
derive the observed denominator. A mismatch, unavailable SDK count, rejection, cancelled/no-response
attempt, pending attempt, or unfinalized planned case keeps the future closure result false.
Adapter/event-chain integration is still not implemented. DeepSeek has a
surface-keyed offline successor but no runtime authority; Anthropic cannot be bound until the
LiteLLM path proves and tests that `stop_reason`, `stop_sequence`, HTTP status, and request ID are
observed before OpenAI-style conversion.

## Capture record and boundary

Two sequential DeepSeek calls were made on 2026-09-02 against the official origin with model
`deepseek-v4-flash`, no tools, no retry, non-streaming mode, and trivial input whose content was not
persisted:

- `max_tokens=16` returned HTTP 200 and `finish_reason=stop`;
- `max_tokens=1` returned HTTP 200 and `finish_reason=length`.

Only the allowlisted response projection and hashed Provider identifier were retained. No response
content, reasoning, prompt, tool arguments, raw headers, API key, account identifier, or absolute
path is present in the fixture package.

These calls were telemetry-contract development probes. They were not evaluation tasks, did not
enter a run ledger, did not produce `task_pass`, and cannot support a Provider-registration or
model-quality claim.

## Source commitments

| Source | Version / commit | SHA-256 |
| --- | --- | --- |
| OpenAI `response_status.py` | SDK `3.1.0` | `3f450d799631c182331ede5280d44e4ac536771aba265812136ebbadfa4b831f` |
| OpenAI `response.py` | SDK `3.1.0` | `96aea83293bd7131e28ae863d8201c783f0f1fc4294b40d5ce138883e6aab9c6` |
| Anthropic `stop_reason.py` | `691471837f19dd4ce50fd96eaf91993a0eb4d72a` | `f71a61017fa9ba24d93200f0db43937c6d20deb8372f4837e11083e15a66ade2` |
| Anthropic `message.py` | `691471837f19dd4ce50fd96eaf91993a0eb4d72a` | `175ed6fed6f7fe03f2157f38b2937106391f8f845d6012128bf7b29df0c30829` |

Official references:

- [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create)
- [Kimi Chat Completions](https://platform.kimi.com/docs/api/chat)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)

Machine-readable rules are in [provider_completion_mapping_v1.json](provider_completion_mapping_v1.json).
Fixtures are under [fixtures/](fixtures/); byte commitments and provenance summaries are in
[fixture_manifest.json](fixture_manifest.json). Executable behavior and the coverage report are
locked by [test_provider_completion_mapping.py](../../tests/test_provider_completion_mapping.py).
