# Pilot Staging Verification Snapshot

> Successor note (2026-08-25): branch candidate v3 commitment
> `22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9` migrates the live preflight to
> `pilot_pack.supervised_v4.json` while keeping the pilot Provider as DeepSeek.
> Anthropic remains offline-contract-only; no v3 Provider or participant run is claimed.

> Current note: Completion Telemetry v2 candidate `1f6ac18e…e5ce5` 已进入
> `main@094cb9b1` 并通过无 Provider Key clean CI；它不继承 predecessor
> `7744770a…f0d11` 的任何结果。

Date: 2026-08-23 (Asia/Shanghai)

This is a local and GitHub clean-CI engineering verification snapshot. It is not an external participant
result, a production deployment attestation, a security certification or a Provider
quality rerun.

## Current main integration

PR #11 已 regular merge 至
`main@094cb9b173e5d153f1aff9db2ce8a25e50a57f7d`。该精确 commit 的：

- [`offline-quality-gate` run 32648925769](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925769)
  通过 258 个根测试、Phase 5 50/50、evidence 21/21 与
  `phase5-ci-v1=valid`；
- [`pilot-staging-ci` run 32648925679](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925679)
  通过 51 个 offline contracts、1 个真实 PostgreSQL contract、无 Provider Key
  Compose startup/teardown 与最终 gate；
- [`production-slice-e2e` run 32648925726](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925726)
  通过 18 个 contracts 与真实 PostgreSQL/MinIO/OTel E2E；这是相邻服务回归，
  不是 telemetry 或模型质量成绩。

长期步骤级证据见
[`docs/evidence/completion-telemetry-v2-main-ci-v1/README.md`](../../docs/evidence/completion-telemetry-v2-main-ci-v1/README.md)。
下方 PR #5 数字保留为早期历史基线，不是当前 main 的最新计数。

## Frozen boundary

- Historical v1 commitment:
  `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`
- Completion Telemetry v2 status: `candidate_locked / valid`
- Completion Telemetry v2 commitment:
  `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5`
- `prior_results_inherited`: `false`
- Paid/model network calls made by this verification: 0
- Provider API Key read, printed or stored: no

## Test results

| Layer | Result | Network/model behavior |
| --- | ---: | --- |
| Root repository suite | 258 passed | full root suite; not 258 telemetry-only tests |
| Pilot offline contracts | 51 passed | in-memory/fake executor plus config/schema/PowerShell/workflow checks; no network |
| Real PostgreSQL integration | 1 passed | local PostgreSQL 17.6 container; fake executor |
| Existing production slice | 18 passed | process-level test suite |
| GitHub pilot-staging CI | success | 51 offline contracts plus 1 real PostgreSQL contract; no Provider key/worker/model call |

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
sha256:24ab5d3dfae6fdbce6f9ae7e176a106f0e09c955086d03de4f7304c879ab984b
```

The same image validated the locked candidate from `/app/core` without a Provider
secret or network call. This is a local Docker content digest, not a signed registry
artifact or deployment proof. Rebuilding or changing any copied file creates a new
digest and requires a new campaign commitment.

## GitHub clean CI

PR #5 was regular-merged as
`a20fdfd8ff6a2e4e29881aa6693589655e307e72`. The exact `main` commit passed
[`pilot-staging-ci` run 32585792915](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792915):
three non-Provider ephemeral secrets, three synthetic registry entries, 41 offline
contracts, one real PostgreSQL contract, offline API Compose startup, teardown and the
final fail-closed gate all succeeded. Bootstrap reported
`provider_secret_created=false` and `secret_values_printed=false`; the workflow did not
start the online worker or make a model call. The durable claim boundary is recorded in
[`docs/evidence/pilot-staging-linux-ci-main-v1/README.md`](../../docs/evidence/pilot-staging-linux-ci-main-v1/README.md).

## Remaining launch gate

The service is deliberately bound to loopback in Compose. A 1–2 person supervised
pretest may use the fail-closed Tailscale HTTPS scripts, but it remains permanently
ineligible for an external-validation claim. Before the formal 3–5 person campaign,
the operator must provide and verify managed PostgreSQL
TLS plus backup/PITR, secret management and rotation, immutable image/deployment
identity, redacted telemetry/alerts, an external daily retention schedule, rollback,
incident contact and any applicable ethics/IRB determination.

Until then, `external_validation_claim_allowed` must remain false. A supervised
same-participant UX regression has completed and is documented, but it is permanently
ineligible for an external-validation claim and is not an independent second participant.
The formal path also remains blocked by
`operator_eligibility_adjudication_not_implemented` until a trusted pseudonymous
operator review receipt is implemented.
