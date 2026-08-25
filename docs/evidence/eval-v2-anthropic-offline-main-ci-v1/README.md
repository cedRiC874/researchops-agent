# Anthropic offline adapter candidate v3 — main CI v1

本页固化 PR #15、PR #16 与 `main@3e487096` 的长期工程证据。它记录 private
custodian evidence predecessor、Anthropic CLI/offline adapter、candidate v3、同一
tree 发布关系和三个最终 main checks；不包含 API Key、Provider body、在线模型结果、
private 题面或逐题 pilot 数据。

## Merge provenance

| 字段 | 值 |
| --- | --- |
| PR #15 | [docs(evidence): record private custodian main CI](https://github.com/cedRiC874/researchops-agent/pull/15)，regular merge |
| PR #15 head | `24a9d29346014b4f41425687122ef12d9b6d3652` |
| PR #15 merge commit | `f17df93510b1fe9d933a6a509a47de91dc664e44` |
| PR #15 post-merge main run | [`offline-quality-gate` 32839710490](https://github.com/cedRiC874/researchops-agent/actions/runs/32839710490)，`success` |
| PR #16 | [feat(providers): complete Anthropic offline adapter](https://github.com/cedRiC874/researchops-agent/pull/16)，regular merge |
| Local implementation commit | `329a6dc75c4429ddec24f720599c828c58950251` |
| Published PR #16 head | `7079af0869586291f8ef4f063fda429039bd4f3e` |
| Local/remote PR #16 tree | `710ef581f2e1355e33d9af135325468b4db1c095`，完全相同 |
| Current main merge commit | `3e487096f34d483b453c68a131d605ba72a17368` |
| Current main tree | `e568820b077669393f03a889e9f2fd186e924787` |
| Model/Provider calls in CI | `0`；offline contract reports `network_calls=0` |

Git transport 故障期间，PR #16 head 由 GitHub Git Data API 发布，因此本地实现 commit
与远端 PR head 的 commit SHA 不同。两者 tree 完全相同，说明文件内容没有漂移。
`main@3e487096` 的两个 parent 是 PR #15 merge `f17df935…` 和 PR #16 head
`7079af08…`；merge tree 同时包含两项工作，所以不应与单独的 PR #16 head tree 比较为
“内容不一致”。

## PR and main checks

PR #15 的两个 `offline-evaluation` checks 均为 `success`。合并后的 main run
`32839710490` 在 `main@f17df935` 上完成：

- 279 项 root unit/integration tests；
- Eval、Phase 6、Eval v2 与 private custodian contracts；
- Phase 5 `offline_deterministic / components_and_control_plane` 50/50；
- evidence citations 21/21、artifact/profile verifier `valid`；
- `network_calls=0`，没有在线 Provider 调用。

PR #16 的 check rollup 全部成功：两个 `offline-evaluation` checks，以及
[`pilot-staging-ci` 32838891018](https://github.com/cedRiC874/researchops-agent/actions/runs/32838891018)
和
[`production-slice-e2e` 32838891019](https://github.com/cedRiC874/researchops-agent/actions/runs/32838891019)。

合并后的 `main@3e487096` 精确有三个 GitHub Actions check runs，均为
`completed / success`：

- [`offline-quality-gate` 32840171286](https://github.com/cedRiC874/researchops-agent/actions/runs/32840171286)：
  301 项 root tests；candidate v3/predecessor/Anthropic contract 校验；Phase 5 50/50、
  evidence 21/21；private custodian 继续 fail closed；`network_calls=0`。
- [`pilot-staging-ci` 32840171287](https://github.com/cedRiC874/researchops-agent/actions/runs/32840171287)：
  51 项 offline contracts、1 项真实 PostgreSQL migration/lifecycle contract、无 Provider
  secret 的 offline Compose startup/teardown/final gate；
  `provider_secret_created=false`、`secret_values_printed=false`。
- [`production-slice-e2e` 32840171312](https://github.com/cedRiC874/researchops-agent/actions/runs/32840171312)：
  18 项 service contracts 与真实 PostgreSQL/MinIO/OTel Compose E2E；最终
  `status=passed`、`secret_values_printed=false`。

这些 checks 证明 clean checkout 的离线合同和相邻服务回归通过。Phase 5 的 50/50 不是
LLM 规划准确率；pilot/production checks 也不是 Anthropic 在线质量证据。

## Candidate v3 boundary

| 字段 | 值 |
| --- | --- |
| Candidate | `eval-v2-public-regression-deepseek-anthropic-offline-v3` |
| Status | `candidate_locked`；完整 campaign 仍为 `design_only` |
| Commitment | `22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9` |
| Predecessor v2 commitment | `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5` |
| Prior results inherited | `false` |
| Public/pilot execution Provider | 仍为 `deepseek / deepseek-v4-flash` |
| Anthropic implementation status | `offline_contract_only` |
| Anthropic campaign registered | `false` |
| Anthropic online calls performed | `false` |
| Model-quality claim allowed | `false` |
| Private holdout | `0/50`、未授权；正式注册 Provider 仍为 `1/2` |

Anthropic 目前只在 Phase 6/self-pilot CLI 路径具备离线构造合同；self-pilot Web preflight
和付费 public runner 均未启用。CI 没有证明真实 Models/Messages API 可用性、tool-call
兼容、usage/error semantics、成本、延迟或模型质量，也不继承 DeepSeek 的 public/pilot
结果。

## Historical immutability

从 `main@16a9133d` 到 `main@3e487096` 的 Git tree 对比确认：

- historical v1 candidate `evals/v2/public_regression_candidate.json` 的 Git blob 保持
  `5f7107c12123836ffafae60cc42f4f8ccec8253d`；
- predecessor v2 candidate `evals/v2/public_regression_candidate_v2.json` 的 Git blob 保持
  `cf263d7e980734d9967e0f6b819ca68033a509e3`；
- 6 个既有 pilot pack/review 文件均未改变；PR #16 只新增 supervised v4 pack/review；
- 既有 32 个 `docs/evidence/` 文件均未改变；PR #15 只新增 private custodian main CI
  evidence。

因此 v1/v2 candidate、历史 pilot packs 和历史 evidence 保持原样；candidate v3 明确
`prior_results_inherited=false`，v4 review 明确 `prior_pilot_results_inherited=false`。

## Next gates

1. 下一项单独设计 Anthropic 专用 `GET /v1/models/{model_id}` 零生成-token 可用性预检；
   它只验证固定 endpoint、认证与 exact model 可见性，不调用 Messages/Completions。它仍是
   联网认证请求，不能称为 offline、免费或 Messages/tool/质量证据。
2. 只有用户明确提供 Key、封顶预算和一次性授权后，才可运行预承诺的小规模
   tool/usage/error-semantics pilot；结果不得用于调 prompt/scorer/tool/candidate，不并入
   既有 Eval 成绩，也不使用 repo-local holdout 或已运行公开题。
3. 只有完成外部领域专家复核、R/SAS 独立 cross-check，以及 external custodian 的
   private 50 题 registration/commitment 后，才考虑（不是自动）把 campaign 第二 Provider
   从 `planned` 改为 `registered`，或扩展 non-synthetic private evaluation。

当前 private custodian kit 仍为 synthetic-only；真实 non-synthetic release 固定拒绝。
Phase 6 仍不支持批准后在线恢复。

## PR scope

本长期证据 PR 只允许新增本页并更新 README、EVIDENCE、PORTFOLIO 与 handoff 的当前状态
入口。它不得修改代码、workflow、依赖、candidate、prompt、scorer、tool schema、旧 packs、
历史 evidence 或在线运行产物，也不得读取 API Key 或触发付费评测。
