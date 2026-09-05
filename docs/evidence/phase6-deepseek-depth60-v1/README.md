# DeepSeek Phase 6 Depth-60 — sanitized online evidence

## Outcome

One precommitted, single-use run executed all 60 repository-visible development tasks exactly once.
The four historical repo-local holdout tasks were not selected or rerun.

```text
status                       completed
planned / attempted          60 / 60
completed / not started      60 / 0
passed / failed              20 / 40
success rate                 33.33%
model requests               103
input / output tokens        235,943 / 54,396
estimated cost               CNY 1.197393
actual Provider bill         unknown / null
latency P50 / P95            7.52 s / 16.77 s
runtime / harness failures   0 / 0
holdout executed             0 / 4
```

The historical development subset remained 16/16. The newly frozen 44-task extension passed 4/44.
This is the principal result: broadening the repository-visible development suite exposed a large
contract-following gap that the earlier 16-task run did not show.

## Failure denominators

Forty tasks failed at least one locked scorer check. `required_phrases` failed on 40 tasks and was
the dominant denominator. Thirty-one tasks required standalone machine-readable `ASSERT` lines;
only 2/31 of those tasks passed the complete locked task contract. Other failure units were evidence
(3), forbidden assertions (3), completion integrity (2), forbidden phrases (2), numeric claims (2),
outcome (2), and one each for arguments, evidence grounding, tool sequence and tool status.

Two tasks ended with the stable completion error `provider_output_incomplete`. They remain included
failures rather than being silently dropped; the public projection omits their task IDs.

## What remained strong

- Tool selection accuracy: 98.33%.
- Argument task accuracy: 98.33%.
- Evidence grounding accuracy: 96.30%.
- Numeric-claim task accuracy: 89.47%.
- Safety and usage-integrity accuracy: 100%.
- Approval-control failure and bypass rates: 0%.
- All 60 audit chains and all committed artifact hashes verified.
- The ephemeral frozen-input copy was removed before artifact publication.

These component metrics do not override the locked 20/60 task result.

## Usage, cost and latency

Usage coverage was 60/60 and contained 103 model requests, 235,943 input tokens and 54,396 output
tokens. The CNY 1.197393 estimate applies the frozen 2026-08-31 peak price and treats every input
token as a cache miss. It is not a Provider invoice. The local CNY 6 stop was not reached.

Nearest-rank latency was 7,516.66 ms at P50 and 16,766.23 ms at P95; maximum observed agent-segment
latency was 21,094.10 ms. This sequential development run does not establish a production SLA.

## Integrity and publication boundary

The public projection is [public_summary.json](public_summary.json). Original local artifacts are
not copied. [artifact_commitments.json](artifact_commitments.json) retains their byte sizes and
SHA-256 values, including omitted per-task JSONL, SQLite and consume/terminal receipts.

The publication excludes authorization identifiers and derived bindings, API credentials and
headers, raw Provider payloads, per-task model outputs, event payloads, local absolute paths and any
private or non-synthetic data.

## Claim boundary

This evidence is attributable to `DeepSeek + frozen ResearchOps control plane`, not to the model
alone. It covers repository-visible development tasks. It does not prove private-holdout or unknown
distribution generalization, production reliability, cross-Provider performance, a Provider billing
total, or an LLM planning accuracy rate. The result cannot be used to tune the same prompt, scorer,
tool schema or task selection and then rerun this consumed plan.
