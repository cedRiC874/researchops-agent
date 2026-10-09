# Kimi Candidate v8 diagnostic successor — main CI v1

本页固化 PR #24、PR #25、Candidate v8，以及 `main@b9054774` 的最终 clean checks。
它是长期工程状态快照，不是新的 Provider observation、Kimi compatibility 结果、模型成绩、
在线授权、Provider 注册或 private/non-synthetic 评测证据。

## Merge provenance

| 字段 | PR #24 | PR #25 |
| --- | --- | --- |
| Pull request | [docs(evidence): record PR #23 main CI](https://github.com/cedRiC874/researchops-agent/pull/24)，regular merge | [feat(eval-v2): add Kimi v8 diagnostic-only successor](https://github.com/cedRiC874/researchops-agent/pull/25)，regular merge |
| Base commit | `27fad953436fd88987d21fcafc1de776f16ab78f` | `01cbc4a370deec2ba5a7a419f8cbd36adc3b98d3` |
| Final reviewed PR head | `e5e1472b36a1ddee9c9aebcae07506f9620591ef` | `662aa6e4109a154943537c6b0a24e4238ca2de19` |
| Merge commit / main at snapshot | `01cbc4a370deec2ba5a7a419f8cbd36adc3b98d3` | `b905477449938b471c4b9af84398ad6e7ba2212b` |
| Merge parents | `27fad953436fd88987d21fcafc1de776f16ab78f`、`e5e1472b36a1ddee9c9aebcae07506f9620591ef` | `01cbc4a370deec2ba5a7a419f8cbd36adc3b98d3`、`662aa6e4109a154943537c6b0a24e4238ca2de19` |
| Base tree | `338dbcc0a798ceccae6e01a616be8f2cddfbd3f1` | `085fbe65e0aa5f7368132a871949bd65bcd89e4f` |
| PR-head / merge tree | `085fbe65e0aa5f7368132a871949bd65bcd89e4f`，完全相同 | `5b0be18e61bc14dd255e56f775e278e83ffad094`，完全相同 |
| Merge time | `2026-08-27T18:39:32Z`（Asia/Shanghai：`2026-08-28T02:39:32+08:00`） | `2026-08-27T19:00:36Z`（Asia/Shanghai：`2026-08-28T03:00:36+08:00`） |
| Provider calls in PR/main CI | `0` | `0` |

PR #25 的最终 reviewed head 是一个同步 merge commit；它以
`46af28ab48293280e1b71123b05e8ffb2ddb3da1` 与最新的
`main@01cbc4a370deec2ba5a7a419f8cbd36adc3b98d3` 为 parents。同步后 README 与
`docs/EVIDENCE.md` 同时保留 PR #24 的相邻 main-CI 状态和 Candidate v8 内容。最终 PR head
和 regular merge tree 完全相同，因此不存在文件内容漂移。

## PR #24 checks and post-merge main

PR #24 最终 head 的两条 status checks 均为 `completed / success`：

- [`offline-quality-gate` push run 33094813774](https://github.com/cedRiC874/researchops-agent/actions/runs/33094813774)，
  job `98596821752`；
- [`offline-quality-gate` pull-request run 33094857456](https://github.com/cedRiC874/researchops-agent/actions/runs/33094857456)，
  job `98596970802`。

PR #24 regular merge 后，`main@01cbc4a` 的
[`offline-quality-gate` push run 33104580118](https://github.com/cedRiC874/researchops-agent/actions/runs/33104580118)
（job `98630885986`）成功：534 项 root tests 通过，Phase 5
`offline_deterministic / components_and_control_plane` 为 50/50、evidence 21/21，
profile 与 `phase5-ci-v1` 均为 `valid`。该文档范围合并没有触发 pilot 或 production workflow；
不能把未触发写成已运行。

## PR #25 checks

PR #25 在同步最新 main 后，最终 head `662aa6e` 的四条 status checks 均为
`completed / success`：

- [`offline-quality-gate` push run 33105515523](https://github.com/cedRiC874/researchops-agent/actions/runs/33105515523)，
  job `98634178987`；
- [`offline-quality-gate` pull-request run 33105521102](https://github.com/cedRiC874/researchops-agent/actions/runs/33105521102)，
  job `98634197758`；
- [`pilot-staging-ci` pull-request run 33105521030](https://github.com/cedRiC874/researchops-agent/actions/runs/33105521030)，
  job `98634197285`；
- [`production-slice-e2e` pull-request run 33105520820](https://github.com/cedRiC874/researchops-agent/actions/runs/33105520820)，
  job `98634196918`。

这些 checks 只证明 clean checkout 的离线合同、真实 PostgreSQL 集成和相邻 production
slice 回归通过。它们没有读取用户 Key、调用 Kimi 或生成模型 token。

## Final main checks

`main@b905477449938b471c4b9af84398ad6e7ba2212b` 精确有三条 push workflow runs，均为
`completed / success`：

- [`offline-quality-gate` 33106332631](https://github.com/cedRiC874/researchops-agent/actions/runs/33106332631)，
  job `98637052136`：608 项 root tests 通过。Candidate v8 verifier 返回
  `valid / diagnostic_snapshot_only=true / network_calls=0`，public-regression 与 controlled
  synthetic Pilot online authorization 均为 false；未确认与已确认 v8 CLI gates 均在 Key
  lookup 前以零调用方式拒绝。Phase 5 `offline_deterministic / components_and_control_plane`
  为 50/50、evidence 21/21、`phase5-ci-v1=valid`、model calls 0。
- [`pilot-staging-ci` 33106333010](https://github.com/cedRiC874/researchops-agent/actions/runs/33106333010)，
  job `98637053631`：54 项 offline contracts、1 项真实 PostgreSQL migration/lifecycle
  contract、无 Provider secret 的 Compose config/startup/teardown/final gate 均通过；
  `provider_secret_created=false`、`secret_values_printed=false`。Candidate v5 / Pack6 仍是
  active 配置，Candidate v8 未接入 Pilot Staging。
- [`production-slice-e2e` 33106332748](https://github.com/cedRiC874/researchops-agent/actions/runs/33106332748)，
  job `98637052226`：18 项 service contracts 与真实 PostgreSQL/MinIO/OTel Compose E2E
  通过；最终 `status=passed`、`terminal_status=succeeded`、
  `secret_values_printed=false`。

Phase 5 的 50/50 不是 LLM 规划准确率；Pilot/production checks 也不是 Kimi Chat、usage、
tool、error-semantics 或模型质量证据。

## Candidate v8 boundary

| 字段 | 值 |
| --- | --- |
| Candidate ID | `eval-v2-public-regression-deepseek-kimi-controlled-chat-v8` |
| Candidate commitment | `b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c` |
| Predecessor commitment | `2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5` |
| Status | `candidate_locked / diagnostic_snapshot_only=true` |
| Online calls / network calls | `0 / 0` |
| Public / controlled Pilot online authorized | `false / false` |
| Full campaign frozen | `false` |
| Prior result / v7 failure / v7 authorization inherited | `false / false / false` |
| Model quality claim allowed | `false` |
| Registered Providers / private holdout | `1/2 / 0/50` |

Chat/Pilot v3 保持 v2 request bytes、acceptance predicates 和 validation precedence，仅为
`kimi_chat_response_invalid` 增加 39 项固定本地 branch code。诊断对象只允许 schema 与 code，
不含 raw header/body、Provider ID/hash、字段值、offset、size、prompt、reasoning、tool payload、
authorization binding 或自由文本。Candidate v8 不可由新授权解锁；任何未来在线尝试必须使用
later-than-v8 的新 successor、fresh legal/pricing review、全新授权 ID/expiry、明确 caps/budget
和用户一次性授权。

Kimi 仍未注册，正式 Provider 仍为 `1/2`；Private Holdout 仍为
`0/50 / not_authorized`，non-synthetic release 固定拒绝。完整 Eval v2 campaign 仍为
`design_only`，public/Pilot execution Provider 仍为 DeepSeek，active Pilot 配置仍为
Candidate v5 / Pack6。

## Immutable identities

| 文件 | Git blob | File SHA-256 |
| --- | --- | --- |
| `evals/v2/public_regression_candidate_v8.json` | `6eeb6cee70a09f212528a72e15dee5e4c32c3391` | `6615cc6638e79dc854265e46cf61dd4de9932e57a6fe2415b721f0a408b4eac0` |
| `evals/v2/kimi_chat_completions_contract_v3.json` | `7fdb4cf616e75ba4337a3081c8347666269e72ce` | `464b87bb4a1db9b66c252ef502a73dfb5037637ad75c3619fbc61349fad873c2` |
| `evals/v2/kimi_controlled_pilot_contract_v3.json` | `896c5e2898ec38188356dd9594f93cf2d147c652` | `8e0b9b67f5427ddde1097c2d1ff9769a309f1031bd0b224a328f430ec675079b` |
| `evals/v2/kimi_runtime_candidate_v8_contract.json` | `f2e27dc5d2ec72b3c9fd0f120ee0574c32ba15b4` | `b5b74b08a34ed346fbc12999b8ddb76505c3b6728361560aa32638c122e3eb98` |
| `src/researchops/kimi_chat_transport_v3.py` | `384ba16c36df6ebcfaa717dc76a6550abbb11d77` | `6a702c9edb4153b258c5ea1dcc8a9047b2d46fa8281a38fac7c0ee93a475bdef` |
| `src/researchops/kimi_controlled_pilot_v3.py` | `c44e827fad11d5f88ad414cd08f0e8891b2e8ccd` | `39e03a20d64e6907231233b087bb4517b3de854e4db1493b81038e44485cab00` |
| `docs/KIMI_CONTROLLED_PILOT_V3_RUNBOOK.md` | `40697ad4b93c94e34ffc858420cdf7ef2490e94b` | `1823231feed25169e981a9521d438968ce2db8f468ad36b09356a3f5b3280e3d` |
| `.github/workflows/ci.yml` | `db4758ac9576ee7f8e503bac1a30faec90b8fc01` | `51acdf70596c7bfa9d5245023a0a09a16c6c664b7236d3a5215215eabb61fa3d` |

这些 identity 绑定 `main@b9054774` 的文件 bytes。历史 v1–v7 Candidates、Pack1–8、历史
failure evidence 与既有 main-CI 快照继续保持原样。

## Publication, privacy and scope boundary

本页不包含 API Key、Authorization/Bearer、raw Provider header/body、prompt、reasoning、tool
arguments/results、request/completion ID 或 hash、authorization ID/hash/binding、用户邮箱、本机
绝对路径、private artifact locator、private 题面或逐题 Pilot 数据。

本长期证据 PR 只允许新增本页，并更新 README 与 `docs/EVIDENCE.md` 的当前 main 状态入口。
不得修改代码、Candidate、contracts、Pack/review、历史 evidence、workflow、依赖或在线产物；
也不触发 Provider 调用。
