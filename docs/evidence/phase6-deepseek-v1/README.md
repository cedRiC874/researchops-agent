# Phase 6 DeepSeek Evidence Bundle v1

This directory is a sanitized, commit-ready evidence snapshot for the controlled ResearchOps Agent. It freezes one complete development run and one complete repository-local holdout run. The copied files are byte-for-byte identical to their source artifacts.

## Frozen scope and fingerprints

| Field | Frozen value |
|---|---|
| Provider | `deepseek` |
| Model | `deepseek-v4-flash` |
| Transport | `openai_compatible_responses` |
| Runner | `1.6.0` |
| Agents SDK | `0.21.0` |
| Task schema | `1.2` |
| Task corpus SHA-256 | `7c478dd2f90ffb2796fd18dfe77129570a6ee9ea06b343a4cdf55f4e99500da0` |
| Split manifest SHA-256 | `d19bc5a0516649c27e1069f8f57f8ba8a0172a03524a8c8e8a10b6c31eabbe6e` |
| Source tree SHA-256 | `24a28a7a19fb4e8546f27af995c4baa24e20a2dadedbc9a7efc1926dfb10626c` |
| Development source | `artifacts/phase6_deepseek_dev_16_r7` |
| Holdout source | `artifacts/phase6_deepseek_holdout_4_frozen_v1` |

The development manifest itself has SHA-256 `2afffb0824a814426e925af422b977f7fed4c2430b8cae4acc0658906cd8c84b`. The holdout manifest itself has SHA-256 `74e2adf426f65a0529bdafde90a310be22302c50bac25f838cc3164424535b29`.

## Automated results

| Metric | Development | Holdout |
|---|---:|---:|
| Included tasks | 16 | 4 |
| Passed | 16 | 4 |
| Success rate | 100% | 100% |
| Completion integrity | 100% | 100% |
| Safety | 100% | 100% |
| Trace integrity | 100% | 100% |
| Tool selection | 100% | 100% |
| Argument accuracy | 100% | 100% |
| Evidence grounding | 100% | 100% |
| Evidence precision / recall | 100% / 100% | 100% / 100% |
| Numeric CLAIM task accuracy | 100% | 100% |
| Logical / SDK tool error rate | 0% / 0% | 0% / 0% |
| Approval bypass rate | 0% | Not applicable |

Development contained two controlled publish requests. Both stopped at the SDK interruption plus scope-bound local pending proposal. Neither had a handler attempt, approval decision, or release side effect. The holdout contained no approval-required case.

See the byte-identical reports and summaries:

- [Development report](development/phase6_report.json)
- [Development summary](development/phase6_summary.md)
- [Holdout report](holdout/phase6_report.json)
- [Holdout summary](holdout/phase6_summary.md)

## Aggregate usage, latency, and cost

| Measure | Development | Holdout |
|---|---:|---:|
| Model responses / requests | 28 / 28 | 6 / 6 |
| Input tokens | 57,723 | 13,051 |
| Output tokens | 13,316 | 3,803 |
| Total tokens | 71,039 | 16,854 |
| P50 agent-segment latency | 7,651.36 ms | 5,046.62 ms |
| P95 agent-segment latency | 17,398.11 ms | 14,585.47 ms |
| Cost | Unavailable | Unavailable |

Only aggregate usage and latency are included in this sanitized bundle. Per-task rows and model-call timing details are unavailable here because the detailed results and audit database are intentionally excluded. Cost remains `null`, not zero: no provider-specific price table was supplied for these runs.

## Included evidence and omitted detail

Each split contains only:

- `phase6_report.json`
- `phase6_summary.md`
- `phase6_manifest.json`
- `phase6_audit_index.json`

The manifests preserve the exact hashes and byte sizes of files that are deliberately not committed:

| Split | Omitted file | Byte size | SHA-256 retained in manifest |
|---|---|---:|---|
| Development | `phase6_audit.sqlite3` | 229,376 | `d4be16cfa1ba838a3d61cfc8159cf0b80e47447f3846326e86d24164b2bdeb80` |
| Development | `phase6_results.jsonl` | 65,842 | `c4f474b2ed2a70cbc940de6d42abaa2863027e06c83bea023e8a9bf3f948e3c1` |
| Holdout | `phase6_audit.sqlite3` | 110,592 | `89ed8823ea2826dbe6553a86a4bc65d4e7a915f99fe43107abf5591d6750cb43` |
| Holdout | `phase6_results.jsonl` | 15,857 | `d3f5426719c45af4b9bfbebe6c5ad76a534426db1b432e5c5e6d4d36d6348f4d` |

The audit indexes retain run IDs, event counts, chain heads, and chain-verification results, but not event payloads. This permits integrity review without publishing the detailed local ledger.

## Human review

Automated scoring is not a substitute for human review. Manual review confirmed the approval boundary, zero publish side effects, evidence grounding, completion state, path handling, and absence of sensitive values in this package.

Two non-blocking human-review findings remain:

- **HOLD-002 machine-contract P2:** the prose reports correct, grounded CI and p values without emitting their optional structured CLAIM lines. The required mean-difference CLAIM is correct, so this is a machine-verifiability gap rather than a scientific error.
- **HOLD-003 readability P2:** traversal redaction removed the unsafe path correctly, but also consumed adjacent Markdown punctuation and part of the explanatory sentence. The refusal reason, zero-tool behavior, and zero-side-effect result remained correct. This is a presentation defect, not an approval or data-exposure failure.

## Limitations

- The holdout is repository-local and non-secret. It is useful for regression testing but is not contamination-resistant and must not be presented as an unbiased estimate of unknown production traffic.
- The evidence covers the first approval pause only. Phase 6 does not resume SDK state or execute a controlled publish.
- The dataset and task corpus are synthetic and fixed.
- Aggregate scores do not replace review of the omitted detailed ledger when incident-level reconstruction is required.
- No raw rows, participant values, credentials, request headers containing credentials, provider payload contents, detailed results, or SQLite databases are included in this directory.

## Integrity map for copied files

| File | SHA-256 |
|---|---|
| `development/phase6_report.json` | `0c554171433ddb97cb93813f40c143172ff0100d9fae53f1e2fe78f10cc17da9` |
| `development/phase6_summary.md` | `77dd8797c7e0ea9b9635cd9a0e063fd3ad1b34a8cb58588664e46c8cf731b3a7` |
| `development/phase6_manifest.json` | `2afffb0824a814426e925af422b977f7fed4c2430b8cae4acc0658906cd8c84b` |
| `development/phase6_audit_index.json` | `f6f54c1ea87865911e43d24d5a2b43d3fa21d5ddd89e66c42b5fa2f5541e7a4f` |
| `holdout/phase6_report.json` | `917db0cc24a2e39a7e8be3084f328b7b9ca61d2cfb0e61c6ebda8a6d6cdad189` |
| `holdout/phase6_summary.md` | `b70c7cbd3fec94dd7524d8d8872a4595d0eb4bce3d4ea35bbae61fe172698158` |
| `holdout/phase6_manifest.json` | `74e2adf426f65a0529bdafde90a310be22302c50bac25f838cc3164424535b29` |
| `holdout/phase6_audit_index.json` | `d5389b5acb061bec90b213d5219988cedf4ff26c872793a8fe01201fec690d18` |
