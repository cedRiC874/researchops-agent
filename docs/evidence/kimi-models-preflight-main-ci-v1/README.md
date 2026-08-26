# Kimi Models preflight candidate v5 — main CI v1

本页固化 PR #21、`main@c65ff65c`、candidate v5、supervised v6 pack、独立 post-lock
metadata receipt，以及同一 merge commit 的三条最终 main checks。它不包含 API Key、raw
Provider body、raw request ID、request-ID hash、用户邮箱、private 题面或逐题 Pilot 数据。

## Merge provenance

| 字段 | 值 |
| --- | --- |
| PR #21 | [feat(providers): add Kimi Models API preflight](https://github.com/cedRiC874/researchops-agent/pull/21)，regular merge |
| Base commit | `3dfb0a367b51f45656cbc315989a85f75341a0f7` |
| Implementation / candidate-lock commit | `ca0e0380223c450eaeb4d5a9c8a96bdd8084240d` |
| Final reviewed PR head | `ccd03ccfe6fb5b0973050ebfa72517c2c41a92fc` |
| PR #21 merge commit / main at snapshot | `c65ff65c0cbb67205956ddae991768ba9fca9293` |
| Merge parents | `3dfb0a367b51f45656cbc315989a85f75341a0f7`、`ccd03ccfe6fb5b0973050ebfa72517c2c41a92fc` |
| PR-head / merge tree | `ddaf063feb19cae571b87bf2c238196cabf8a5e9`，完全相同 |
| Provider calls in PR/main CI | `0`；candidate verifier 与未确认 CLI 均报告 `network_calls=0` |

PR #21 于 `2026-08-26T10:11:53Z` regular merge。Merge commit 保留 base 与最终 PR
head 两个 parent；reviewed head 和 merge tree 完全相同，因此没有文件内容漂移，也不能误写成
fast-forward、rebase 或 squash merge。

## Timeline and evidence separation

1. Candidate v5 于 `2026-08-26T08:39:45Z` 在任何 Kimi live request 之前锁定，只绑定
   MockTransport 离线实现与 `network_calls=0` 的 pre-call snapshot。
2. 一次单独授权的 metadata GET 于 `2026-08-26T09:41:49.967Z` 完成。它是 candidate
   锁定后的外部 observation，不回填 candidate 或 supervised v6 pack。
3. Final PR head `ccd03ccf…` 只用文档记录脱敏 receipt 和一次性授权已消耗；没有再次调用
   Provider。
4. PR #21 merge 后的三条 main workflows 都是 clean-checkout 工程证据，没有读取 Provider
   Key 或调用 Kimi。

因此 `candidate/CI network_calls=0` 与“项目历史另有一次 post-lock metadata GET”同时为真，
不得互相覆盖或合并成模型成绩。

## PR and main checks

Final PR head `ccd03ccf…` 的 checks 全部为 `completed / success`：

- [`offline-quality-gate` pull-request run 32956425112](https://github.com/cedRiC874/researchops-agent/actions/runs/32956425112)
  与同 head 的 [`push` run 32956412297](https://github.com/cedRiC874/researchops-agent/actions/runs/32956412297)；
- [`pilot-staging-ci` 32956425125](https://github.com/cedRiC874/researchops-agent/actions/runs/32956425125)；
- [`production-slice-e2e` 32956425121](https://github.com/cedRiC874/researchops-agent/actions/runs/32956425121)。

合并后的 `main@c65ff65c` 精确有三条 push workflow runs，均为
`completed / success`：

- [`offline-quality-gate` 32957003253](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003253)，
  job `98140772939`：368 项 root tests（Windows 本地对应 1 项平台条件 skip）；candidate v5
  commitment `105b7def…5165dffc` 与 predecessor v4 通过 verifier，CI
  `network_calls=0`；Phase 5 `offline_deterministic / components_and_control_plane` 为
  50/50、success rate 1、evidence 21/21、`phase5-ci-v1=valid`。
- [`pilot-staging-ci` 32957003191](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003191)，
  job `98140772908`：51 项 offline contracts、1 项真实 PostgreSQL migration/lifecycle
  contract、无 Provider secret Compose startup/teardown/final gate 均通过；
  `provider_secret_created=false`、`secret_values_printed=false`。
- [`production-slice-e2e` 32957003204](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003204)，
  job `98140773306`：18 项 service contracts 与真实 PostgreSQL/MinIO/OTel Compose E2E
  通过；最终 `status=passed`、`terminal_status=succeeded`、
  `secret_values_printed=false`。

这些 checks 证明 clean checkout 的离线合同、真实 PostgreSQL 集成和相邻服务回归通过。
Phase 5 的 50/50 不是 LLM 规划准确率；Pilot/production checks 也不是 Kimi API、tools、
usage/cost semantics 或模型质量证据。

## Temporary GitHub artifacts

| Run | Artifact | ID | Bytes | ZIP SHA-256 | Expires at |
| --- | --- | --- | ---: | --- | --- |
| `32957003253` | `phase5-offline-evidence` | `9602423225` | 5,648 | `8530175bfc6c7cdcc665c3c9e2f9c20588c0c9c35a40b98fd9393734d6b99add` | `2026-09-09T10:14:27Z` |
| `32957003204` | `production-slice-e2e-32957003204` | `9602404725` | 2,861 | `6a1299a76a7147ec4bb5546e08b08380a00bee0233168c1e9fbcafee76c4ead1` | `2026-09-09T10:13:52Z` |

Pilot run 没有 artifact。这些 GitHub artifacts 是 14 天临时产物，本页只记录其当时的 ID、
大小、digest 和过期时间；长期证据不依赖它们永久可下载。

## Candidate v5 and supervised v6 boundary

| 字段 | 值 |
| --- | --- |
| Candidate | `eval-v2-public-regression-deepseek-kimi-models-preflight-v5` |
| Status | `candidate_locked`；完整 campaign 仍为 `design_only` |
| Commitment | `105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc` |
| Predecessor v4 commitment | `1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7` |
| Prior results / post-lock receipt inherited | `false / false` |
| Public/Pilot execution Provider | 仍为 `deepseek / deepseek-v4-flash` |
| Kimi candidate contract status | `implemented_offline_tested_not_run`（pre-call snapshot） |
| Kimi Chat/tool/model calls | `0` |
| Kimi campaign registered | `false`；正式 Provider 仍为 `1/2` |
| Model-quality claim allowed | `false` |
| Private holdout | `0/50`、未授权；`non_synthetic_release_supported=false` |
| Supervised v6 task commitment | `83363291f30c7edd62d30e88da38fcf966b7d01c5ac16d3a2964ee9571555d72` |
| Supervised v6 online/participant evidence | 无；沿用 DeepSeek 与历史 v5 的六题/翻译，不继承历史结果 |

关键文件固定值：

| 文件 | Git blob | File SHA-256 |
| --- | --- | --- |
| `evals/v2/public_regression_candidate_v5.json` | `a36120587ca56f87c75d328da5f583ac9a9e8806` | `b6d31c5b48ff62f4e4774c4aaa9a392d9a324dd0cd07b686bbccbac940063a8c` |
| `evals/v2/kimi_provider_contract.json` | `cff40bae65d17d2e95fe8f62e6ec35c36af3ba09` | `d40f5696ce6930d0710dc73b875ad09c9cd55c5b6a4606a96fea043608541e17` |
| `evals/v2/kimi_models_preflight_contract.json` | `ef37f0d2f1c48d76f20adeec8271afb98a11b817` | `3cc4c7cbbc1fff33c8952abf8b57d30bc33a5487b7da29de00b0268cb2d34f51` |
| `src/researchops/kimi_preflight.py` | `3bcc1936414cf40ae4883555165276535f55dc61` | `2627935a19ee798be867f9ee87f26ede07734355339a6780857dd5901e9bfe3a` |
| `services/pilot_staging/content/pilot_pack.supervised_v6.json` | `7684504dbb4fed8816a6bd08502990414c2aa5d6` | `7536b148fd3873135707962421e57482602a3daccb1d982f6bb4d72219cec3c5` |
| `services/pilot_staging/content/pilot_pack.supervised_v6.review.json` | `b65505c62f98456bf5119f2c4bda7d2de57957cd` | `6b62f4125c1b4b8ee99c1dcd4fc28a8ffb87de746457c6e345965e87fe9cbdfd` |

## Post-lock metadata receipt

公开来源为
[PR #21 脱敏评论](https://github.com/cedRiC874/researchops-agent/pull/21#issuecomment-5423475486)：

| 字段 | 值 |
| --- | --- |
| Checked at | `2026-08-26T09:41:49.967Z` |
| Method / endpoint | `GET https://api.moonshot.cn/v1/models` |
| Status / HTTP | `verified / 200` |
| HTTP attempts / network calls | `1 / 1` |
| Latency | `580 ms` |
| Requested / returned model | `kimi-k3 / kimi-k3` |
| Models API authenticated / exact model visible | `true / true` |
| Model token calls | `0` |
| Token usage / cost | `null / null`；不得改写成免费或成本 0 |
| Retry / Chat / Responses / tools / generation | 均未执行 |

Receipt 只证明该中国区账号在该时间点认证成功且 exact model 可见。它不验证或授权 Chat、
Responses、tools、usage/cost semantics、模型质量、Pilot、campaign 注册、non-synthetic 或
private evaluation。Request-ID hash 仅限本地 correlation，未进入公开 evidence。一次性授权已
消耗且不得重试；任何新 Provider request 需要新的明确授权。

## Historical immutability

从 base `3dfb0a36…` 到 final reviewed head `ccd03ccf…` 的 Git blob 对比确认：

- historical v1–v4 candidates：4/4 unchanged；
- historical supervised v1–v5 packs/reviews：9/9 unchanged（v1 仅有 pack，v2–v5 各有
  pack/review）；
- 已存在的 `docs/evidence/**`：35/35 unchanged；
- `evals/v2/campaign.json`、两个 Anthropic contracts 和 legacy
  `pilot_summary.schema.json` 均 unchanged；
- PR/merge tree 中没有 CCTK、`output/**` 或 `email/**` 文件。

因此历史 candidates、packs/reviews 与 evidence 均保持原样；candidate v5、supervised v6
及 post-lock receipt 互不继承。

## Next gates

1. 下一项可向用户建议预审当前有效的 2025-04-28 条款与已公布的 2026-08-31 条款；只有
   当前用户任务明确要求时才实施，结论继续限定为 synthetic-only，不把尚未生效的文本冒充
   最终版本。
2. 可向用户建议在不调用 Provider 的前提下设计 public candidate v6 与新的 supervised
   pack；只有当前用户任务明确要求时才实现。请求数、输入/输出 token、金额、时限、
   retry/fallback 和 side-effect 上限都必须先作为 draft 明示并获得用户接受。
3. 任何新在线 request 都需要新的 Key、封顶预算、请求/token 上限和一次性明确授权；现有
   metadata 授权不得复用。
4. 只有完成外部领域专家、R/SAS cross-check、private 50 题冻结及全部合规门禁后，才考虑
   （不是自动）正式注册第二 Provider 或启用 non-synthetic private evaluation。

当前 private custodian kit 仍为 synthetic-only；真实 non-synthetic release 固定拒绝。
Phase 6 仍不支持批准后在线恢复。

## PR scope

本长期证据 PR 只允许新增本页并更新必要的当前状态入口。它不得修改代码、workflow、依赖、
candidate、contracts、prompt、scorer、tool schema、旧 packs/reviews、历史 evidence、private
kit 或在线运行产物，也不得读取 API Key、公开 request-ID hash 或触发 Provider 调用。
