# Pilot Staging Verification Snapshot

Date: 2026-08-23 (Asia/Shanghai)

This is a local engineering verification snapshot. It is not an external participant
result, a production deployment attestation, a security certification or a Provider
quality rerun.

## Frozen boundary

- Eval v2 candidate status: `valid`
- Candidate commitment:
  `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`
- `src/researchops` modified by this work: no
- Paid/model network calls made by this verification: 0
- Provider API Key read, printed or stored: no

## Test results

| Layer | Result | Network/model behavior |
| --- | ---: | --- |
| Pilot API/domain/config/scripts/CI | 38 passed | in-memory store, fake executor and static PowerShell/workflow checks; no network |
| Locked candidate + JSON Schema | 3 passed | local file/hash validation only |
| Real PostgreSQL integration | 1 passed | local PostgreSQL 17.6 container; fake executor |
| Existing repository suite | 246 passed | existing offline suite |
| Existing production slice | 18 passed | process-level test suite |

The PostgreSQL integration applied the real migration, completed a six-task participant
lifecycle, verified consent replay remained idempotent after campaign completion,
verified the supervised environment marker survives a differently configured reader,
enforced the supervised one-to-two participant database bound and exercised a
participant skip without counting it as a technical failure,
checked the append-only event chain and task-pack hash, ran the summary in a repeatable
read snapshot, applied the checksum-aware migration runner twice, and confirmed a
forged stored migration checksum fails closed. The temporary container used `--rm`,
created no persistent volume and was stopped after the test.

## Linux image

The final local image built successfully and `pip check` reported no broken
requirements:

```text
researchops-pilot-staging:supervised-local
sha256:d587ba672dc7faad8b7f735d85719811b9f532d4a4a7b3fc4f965cff636472f5
```

The same image validated the locked candidate from `/app/core` without a Provider
secret or network call. This is a local Docker content digest, not a signed registry
artifact or deployment proof. Rebuilding or changing any copied file creates a new
digest and requires a new campaign commitment.

## Remaining launch gate

The no-Provider-key Linux workflow is implemented. A GitHub clean run for the exact
commit is required before calling it remote CI evidence; the workflow file and local
workflow/schema/PowerShell contracts alone are not a remote passing result.

The service is deliberately bound to loopback in Compose. A 1–2 person supervised
pretest may use the fail-closed Tailscale HTTPS scripts, but it remains permanently
ineligible for an external-validation claim. Before the formal 3–5 person campaign,
the operator must provide and verify managed PostgreSQL
TLS plus backup/PITR, secret management and rotation, immutable image/deployment
identity, redacted telemetry/alerts, an external daily retention schedule, rollback,
incident contact and any applicable ethics/IRB determination.

Until then, `external_validation_claim_allowed` must remain false and no external pilot
result may be claimed. The formal path also remains blocked by
`operator_eligibility_adjudication_not_implemented` until a trusted pseudonymous
operator review receipt is implemented.
