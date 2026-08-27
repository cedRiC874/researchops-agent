# Kimi controlled Pilot v6/v7 history — main CI v1

本页固化 PR #23、`main@27fad953`、Candidate v6/v7、Pack7/8、两次独立 post-lock
failure evidence，以及同一 regular merge commit 的三条最终 main checks。它是长期工程状态
快照，不是新的 Provider observation、compatibility 结果、模型成绩或在线授权。

## Merge provenance

| 字段 | 值 |
| --- | --- |
| PR #23 | [feat(eval-v2): archive Kimi controlled pilot v6/v7 history](https://github.com/cedRiC874/researchops-agent/pull/23)，regular merge |
| Base commit | `da17e6b81d8f48f2ae72c7c886236ca358e3b45c` |
| Final reviewed PR head | `9ee1953fe5569137be18c9af933d98a05ed686ce` |
| PR #23 merge commit / main at snapshot | `27fad953436fd88987d21fcafc1de776f16ab78f` |
| Merge parents | `da17e6b81d8f48f2ae72c7c886236ca358e3b45c`、`9ee1953fe5569137be18c9af933d98a05ed686ce` |
| Base tree | `283a1ec0d5fd9e5a2f8a0561283ed84a4fd54560` |
| PR-head / merge tree | `338dbcc0a798ceccae6e01a616be8f2cddfbd3f1`，完全相同 |
| Merge time | `2026-08-27T16:19:51Z`（Asia/Shanghai：`2026-08-28T00:19:51+08:00`） |
| Provider calls in PR/main CI | `0`；全部 Candidate、tombstone、environment 与 private gates 均为离线验证 |

Merge commit 保留 base 与最终 PR head 两个 parent；reviewed head 和 merge tree 完全相同，
因此没有文件内容漂移，也不能误写成 fast-forward、rebase 或 squash merge。

## Main checks

`main@27fad953` 精确有三条 push workflow runs，均为 `completed / success`：

- [`offline-quality-gate` 33092660167](https://github.com/cedRiC874/researchops-agent/actions/runs/33092660167)，
  job `98589331666`：534 项 root tests 通过。Candidate v7 verifier 返回
  `valid / historical_snapshot_only=true / network_calls=0`；独立 dependency
  environment gate 验证 82 个 exact pins、0 mismatch、`candidate_verified=false`；v6/v7
  online CLI 均保持永久 tombstone。Private status 固定包含
  `historical_candidate_execution_forbidden`，private request/access 均为 false。Phase 5
  `offline_deterministic / components_and_control_plane` 为 50/50、evidence 21/21、
  `phase5-ci-v1=valid`、model calls 0。
- [`pilot-staging-ci` 33092660220](https://github.com/cedRiC874/researchops-agent/actions/runs/33092660220)，
  job `98589332120`：54 项 offline contracts、1 项真实 PostgreSQL migration/lifecycle
  contract、无 Provider secret 的 Compose config/startup/teardown/final gate 均通过；
  `provider_secret_created=false`、`secret_values_printed=false`。Candidate v5 / Pack6 仍为
  active 配置，当前 source drift 使 online worker 在 Provider 构造前 fail-closed；offline API
  不把启动成功冒充 Candidate 执行授权。
- [`production-slice-e2e` 33092660267](https://github.com/cedRiC874/researchops-agent/actions/runs/33092660267)，
  job `98589332288`：18 项 service contracts 与真实 PostgreSQL/MinIO/OTel Compose E2E
  通过；最终 `status=passed`、`terminal_status=succeeded`、
  `secret_values_printed=false`。

这些 checks 证明 clean checkout 的离线合同、真实 PostgreSQL 集成与相邻生产切片回归通过。
Phase 5 的 50/50 不是 LLM 规划准确率；Pilot/production checks 也不是 Kimi Chat、usage、
tool、error-semantics 或模型质量证据。

## Candidate and Pack boundary

| 字段 | Candidate v6 / Pack7 | Candidate v7 / Pack8 |
| --- | --- | --- |
| Candidate commitment | `57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641` | `2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5` |
| Candidate status | historical pre-call snapshot | historical pre-call snapshot |
| Post-lock observation | first request failed during usage validation | first required-tool request failed during local response validation |
| Model requests / network calls | `1 / 1` | `1 / 1` |
| Scenarios completed | `0 / 3` | `0 / 3` |
| Trusted Provider tools / executions | `0 / 0` | `0 / 0` |
| Actual tokens / bill / Provider latency | unknown / unknown / unknown | unknown / unknown / unknown |
| Result inherited by Candidate or Pack | false | false |
| Authorization reusable | false | false |
| Compatibility or quality claim allowed | false | false |

Kimi 仍未注册，正式 Provider 仍为 `1/2`；Private Holdout 仍为 `0/50 / not_authorized`，
non-synthetic release 固定拒绝。Public/Pilot execution Provider 仍为 DeepSeek；active Pilot
配置仍为 Candidate v5 / Pack6。Candidate v6/v7 与 Pack7/8 仅为 historical artifacts。

## Immutable identities

| 文件 | Git blob | File SHA-256 |
| --- | --- | --- |
| `evals/v2/public_regression_candidate_v6.json` | `ce6700d25c69367a5e21f8eb158064da40b8b118` | `a6b91f68eda6aee4f435ab091e81034ed93971f4358675945d22d5b70daba657` |
| `evals/v2/public_regression_candidate_v7.json` | `1d31bcfc135834b18d02e1797e408f539883e55a` | `efc6ca2bbd97abe6c659983a386784938a74614cd1028861c25e20478ce7b278` |
| `services/pilot_staging/content/pilot_pack.supervised_v7.json` | `2dcdef2da494ab8ba9883834ac9302374f562eba` | `636701f8038c48a8c89b2bf024eb579b1bb7a730e8c5d5afaf38457d64756316` |
| `services/pilot_staging/content/pilot_pack.supervised_v7.review.json` | `f62a4e32c74b0acdd0902816a47bab2c36e0d095` | `e7bf9f76faaffacc34e573fd6ebff0de18250b2d9c97666773a4cefdcc9f5106` |
| `services/pilot_staging/content/pilot_pack.supervised_v8.json` | `6b508dae070b7c957558465c1ab69772c00b7560` | `e5c2c895ea838753356360041be28701f41a74b057d9782b2ef8c7c74d721100` |
| `services/pilot_staging/content/pilot_pack.supervised_v8.review.json` | `67c6e617b08802bd81fefbbf67f6e7910ce31c26` | `45df91122f06bc0e538082b58b47dadcd4cd0bd71fdc0afea54d461165c39ee7` |
| `docs/evidence/kimi-historical-status-overlays-v1/pilot_pack_v7_v8_post_lock_status.json` | `329d0c3eacbd7b0584f52ff8e02b923572aa6862` | `99c5cd3a2a88783b4c108db37c3c4baf85640acf5654fbe5d5360dd4e830d32c` |
| `docs/evidence/kimi-historical-status-overlays-v1/kimi_v1_chain_linkability_disclosure.json` | `8aee06225d3024287f9002913b27679ded19ae00` | `d47e188140ea94932580fd77866498353bdd180165c29068f8e7114b3140a605` |

Versioned overlays 绑定 Pack/review/predecessor hashes、task commitment 与两次公开 failure
projection；它们明确 `occurred=true`，同时把 Candidate/Pack inheritance、retry、Provider
registration、quality、external validation、private 和 non-synthetic authorization 固定为 false。
V1 chain head 只作为刻意可关联的 opaque commitment 披露，不是授权 ID/hash/binding。

## Publication and privacy boundary

本页不包含 API Key、Authorization/Bearer、raw Provider header/body、prompt、reasoning、tool
arguments/results、request/completion ID 或 hash、authorization ID/hash/binding、用户邮箱、本机
绝对路径、用户目录或 private artifact locator、private 题面或逐题 Pilot 数据。两套 failure
evidence 继续只发布脱敏 projection、opaque file
commitments 与 public source commitments；原 private artifact 不进入仓库。

本页不修改 Candidate、Pack、parser、prompt、scorer、tool schema、workflow、依赖、历史
evidence 或 private kit，也不触发 Provider 调用。任何 later-than-v7 successor 都必须重新锁定
source/contracts/Candidate commitment，默认不可在线执行；v6/v7 failure observation 不得用于
调整 prompt、scorer、tool schema、scenario 或 task selection。

## PR scope

本长期证据 PR 只允许新增本页，并更新 README 与 `docs/EVIDENCE.md` 的当前 main 状态入口。
不得修改代码、Candidate、contracts、Pack/review、历史 evidence、workflow、依赖或在线产物。
