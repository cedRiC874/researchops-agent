# DeepSeek Phase 6 depth-60 runbook

Status: `historical_run_preserved / current_tree_successor_valid_non_executable`.

This historical run deepened the existing Phase 6 DeepSeek line instead of adding another Provider.
It executed exactly 60 repository-visible development tasks once, sequentially.
The four historical repo-local holdout tasks remain byte-for-byte unchanged and are not selected or
rerun.

## Frozen identity

- Plan: `phase6-deepseek-depth60-v1`
- Commitment: `8019ef294b5028ab4e44c006f01e02bddb5a3b67b1ed88b84945bf37e75c216e`
- Provider/model alias: `deepseek / deepseek-v4-flash`
- Official version observed at lock: `DeepSeek-V4-Flash-0731`
- Transport: `openai_compatible_responses`
- Scope: `P6-DEV-001` through `P6-DEV-060`
- Historical development tasks: 16
- Newly frozen extension tasks: 44
- Holdout selected: 0/4

`deepseek-v4-flash` is a mutable Provider alias. The run records the requested alias, timestamp,
Provider trace and usage; it cannot claim binary identity with the historical 2026-08-21 run.

The plan records URL, retrieval time, decoded byte count and SHA-256 for three official pages, but
does not redistribute the third-party response bodies. Those source hashes are maintainer-side
capture metadata, not independently recomputable repository evidence; a reviewer must refetch the
current official pages or verify an externally retained capture before authorizing the run.

The historical plan and result remain immutable. The current source tree intentionally differs from
the historical v1 source bundle, so validating that plan against current code now fails closed with
`phase6_depth60_component_drift`. Validate the current-tree v2 source-integrity successor without
reading a Key or making a network request:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli phase6-validate-deepseek-depth60 `
  --plan evals/phase6_deepseek_depth60_plan_v2.json
```

Expected terminal fields are `status=valid`, `source_bundle_algorithm=v2`,
`supersedes_plan_id=phase6-deepseek-depth60-v1`, `online_execution_authorized=false`,
`network_calls=0` and `model_calls=0`. The successor is a current-tree integrity commitment only;
it does not revalidate the historical result and cannot be used as a runtime binding.
Here, current-tree integrity is limited to the enumerated source closure and component hashes in the
successor plan; it is not a commitment to every file in the repository.

## Frozen local stops and request bounds

| Limit | Frozen value |
| --- | ---: |
| Development tasks | 60 |
| Repetitions | 1 |
| Concurrency | 1 |
| Client retries / resume | 0 / false |
| Max turns per task | 8 |
| Max output per model response | 2,000 tokens |
| Case timeout | 120 seconds |
| Total timeout | 5,400 seconds |
| Total requests | 450 |
| Total input tokens | 750,000 |
| Total output tokens | 350,000 |
| Local conservative observed-cost stop | CNY 6.00 |

Cost uses the 2026-08-31 official peak price with every input token treated as a cache miss:
CNY 3/M input and CNY 9/M output. Enforcement is a pre-case reserve plus a post-case observed stop;
one in-flight Agent case can overshoot the local totals before usage is returned. It is not a strict
Provider billing hard cap, and the actual Provider bill remains unknown unless separately reconciled.

## Historical authorization procedure — not currently executable

The procedure below records how the consumed historical v1 run was authorized. It is not a current
run instruction: the current tree rejects v1 for component drift and rejects v2 as
`phase6_depth60_successor_plan_not_executable`. There is currently no executable Depth-60 plan.

The plan does not authorize itself. A fresh user authorization must state:

- the exact plan commitment above;
- one authorization ID and a UTC expiry at least 90 minutes in the future;
- permission to use the locally configured `DEEPSEEK_API_KEY` without displaying, persisting or
  logging its value;
- acceptance of every frozen request/token/time/cost limit;
- one run only, no retry, no resume and no fallback;
- results cannot tune the same prompt, scorer, tools or task selection;
- no private, non-synthetic or repo-local holdout execution.

At the time of the historical run, the controlled command was:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli phase6-run-deepseek-depth60-online `
  --output-dir artifacts/phase6_deepseek_depth60/run-01 `
  --expected-plan-commitment 8019ef294b5028ab4e44c006f01e02bddb5a3b67b1ed88b84945bf37e75c216e `
  --authorization-id <AUTHORIZATION_ID> `
  --authorization-expires-at-utc <YYYY-MM-DDTHH:MM:SSZ> `
  --confirm-online
```

The CLI has no Key argument. Plan, exact commitment, authorization, path, component and dependency
checks precede a Key-presence readiness check; only after the exclusive receipt is created is the
Key handed to the Provider transport. A second component check occurs inside the runner after
receipt consumption. Success, failure, timeout or setup error does not authorize a second attempt.

The historical v1 bound Python source is the deterministic transitive local-import closure of the
Depth-60 CLI, runner, scorer, tool runtime and freeze gates. The v2 successor retains that closure
model while adding domain separation, package-initializer coverage and fail-closed relative-import
escape handling. A reachable source edit or new reachable import changes the applicable
commitment; merely adding an unimported Provider successor does not. The historical artifact
manifest reports both its bound v1 bundle hash and the observational full source-tree hash.

The consume receipt is immutable and a separate terminal receipt binds the outcome and final
report/manifest hashes. This is a local single-worktree control, not a globally immutable external
anchor; deleting local receipts or moving to another clone would defeat it and is outside the
authorized procedure.

## Report contract

The sanitized output records:

- planned, attempted, completed, passed, failed and not-started denominators;
- overall and tag/category breakdowns;
- P50/P95 latency for attempted and completed cases;
- request, input-token and output-token totals plus usage coverage;
- conservative CNY cost, cost coverage and `actual_provider_bill=null`;
- completion, runner, tool, safety, approval and audit-chain failures;
- source/corpus/split/runtime hashes and the single-use plan receipt.

The result is attributable only to `DeepSeek + frozen prompt/tools/scorer/control plane` on visible
development tasks. It is not a private holdout, unknown-distribution generalization, production SLA,
cross-Provider comparison or model-only quality result.
