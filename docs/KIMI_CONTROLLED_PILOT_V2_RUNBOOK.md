# Kimi controlled synthetic pilot v2 / Candidate v7 runbook

This is the frozen historical procedure used after the consumed Candidate v6 attempt. Candidate v7's
single authorization has now also been consumed by a fail-closed response-validation attempt. This
runbook is retained for audit only and must not be used for another online call.

## Frozen successor identity

```text
candidate_id          eval-v2-public-regression-deepseek-kimi-controlled-chat-v7
candidate_commitment  2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5
predecessor            57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641
chat_contract          eval-v2-kimi-chat-completions-v2
pilot_contract         kimi-controlled-synthetic-pilot-v2
provider               moonshot_kimi
model                  kimi-k3
origin                 https://api.moonshot.cn
transport              moonshot_direct_chat_completions_sse_v2
reasoning_effort       low
data                    fresh synthetic only
```

Candidate v7 does not inherit the v6 post-lock failure, authorization, Provider output, usage, cost,
result or any quality claim. The v1 prompt, scenario selection and tool schema remain unchanged; only
the versioned wire parser/contract and authorization binding are successors.

After Candidate v7 was locked, one separately authorized attempt stopped on the first required-tool
request with local stable code `kimi_chat_response_invalid`: one request/call, zero completed scenarios,
zero trusted tool calls and zero tool executions. No usage record passed validation; actual tokens,
billing and Provider latency remain unknown. G4 is `planned_not_registered`, and the authorization
cannot be retried. See the
[sanitized v7 failure evidence](evidence/kimi-controlled-pilot-v2-response-failure-v1/README.md).

Candidate v7 is unrelated to the historical supervised `pilot_pack.supervised_v7.json`; its Pack8
successor is also retained only as a historical artifact. Active Pilot Staging remains configured to
Candidate v5 / Pack6 and fails closed until a current source-bound successor exists.

## Parser v2 terminal usage contract

The final non-empty `choices[0]` event must contain `finish_reason` and at least one documented usage
projection before exactly one `[DONE]` marker:

- top-level `usage` only;
- `choices[0].usage` only;
- both, with identical prompt/completion/total values and compatible optional cache reporting.

`prompt_tokens`, `completion_tokens` and `total_tokens` are required and must reconcile. The optional
`cached_tokens` field is accepted from either location; if both locations report it, values must match.
If neither reports cache usage, the public projection is `null` and all input is costed conservatively
as uncached. The parser rejects empty-choices usage trailers, unknown usage fields, duplicate terminal
usage/finish/DONE, terminal-after-data and incomplete tool fragments.

The contract binds three first-party source captures:

- [Chat API](https://platform.kimi.com/docs/api/chat)
- [OpenAI migration guide](https://platform.kimi.com/docs/guide/migrating-from-openai-to-kimi)
- [Streaming guide](https://platform.kimi.com/docs/guide/utilize-the-streaming-output-feature-of-kimi-api)

This is offline protocol evidence, not online compatibility evidence.

## Locked caps

| Control | Value |
| --- | ---: |
| Synthetic scenarios | 3 |
| Model requests | at most 8 |
| Concurrency | 1 |
| Client retries | 0 |
| Input tokens | 8,000/request; 40,000 total |
| Output tokens | 1,536/request; 10,000 total |
| Tool executions | at most 6 |
| Timeout | 90 seconds/request; 600 seconds/run |
| Local conservative reservation limit | CNY 5.00 |
| Fallback / resume | disabled / disabled |

The local monetary control is not a Provider-side billing guarantee. Actual billed cost remains
`null/unknown` without external billing reconciliation.

## Permanent v6 tombstone and safe dry-run

The old command is permanently disabled, even if old online arguments are supplied:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m researchops.cli kimi-controlled-synthetic-pilot
$v6ExitCode = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH
"exit_code=$v6ExitCode"
```

Expected: exit `4`, `status=not_run`,
`error_code=kimi_pilot_v6_online_permanently_disabled`, zero calls and no Key lookup.

Candidate v7 dry-run:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m researchops.cli kimi-controlled-synthetic-pilot-v7
$v7ExitCode = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH
"exit_code=$v7ExitCode"
```

Expected: exit `4`, `status=not_run`, `error_code=kimi_pilot_v7_confirmation_required`, zero
attempts/calls/model requests/tools, null attestations/cost and all authority claims false.

## Consumed v7 execution — no online command

Do not invoke the Candidate v7 CLI with online confirmation, reconstruct the former argument set or
grant another capability for the v7 contracts. The CLI and code remain frozen implementation
artifacts; their presence is not authorization.

No executable online command is published after G4 failure. A future attempt requires a separately
versioned parser/contract/candidate/runbook/CLI gate, a new commitment and an entirely new
authorization. The v6 and v7 authorizations and IDs must never be reused. At or after
`2026-08-30T16:00:00Z`, T2b is additionally required before any later design can become eligible.

## Offline receipt verification

The consumed run has already passed offline verification. Its unpublished local authorization ID is
not reproduced here; use the public evidence projection rather than copying authorization-bound raw
artifacts.

Artifacts use the isolated `artifacts/kimi_controlled_pilot_v2` namespace and bind the runtime
Candidate v7 commitment, predecessor v6 commitment, Chat v2 hash and Pilot v2 hash. A v1 artifact is
invalid in the v2 verifier and vice versa.

The observed receipt did not reach `compatibility_verified_only`. It cannot establish model quality,
register Kimi, authorize private/non-synthetic data or change the full Eval v2 campaign.
