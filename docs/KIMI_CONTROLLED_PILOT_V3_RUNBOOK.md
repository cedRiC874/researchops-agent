# Kimi controlled synthetic Pilot v3 / Candidate v8 runbook

This runbook describes the offline diagnostic successor only. It is not an authorization to call
Kimi and intentionally publishes no executable online command.

## Frozen identity

```text
candidate_id          eval-v2-public-regression-deepseek-kimi-controlled-chat-v8
candidate_commitment  b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c
predecessor            2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5
chat_contract          eval-v2-kimi-chat-completions-v3
pilot_contract         kimi-controlled-synthetic-pilot-v3
transport              moonshot_direct_chat_completions_sse_v3
artifact_namespace     artifacts/kimi_controlled_pilot_v3
authorization_schema   kimi-pilot-authorization/3.0
data                    fresh synthetic only
```

Candidate v8 does not inherit Candidate v7's consumed authorization, post-lock response-validation
failure, Provider output, token use, bill, latency, result or any compatibility/quality claim. The
prompt, scorer, tool schema, three scenarios, request bytes and response acceptance predicates remain
unchanged. The successor adds observability only.

## Sanitized response diagnostic

Only `kimi_chat_response_invalid` may carry:

```json
{
  "response_validation_diagnostic": {
    "schema_version": "kimi-response-validation-diagnostic/1.0",
    "code": "top_level_field_set_invalid"
  }
}
```

`code` is selected by a fixed local parser branch from the closed enum in the
[Chat v3 contract](../evals/v2/kimi_chat_completions_contract_v3.json). When the terminal error
remains `kimi_chat_response_invalid`, it is copied unchanged to `request_failed`, `run_terminal`,
checkpoint and receipt. If a terminal authorization/terms/pricing guard supersedes that error, the
original request-failure event remains hash-bound while the terminal/checkpoint/receipt diagnostic
is null and the guard error becomes authoritative. The verifier rejects a missing, unknown,
non-generic or conditionally mismatched diagnostic.

This is structural consistency checking, not authenticated tamper evidence. An attacker who can
rewrite all private artifacts can replace one valid enum with another and recompute the unkeyed hash
chain. Authenticated diagnostic provenance would require a repo-external HMAC/signing key or an
externally anchored chain head; neither is implemented or claimed here.

The diagnostic never stores raw headers/body, Provider request/completion/model IDs or their hashes, actual field
names or values, JSON pointers, byte/event offsets, sizes, prompt/reasoning/tool payloads, paths,
email/account/project/user identifiers, authorization values or free-text exceptions. It identifies
only the local validation branch and never authorizes a causal Provider-fault claim.

That boundary applies only to the diagnostic subobject. Private gitignored events, checkpoints and
receipts still contain authorization hashes/bindings, exact operation times, tombstone and hash-chain
integrity values. They are not public evidence and must never be copied verbatim into a PR or report.

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

The monetary reservation is a local fail-closed control, not a Provider billing guarantee.

## Offline checks

Candidate validation performs no network request:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-verify-public-freeze `
  --candidate evals/v2/public_regression_candidate_v8.json
$candidateExitCode = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH
"exit_code=$candidateExitCode"
```

The successor dry-run also performs no Key lookup or network request:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m researchops.cli kimi-controlled-synthetic-pilot-v8
$dryRunExitCode = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH
"exit_code=$dryRunExitCode"
```

Expected: exit `4`, `status=not_run`, `error_code=kimi_pilot_v8_confirmation_required`, zero
attempts/calls/model requests/tools and all authority claims false.

The Candidate v7 online command is permanently tombstoned. Its read-only artifact verifier remains
available, but neither a v7 ID nor authorization may be reused by v8. Pilot v3 checks the v1, v2 and
v3 artifact namespaces and creates a cross-version tombstone before any Key lookup.

## No current online authorization

Candidate v8 is permanently `online_not_authorized`; confirmation or a new authorization cannot
unlock it. A future call requires a successor later than v8 and all of the following as a new decision:
fresh legal and pricing attestations, a new authorization ID and expiry, exact caps, budget and
explicit one-time user authorization. At or
after `2026-08-30T16:00:00Z`, final effective-terms review is additionally required before any later
design can be eligible.

Kimi remains unregistered; Provider registration remains `1/2`, private remains `0/50`, and no model
quality, tool/usage/error compatibility, private or non-synthetic claim is allowed.
