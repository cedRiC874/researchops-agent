# Completion Telemetry v2 RFC

Status: offline implementation candidate

Date: 2026-08-23

Scope: Eval v2 runner and supervised pilot staging

## 1. Decision

Completion Telemetry v2 adds one nullable, allowlisted diagnostic field:

```text
completion_failure_source
```

It explains which locally observable completion branch produced a stable top-level
failure code. It does **not** establish the Provider's internal or causal root cause.
The implementation must not retain Provider response bodies, output bodies, raw
status values, incomplete-detail objects, credentials, or direct identifiers.

The normative machine-readable contract is
`evals/v2/completion_telemetry_contract.json`.

## 2. Stable values and mapping

| `completion_failure_source` | Required `error_code` | Meaning |
| --- | --- | --- |
| `final_output_missing` | `provider_output_incomplete` | The normalized result has no non-whitespace final output. |
| `response_output_item_incomplete` | `provider_output_incomplete` | A response or output item reports an incomplete status. |
| `response_not_completed` | `provider_output_not_completed` | A response or output item reports another non-completed status. |
| `output_limit_suspected` | `output_limit_suspected` | Observed output tokens reach the configured limit. |

When more than one signal is present, classification is deterministic:

1. `final_output_missing`;
2. `response_output_item_incomplete`;
3. `response_not_completed`;
4. `output_limit_suspected`.

All non-null values require `outcome=controlled_failure` and
`completion_status=output_truncated`. Unknown values and mismatched source/error
pairs fail closed.

## 3. Null and coverage semantics

`null` has two deliberately different interpretations:

- for a completion failure, it means a legacy or otherwise unobserved source;
- for every other result, it means not applicable.

Therefore summaries report an applicable denominator, observed count, unknown
count, coverage rate, and deterministically ordered source counts. Missing legacy
data is never converted to zero failures and is never inferred from `error_code`.
The existing tool-call and failure-reason counters retain their v1 meanings.

## 4. Artifact and event compatibility

Old Eval v2 reports and public-regression artifacts remain historical, readable
evidence. They are not rewritten or backfilled. New reports carry the v2 contract
marker and may aggregate legacy records only when unknown coverage remains visible.

The pilot append-only event chain keeps the original five-field
`execution_telemetry_sha256` calculation byte-for-byte. New terminal events and
retention tombstones additionally carry a versioned v2 digest that binds the same
five fields plus `completion_failure_source`. A missing version is interpreted as
legacy v1. Unknown versions, incomplete v2 digest envelopes, markerless non-null
sources, and digest/source mismatches are invalid.

Retention after an upgrade may legitimately create a v2 tombstone for an older v1
terminal event. In that case the v1 digest remains authoritative for the terminal,
the source remains unknown, and no historical event is mutated.

## 5. Candidate and evidence boundary

Root executor and runner source files are part of the frozen public-regression
candidate hash scope. The v1 candidate commitment
`7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`
and its existing evidence therefore remain historical and unchanged.

The offline v2 implementation receives a new candidate manifest and commitment.
No v1 public-regression result, pilot result, pass rate, or model-quality claim is
inherited. This RFC authorizes no private-holdout access and no online or paid run.

The locked offline candidate is `evals/v2/public_regression_candidate_v2.json`,
commitment `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5`.
This v2 artifact remains immutable historical lineage. Its offline-only successor is
`public_regression_candidate_v3.json` (`22c985e9…b2a9`), which adds the Anthropic
offline adapter contract without inheriting v2 results or registering Anthropic.

## 6. Verification requirements

Offline tests must cover:

- all four sources, mapping validation, and precedence;
- legacy-null aggregation without false zeroes;
- sanitized per-case checkpoints and atomic/hash-chain behavior;
- pilot domain, in-memory, PostgreSQL migration, summary, and JSON schema paths;
- v1/v2 terminal-event and retention-tombstone compatibility;
- tamper detection for the new source and v2 digest;
- candidate component hashes and the explicit non-inheritance boundary.

Online Provider calls and paid public regression are explicitly outside this
implementation step.
