# Pilot Staging Verification Snapshot

> Current main note (2026-08-26): candidate v5 commitment
> `105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc` and
> `pilot_pack.supervised_v6.json` are bound in `main@c65ff65c` while keeping the pilot Provider as
> DeepSeek.
> Kimi Models preflight was `implemented_offline_tested_not_run` at candidate lock, so the frozen
> candidate/pilot snapshot retains `live_models_preflight_performed=false`. After that lock, one
> separately authorized metadata GET completed at `2026-08-26T09:41:49.967Z`: HTTP 200,
> attempts/network calls `1/1`, requested/returned model `kimi-k3`, exact visibility true, zero model
> tokens and cost `null`. The receipt is not inherited by candidate v5 or pilot v6. It does not
> authorize Chat, tools, model quality, Provider registration or private evaluation. The one-time
> authorization is consumed and the request must not be retried; any new request requires fresh
> explicit authorization. No Kimi Chat/tool/model call or predecessor result exists. The successor
> candidate/pilot state itself has offline verification only. PR #21 regular-merged it to
> `main@c65ff65c`; this remains engineering evidence, not participant evidence. The exact merge and
> final main runs are recorded in the
> [`Kimi Models preflight main CI snapshot`](../../docs/evidence/kimi-models-preflight-main-ci-v1/README.md).

> Historical PR #19 note: candidate v4 commitment
> `1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7` and historical
> `pilot_pack.supervised_v5.json` regular-merged to `main@77911226`, while keeping the pilot
> Provider as DeepSeek. Main pilot run 32930473989 passed 51 offline contracts, 1 real PostgreSQL
> contract and no-Provider-secret Compose. Anthropic remained offline-contract-only; that run did
> not perform a live Models preflight, Anthropic Provider run or v5-pack participant run.

> Historical predecessor note: Completion Telemetry v2 candidate `1f6ac18e…e5ce5` 已进入
> `main@094cb9b1` 并通过无 Provider Key clean CI；它不继承 predecessor
> `7744770a…f0d11` 的任何结果。

Date: 2026-08-26 (Asia/Shanghai)

This is a local and GitHub clean-CI engineering verification snapshot. It is not an external participant
result, a production deployment attestation, a security certification or a Provider
quality rerun.

## PR #21 main integration

PR #21 已 regular merge 至
`main@c65ff65c0cbb67205956ddae991768ba9fca9293`。该精确 commit 的：

- [`offline-quality-gate` run 32957003253](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003253)
  通过 368 个根测试、candidate v5 verifier、Phase 5 50/50、evidence 21/21 与
  `phase5-ci-v1=valid`，CI `network_calls=0`；
- [`pilot-staging-ci` run 32957003191](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003191)
  通过 51 个 offline contracts、1 个真实 PostgreSQL contract、无 Provider Key Compose
  startup/teardown 与最终 gate；
- [`production-slice-e2e` run 32957003204](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003204)
  通过 18 个 contracts 与真实 PostgreSQL/MinIO/OTel E2E。

这些 runs 没有调用 Kimi。Candidate 锁定后的一次 metadata receipt 是独立 observation，
不回填 v5/v6，也不授权 Kimi Pilot、Provider 注册或模型质量声明。

## Historical PR #19 main integration

PR #19 已 regular merge 至
`main@77911226b0e2a7e7d15ac5be9c2aafc19c5ea335`。该精确 commit 的：

- [`offline-quality-gate` run 32930474006](https://github.com/cedRiC874/researchops-agent/actions/runs/32930474006)
  通过 334 个根测试、candidate v4 verifier、Phase 5 50/50、evidence 21/21 与
  `phase5-ci-v1=valid`，Anthropic preflight 保持 `not_run / network_calls=0`；
- [`pilot-staging-ci` run 32930473989](https://github.com/cedRiC874/researchops-agent/actions/runs/32930473989)
  通过 51 个 offline contracts、1 个真实 PostgreSQL contract、无 Provider Key
  Compose startup/teardown 与最终 gate；
- [`production-slice-e2e` run 32930473976](https://github.com/cedRiC874/researchops-agent/actions/runs/32930473976)
  通过 18 个 contracts 与真实 PostgreSQL/MinIO/OTel E2E；这是相邻服务回归，
  不是 Anthropic API 或模型质量成绩。

长期步骤级证据见
[`docs/evidence/anthropic-models-preflight-main-ci-v1/README.md`](../../docs/evidence/anthropic-models-preflight-main-ci-v1/README.md)。

### Historical Completion Telemetry v2 baseline

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

- Current candidate v5 status: `candidate_locked / valid`
- Current candidate v5 commitment:
  `105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc`
- Predecessor v4 commitment:
  `1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7`
- Historical predecessor v3 commitment:
  `22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9`
- Active supervised pack: `pilot_pack.supervised_v6.json`
- Historical v4-bound pack: `pilot_pack.supervised_v5.json`
- Kimi Models preflight: `implemented_offline_tested_not_run`
- Kimi live preflight recorded in candidate/pilot v6 snapshot: `false`
- Separate post-lock metadata observation: `verified / HTTP 200 / attempts 1 / network 1 /
  exact kimi-k3 visible / model tokens 0 / cost null`
- Post-lock receipt inherited or authorizing: `false`
- Kimi Chat/tool/model calls performed: `false`
- Anthropic Models preflight: `implemented_offline_tested_not_run`
- Public/pilot execution Provider: still `deepseek / deepseek-v4-flash`
- Historical v1 commitment:
  `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`
- Completion Telemetry v2 status: `candidate_locked / valid`
- Completion Telemetry v2 commitment:
  `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5`
- `prior_results_inherited`: `false`
- Paid/model network calls made by this verification: 0
- Provider API Key read, printed or stored: no

## Historical PR #19 main CI results

| Layer | Result | Network/model behavior |
| --- | ---: | --- |
| Root repository suite | 334 passed | full root suite; not 334 Anthropic- or pilot-only tests |
| Pilot offline contracts | 51 passed | in-memory/fake executor plus config/schema/PowerShell/workflow checks; no network |
| Real PostgreSQL integration | 1 passed | PostgreSQL 17.6 container; fake executor |
| Existing production slice | 18 passed | process-level test suite |
| GitHub pilot-staging CI | success | 51 offline contracts plus 1 real PostgreSQL contract; no Provider key/worker/model call |

### Historical local PostgreSQL integration detail

The earlier local PostgreSQL integration applied the real migration, completed a six-task participant
lifecycle, verified consent replay remained idempotent after campaign completion,
verified the supervised environment marker survives a differently configured reader,
enforced the supervised one-to-two participant database bound and exercised a
participant skip without counting it as a technical failure,
checked the append-only event chain and task-pack hash, ran the summary in a repeatable
read snapshot, applied the checksum-aware migration runner twice, and confirmed a
forged stored migration checksum fails closed. The temporary container used `--rm`,
created no persistent volume and was stopped after the test.

## Historical local Linux image (Completion Telemetry v2 baseline)

The predecessor snapshot's final local image built successfully and `pip check` reported no broken
requirements. This digest is not a candidate v4 or v5 image or deployment identity:

```text
researchops-pilot-staging:supervised-local
sha256:24ab5d3dfae6fdbce6f9ae7e176a106f0e09c955086d03de4f7304c879ab984b
```

The same image validated the locked candidate from `/app/core` without a Provider
secret or network call. This is a local Docker content digest, not a signed registry
artifact or deployment proof. Rebuilding or changing any copied file creates a new
digest and requires a new campaign commitment.

## Historical GitHub clean CI (PR #5 baseline)

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
