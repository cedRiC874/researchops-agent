# Kimi controlled synthetic pilot runbook

This runbook describes the separately authorized T5 compatibility pilot. It is not authorization,
does not relax the synthetic-only boundary and must not be used to tune prompts, scorers, tool
schemas, candidate content or task selection.

## Current state

- Candidate v6 commitment:
  `57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641`.
- Runtime state: `offline_ready_not_run`.
- Kimi campaign registration: `false`; registered Providers remain `1/2`.
- Post-lock controlled-pilot observation: one consumed authorization, one request/call, failed during
  usage validation, zero completed scenarios and zero tool executions. It is not inherited into the
  candidate and cannot be retried.
- Actual billed cost: unknown and represented as `null`, never zero.
- Private/non-synthetic data: forbidden.

The sanitized post-lock record is available in the
[usage-stage failure evidence](evidence/kimi-controlled-pilot-usage-failure-v1/README.md). This
runbook describes the frozen v6 procedure for audit purposes; it must not be used to repeat that run.
A future attempt requires a versioned successor runbook and new authority.

## G3 authority required before any online command

The operator must have a new, single-run user authorization that explicitly accepts all of these
locked limits:

| Control | Locked value |
| --- | ---: |
| Synthetic scenarios | 3 |
| Model requests | at most 8 |
| Concurrency | 1 |
| Client retries | 0 |
| Input tokens | 8,000/request; 40,000 total |
| Output tokens | 1,536/request; 10,000 total |
| Tool executions | at most 6 |
| Timeout | 90 seconds/request; 600 seconds total |
| Local conservative cost hard-stop | CNY 5.00 |
| Fallback | disabled |

The authorization must include a unique ID and a timezone-aware expiry. It is single-use, cannot be
resumed or retried and must leave a complete 600-second window before every applicable expiry.

Do not paste the Key into chat, source files, command history, artifacts or authorization text. The
Key must be supplied as `MOONSHOT_API_KEY` only in the run process environment and removed after the
attempt.

## Date and source gate

Before 2026-08-31 China time, only the provisional G2a path is eligible. It expires at
`2026-08-30T16:00:00Z`; a run that cannot finish its full 600-second window before that instant is
rejected before the Key is read. At or after 2026-08-31, do not reuse G2a: complete T2b against the
actually effective first-party text and freeze a successor decision first.

Immediately before the run, an operator must capture and review these exact first-party sources:

- service agreement: `https://platform.kimi.com/docs/agreement/modeluse`;
- privacy policy: `https://platform.kimi.com/docs/agreement/userprivacy`;
- payment agreement: `https://platform.kimi.com/docs/agreement/payment`;
- Kimi K3 pricing: `https://platform.kimi.com/docs/pricing/chat-k3`.

The legal capture must yield the three canonical-text SHA-256 values required by
`kimi-terms-canonical-v1`. The pricing capture must yield the canonical source SHA-256 and byte count
required by `kimi-pricing-canonical-v1`, and the operator must confirm the locked Kimi K3 rates remain
CNY 2/20/100 per million cached-input/uncached-input/output tokens. Do not fabricate hashes or reuse a
capture older than one hour. Source-update detection is manual; the CLI validates the supplied
attestations and bindings but does not fetch those pages.

## Safe dry run

From the repository root, first run without online confirmation:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m researchops.cli kimi-controlled-synthetic-pilot
$dryRunExitCode = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH
"exit_code=$dryRunExitCode"
```

Expected result: exit `4`, `status=not_run`, zero attempts/calls/model requests/tools, null pricing and
terms attestations, null actual billed cost and every authorization claim `false`. Any other result
stops the procedure.

## Consumed v6 execution — no online command

The one v6 authorization was consumed by a fail-closed usage-stage attempt. Do **not** invoke the
current CLI with `--confirm-online`, reconstruct the former argument set or grant another capability
for the v6 contract. The current CLI remains part of the frozen implementation snapshot; its presence
is not authorization to execute it.

No executable online command is published here after G4 failure. A later attempt requires all of the
following before a replacement command may be documented:

- a versioned successor parser and explicit terminal usage contract based on first-party protocol
  evidence;
- new MockTransport tests, runtime contract and candidate commitment;
- a successor runbook and CLI gate that cannot select v6;
- a fresh legal/pricing review, Key, budget and separate one-time authorization.

## Receipt verification and interpretation

The consumed run's hash chain has already been verified offline. Its unpublished local authorization
identifier is intentionally not reproduced in this runbook. See the public evidence bundle rather
than copying the original authorization-bound artifacts.

The sanitized receipt may report request counts, observed input/output tokens, local conservative
cost, latency and tool/error classifications. It must not contain the Key, prompts, reasoning, raw
tool arguments, paths or email addresses. The fixed promptless 400 probe can still create Provider
authentication logs, rate-limit accounting or billing; public documentation does not guarantee
otherwise.

A compatible receipt supports only `compatibility_verified_only`. It is not model-quality evidence,
does not register Kimi as the second Provider and does not authorize private or non-synthetic use.
