# DeepSeek Phase 6 depth-60 runbook

Status: `locked_offline_not_run / requires_fresh_single_use_authorization`.

This run deepens the existing Phase 6 DeepSeek line instead of adding another Provider. It executes
exactly 60 repository-visible development tasks once, sequentially. The four historical repo-local
holdout tasks remain byte-for-byte unchanged and are not selected or rerun.

## Frozen identity

- Plan: `phase6-deepseek-depth60-v1`
- Commitment: `012aecfa73983e12fb24e839168b715ce86800b96958d7c244263f0ca9eee9a3`
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

Validate locally without reading a Key or making a network request:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli phase6-validate-deepseek-depth60
```

Expected terminal fields are `status=valid`, `selected_task_count=60`, `holdout_executed=false`,
`network_calls=0` and `model_calls=0`.

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

## Required authorization

The plan does not authorize itself. A fresh user authorization must state:

- the exact plan commitment above;
- one authorization ID and a UTC expiry at least 90 minutes in the future;
- permission to use the locally configured `DEEPSEEK_API_KEY` without displaying, persisting or
  logging its value;
- acceptance of every frozen request/token/time/cost limit;
- one run only, no retry, no resume and no fallback;
- results cannot tune the same prompt, scorer, tools or task selection;
- no private, non-synthetic or repo-local holdout execution.

After authorization, the controlled command is:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli phase6-run-deepseek-depth60-online `
  --output-dir artifacts/phase6_deepseek_depth60/run-01 `
  --expected-plan-commitment 012aecfa73983e12fb24e839168b715ce86800b96958d7c244263f0ca9eee9a3 `
  --authorization-id <AUTHORIZATION_ID> `
  --authorization-expires-at-utc <YYYY-MM-DDTHH:MM:SSZ> `
  --confirm-online
```

The CLI has no Key argument. Plan, exact commitment, authorization, path, component and dependency
checks precede a Key-presence readiness check; only after the exclusive receipt is created is the
Key handed to the Provider transport. A second component check occurs inside the runner after
receipt consumption. Success, failure, timeout or setup error does not authorize a second attempt.

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
