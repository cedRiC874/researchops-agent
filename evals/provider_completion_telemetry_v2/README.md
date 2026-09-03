# Provider completion surface registry v2

Status: **executable offline registry / zero Adapter changes / zero online calls**.

Here, `zero online calls` and the registry field `online_calls_performed=false` are scoped only to
constructing this offline registry successor. They do not describe the provenance source: the
included v3 receipt records three calls from an earlier, separately authorized live shape probe.
No additional Provider call was made to turn that receipt into this registry and fixture set.

This successor fixes one binding ambiguity without changing or replacing the v1 mapping. Mapping
selection is keyed by the exact triple `(provider_id, api_surface, transport_id)`. There is no
nearest-surface, single-surface, or same-Provider fallback. The immutable predecessor is
`evals/provider_completion_telemetry_v1/provider_completion_mapping_v1.json` (48,071 bytes,
SHA-256 `2a3e4c6a81fd76d9e20542091fbffdd4c6137d2e730b626117446109658b946d`).
The selector also verifies the parsed predecessor against its separately published canonical-JSON
SHA-256. The file-byte commitment remains the release/load-time check. Runtime callers cannot pass
an ordinary parsed dictionary: `load_verified_surface_registry` first reads the fixed manifest,
checks exact fields and unique IDs, resolves every declared path inside its fixed root, verifies
byte length and SHA-256 before parsing, and verifies the registry, v1 predecessor, four fixtures,
and source probe receipt. It also recomputes the probe/source origin, model, path, probe ID,
distinct-shape count and native shapes; replays the two unmodified live fixtures from their named
probe records; and replays every declared synthetic mutation from its committed live base. Only the
resulting opaque `VerifiedSurfaceRegistry` is accepted.

Before any v1 alias is exposed, the same loader also validates the v1 fixture manifest and exact
field sets for all 29 v1 fixtures, recursively scans their string leaves, enforces the bounded
numeric/null `usage` tree, replays every declared mutation, and executes every fixture against its
committed mapping expectation. For v2 and the probe receipt it applies the same response-projection
and privacy checks; changing a receipt, registry, fixture, and every enclosing hash together cannot
turn an extra body/content/exception/header field or a sensitive string into an accepted artifact.

## Registered triples

| Provider | API surface | Transport | Mapping source | Runtime binding |
|---|---|---|---|---|
| `deepseek` | `responses` | `openai_compatible_responses` | v3 sanitized live probe successor | offline only; direct-probe/Adapter equivalence unverified |
| `openai` | `responses` | `openai_responses` | exact v1 Provider mapping alias | offline only; official-schema seed unverified live |
| `anthropic` | `messages` | `litellm_anthropic_chat_completions` | exact v1 Provider mapping alias | **blocked** |
| `moonshot_kimi` | `openai_compatible_chat_completions` | `moonshot_direct_chat_completions_sse_v3` | exact v1 Provider mapping alias | offline only; no successful handshake |

Anthropic is registered so the intended native surface is explicit, but selection fails closed.
The current LiteLLM transport translates native `stop_reason` into an OpenAI-style
`finish_reason` and does not retain `stop_sequence`; therefore its output is not an observation of
the native Messages mapping. An Adapter-side pre-transformation observation layer is required
before that triple can be enabled.

Kimi transports do not share a mapping implicitly. The entry above names only the SSE v3
transport. The non-streaming JSON transport and any future transport require their own explicit
triple, provenance statement, and first-live gate.

`offline_selection_allowed` and `runtime_binding_allowed` are independent gates. All current
entries permit offline validation and prohibit runtime binding. This v2 loader hard-requires
`runtime_binding_allowed=false` and `first_live_validation_required=true` for every entry even if a
caller edits both flags together and refreshes the registry hash. Runtime promotion therefore
requires a new registry successor with new validation code and evidence; it cannot be performed by
flipping a v2 boolean.

Selection returns an immutable `VerifiedSurfaceSelection`, not a mapping dictionary. Its mapping
SHA-256 is computed from the actual frozen selected mapping, its telemetry-schema SHA-256 is
computed from the actual record-schema file, and its triple, Adapter version and output-counter
path come from the verified registry. Only a `purpose=runtime_binding` selection can mint the
private-token `VerifiedRuntimeCompletionBinding` consumed by the live T2 builder. No such selection
can succeed under v2. An offline selection may resolve fixtures but cannot create a
`live_adapter_write` record.

## DeepSeek Responses provenance

The DeepSeek `/responses` rules are derived from `probe_out_v3.json` (9,215 bytes, SHA-256
`cb3417b0f3c56eca7fd6d05dda68c4717315004ba0d6e5d6da408d52d990131d`). The probe observed only
two distinct completion shapes:

- `status=completed` with `incomplete_details=null`;
- `status=incomplete` with `incomplete_details.reason=max_output_tokens`.

The completed and length-capped fixtures are sanitized live projections. Missing and unknown
fixtures declare `source_tier=live_capture` and `fixture_kind=synthetic_mutation`; they are branch
tests, not Provider-shape attribution. Values not
observed in that probe—including `failed`, `cancelled`, `queued`, `in_progress`, content filtering,
and other incomplete reasons—remain `unmapped`. The probe used minimal direct request kwargs, not
the Agents SDK Adapter-built kwargs, so `first_live_validation_required` remains true and the
registry does not promote provenance.

The receipt does not persist its capture timestamp, authorization ID/deadline, or a machine-bound
link to the probe script. Those links remain `external_not_machine_bound`; the current script's
presence in this repository is not evidence that it generated the receipt. The v1 and v2
predecessor receipt bytes are intentionally excluded and unavailable in this narrow package; the
v3 receipt retains only the v2 SHA-256 as a non-self-contained historical note. Accordingly, this
package must not be described as a self-authenticating execution record.

`limitations.message_output_item_observed=false` and the adjacent incomplete-message field are
scoped only to the third `responses_message_stage_cap_attempt`. They do not mean that no message
item appeared anywhere in the three probes: the normal-completion probe explicitly contains a
`message/completed` item. The third attempt contains no message item, so the intended
message-stage truncation target remained unobserved.

`fixture_manifest_v2.json` commits the registry and all four fixtures by byte length and SHA-256;
it reports the evidence split as two live-capture projections and two synthetic mutations.

Every rule condition and preserved-native-value path is checked against the selected surface's
completion-discriminator allowlist. `usage`, request IDs, HTTP status, content and other fields can
never produce a `native_status` mapping result.

## Collector boundary

The fixed-count `CompletionTelemetryCollector` remains available for offline fixtures. Runtime uses
a dynamic denominator tracker bound to one verified Provider/surface/transport/Adapter identity and
a `VerifiedRuntimeDenominatorPlanBinding`. The plan fixes case IDs, per-case `max_turns`, the total
model-request cap, zero SDK/HTTP retries and `transport-response-finalization-v1`; it deliberately
contains no exact response count. The canonical plan SHA-256 must equal the externally supplied
preregistration commitment. A forged binding/plan object or an added `expected_response_count` is
rejected. No runtime tracker can be created under v2 because every v2 Provider remains blocked. It
accepts only the opaque, canonical-byte-backed
`SanitizedCompletionCapture` produced by T2's factory;
ordinary mappings cannot be appended. A future runtime integration is required to bind a case
session and make the Provider call `begin_attempt()` before any network request and exactly one
terminal method afterward. Under that contract, attempt indices count all outbound attempts and
response indices are independently allocated only when an actual Provider response was observed,
including a response whose telemetry had to be rejected.
`sdk_request_usage_index` remains nested within its SDK raw response and is never substituted for an
attempt index. Case sealing rejects pending attempts; campaign sealing does not require reaching a
turn or request ceiling and derives the observed response denominator after execution. It has no
request, prompt, message, tool-argument, raw-header, credential, path, exception-text, or
response-body input.

The runtime denominator artifact publishes `derived_after_run=true`,
`exact_response_count_preregistered=false`, all terminal-kind counts, per-case SDK reconciliation,
the derived response count, and any planned case not finalized. SDK count mismatch or unavailability,
any rejection/no-response/cancellation/outcome-unknown, a pending attempt, or a missing planned case
forbids the truncation-exclusion claim. Verification of the external preregistration source that
supplied the plan hash remains a future runner responsibility and is not claimed here.

This package does not modify a Provider Adapter, Agents SDK response, Phase 6 runner, audit ledger,
Depth-60 plan, prompt, scorer, candidate, or task selection. It performs no model or Provider call,
does not enter a run ledger, does not change the locked `20/60`, and cannot satisfy the future
new-unseen-task closing gate.
