# Kimi K3 controlled synthetic handshake

Status: `implemented_offline_tested_not_run`. This is a one-scenario protocol
handshake, not a model-quality evaluation and not Provider registration.

The frozen plan is
[`evals/v2/kimi_k3_handshake_plan_v1.json`](../evals/v2/kimi_k3_handshake_plan_v1.json).
Its canonical commitment is:

```text
8979dfa8b8826afc6e9e44fadc64ed515b767a421ea122d5ffee5ac40c15e7cd
```

## What the one run does

1. Send one non-streaming `kimi-k3` request with
   `tool_choice=required` for the fixed synthetic fixture.
2. Require exactly one `lookup_synthetic_metric` call and execute it once in
   memory.
3. Replay the complete assistant message and its tool result, then require a
   second non-streaming response with `finish_reason=stop`.
4. Send the already frozen promptless invalid-request probe and require HTTP
   400 with `invalid_request_error`.
5. Stop permanently, whether the terminal result is success, failed, or
   outcome unknown. There is no retry or resume path.

The locked caps are 3 network attempts, at most 3 confirmed network calls, 2 model requests, 16,000 total input
tokens, 3,072 total output tokens, 1 tool execution, 300 seconds, concurrency
1, zero client retries, no fallback, and a CNY 2 local observed-usage cost
stop. Token and cost limits are enforced after each returned usage projection;
one in-flight response can overshoot, so none is a Provider-side billing hard
cap. The frozen local pricing projection is CNY 20 per million uncached input
tokens and CNY 100 per million output tokens. Missing usage makes cost coverage
incomplete. The projection is not a Provider invoice or billing guarantee.

The source review is bound to the first-party K3/API/terms/privacy/payment
snapshots captured on 2026-08-31. The effective terms permit Provider service
optimization use, so this plan is synthetic-only. An operator must attest that
there has been no material terms or pricing change before a run.

## Offline validation

This command performs zero network calls and does not load the Key:

```powershell
python -m researchops.kimi_k3_handshake_cli validate
```

Expected result: `status=valid` and the exact plan commitment above.
The commitment includes hashes of the parser, handshake runner, CLI, fixed
probe transport, synthetic tool, dependency lock, and project metadata.

## Required new one-time authorization

No authorization from Kimi v6, v7, or v8 carries over. Before any run, record a
new authorization with all of these exact bindings:

```text
Authorization ID: <new unique value>
Authorization expiry UTC: <at least 300 seconds and no more than 24 hours after execution time>
Expected plan commitment:
8979dfa8b8826afc6e9e44fadc64ed515b767a421ea122d5ffee5ac40c15e7cd

I authorize exactly one kimi-k3-controlled-synthetic-handshake-v1 run using
the frozen one-scenario plan and locked caps. I attest that the 2026-08-31
terms and K3 CNY 20/100 pricing have no material change. This authorizes no
retry, resume, prompt/candidate tuning, quality claim, Provider registration,
private evaluation, or non-synthetic data.
```

Do not put the Key on the command line. The CLI has no Key option. After the
Key has been configured in the process environment and a still-valid one-time
authorization exists, the authorized command is:

```powershell
python -m researchops.kimi_k3_handshake_cli run `
  --confirm-online `
  --accept-locked-caps `
  --attest-terms-and-pricing-unchanged `
  --authorization-id <NEW_AUTHORIZATION_ID> `
  --authorization-expires-at-utc <UTC_EXPIRY> `
  --expected-plan-commitment 8979dfa8b8826afc6e9e44fadc64ed515b767a421ea122d5ffee5ac40c15e7cd
```

The authorization is atomically consumed before the Key is loaded. A failed
or unknown outcome therefore cannot be rerun with the same ID.
After consumption, the effective run timeout is the smaller of 300 seconds and
the remaining absolute authorization window. Every network attempt rechecks
that expiry before incrementing counters or dispatching a request.
This is a local procedural/idempotency boundary, not a cryptographically signed
authorization capability against a malicious local code author.

## Receipts and privacy

The local files are created under `artifacts/kimi_k3_handshake_v1/`:

- `<authorization-id-sha256>.consumption.json`
- `<authorization-id-sha256>.terminal.json`

On handled exits the runner must attempt the terminal receipt. An abrupt process
crash or unrecoverable local I/O error can leave a consumption receipt without
a terminal receipt; such an orphan is `outcome_unknown` and never authorizes a
retry or resume.

The terminal receipt records only bounded attempts/calls, usage coverage,
latencies, invalid-request probe semantics, stable
error state, local cost projection, and commitments. It does not persist the
authorization ID, Key, raw prompt, output, reasoning, tool arguments, or tool
result. Actual Provider-billed cost remains `null`.
