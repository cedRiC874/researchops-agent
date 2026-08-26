# Anthropic Models preflight candidate v4 — main CI v1

本页固化 PR #19 与 `main@77911226` 的长期工程证据。它记录固定 Anthropic Models
metadata preflight、generic Anthropic online entrypoints fail closed、candidate v4、supervised
v5 pack 和同一 merge commit 的三条最终 main checks；不包含 API Key、Provider response
body、在线模型结果、private 题面或逐题 pilot 数据。

## Merge provenance

| 字段 | 值 |
| --- | --- |
| PR #19 | [feat(providers): add fail-closed Anthropic Models preflight](https://github.com/cedRiC874/researchops-agent/pull/19)，regular merge |
| Base commit | `9eabf43455e37d87116f403fbb349a6ba4106ea1` |
| PR head | `3694aa6d3838dcaeb30616db102ab8fa6a8edc4e` |
| PR #19 merge commit / main at snapshot | `77911226b0e2a7e7d15ac5be9c2aafc19c5ea335` |
| Merge parents | `9eabf43455e37d87116f403fbb349a6ba4106ea1`、`3694aa6d3838dcaeb30616db102ab8fa6a8edc4e` |
| PR head / merge tree | `3db2b3c3406b3ca264c2c4989f6bee64f8e1a65d`，完全相同 |
| Model/Provider calls in CI | `0`；offline contract reports `network_calls=0` |

PR #19 于 2026-08-26 regular merge。由于 base 在合并时未出现额外内容冲突，PR head
与 merge commit 的 tree 完全相同；merge commit 仍保留两个 parent，不能把 tree 相同误写成
fast-forward 或 squash merge。

## PR and main checks

PR #19 的 check rollup 全部通过，且都绑定 PR head `3694aa6d…`：

- [`offline-quality-gate` push run 32851884066](https://github.com/cedRiC874/researchops-agent/actions/runs/32851884066)
  与 [`pull_request` run 32851926803](https://github.com/cedRiC874/researchops-agent/actions/runs/32851926803)；
- [`pilot-staging-ci` 32851926835](https://github.com/cedRiC874/researchops-agent/actions/runs/32851926835)；
- [`production-slice-e2e` 32851926886](https://github.com/cedRiC874/researchops-agent/actions/runs/32851926886)。

PR #19 合并后的 `main@77911226` 精确有三条 push workflow runs，均为
`completed / success`：

- [`offline-quality-gate` 32930474006](https://github.com/cedRiC874/researchops-agent/actions/runs/32930474006)：
  334 项 root unit/integration tests；candidate v4 commitment
  `1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7` 与 predecessor v3
  `22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9` 通过 verifier；
  Anthropic preflight 为 `implemented_offline_tested_not_run`，campaign/online calls 均为
  `false`，`network_calls=0`；Phase 5
  `offline_deterministic / components_and_control_plane` 为 50/50、evidence citations 21/21、
  `phase5-ci-v1=valid`。
- [`pilot-staging-ci` 32930473989](https://github.com/cedRiC874/researchops-agent/actions/runs/32930473989)：
  51 项 offline contracts、1 项真实 PostgreSQL migration/lifecycle contract，以及无 Provider
  secret 的 offline Compose startup/teardown/final gate 均通过；
  `provider_secret_created=false`、`secret_values_printed=false`。
- [`production-slice-e2e` 32930473976](https://github.com/cedRiC874/researchops-agent/actions/runs/32930473976)：
  18 项 service contracts 与真实 PostgreSQL/MinIO/OTel Compose E2E 通过；最终
  `status=passed`、`terminal_status=succeeded`、`secret_values_printed=false`。

这些 checks 证明 clean checkout 的离线合同、真实 PostgreSQL 集成和相邻服务回归通过。
Phase 5 的 50/50 不是 LLM 规划准确率；pilot/production checks 也不是 Anthropic API
可用性、tool compatibility 或模型质量证据。

## Candidate v4 and Provider boundary

| 字段 | 值 |
| --- | --- |
| Candidate | `eval-v2-public-regression-deepseek-anthropic-models-preflight-v4` |
| Status | `candidate_locked`；完整 campaign 仍为 `design_only` |
| Commitment | `1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7` |
| Predecessor v3 commitment | `22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9` |
| Prior results inherited | `false` |
| Public/pilot execution Provider | 仍为 `deepseek / deepseek-v4-flash` |
| Anthropic preflight | `implemented_offline_tested_not_run` |
| Live Models preflight performed | `false` |
| Anthropic campaign registered | `false` |
| Anthropic online calls performed | `false` |
| Model-quality claim allowed | `false` |
| Private holdout | `0/50`、未授权；正式注册 Provider 仍为 `1/2` |
| Private custodian kit | `kit_ready_not_authorized`；`non_synthetic_release_supported=false` |

Anthropic preflight 目前只具备固定 `GET /v1/models/{exact_model_id}` 的实现与
MockTransport 离线证据。未确认命令返回 `not_run / network_calls=0`；即使未来获得成功
receipt，它也固定为 non-authorizing，不能授权 Messages/tools、campaign 注册、private access
或质量声明。Generic Phase 6/self-pilot/Web/public-runner Anthropic 入口继续 fail closed，受控
Anthropic Messages/tool pilot 入口尚未实现。

## Historical immutability

从 `main@9eabf434` 到 `main@77911226` 的 Git tree 对比确认：

- historical v1 candidate `evals/v2/public_regression_candidate.json` 的 Git blob 保持
  `5f7107c12123836ffafae60cc42f4f8ccec8253d`；
- predecessor v2 candidate `evals/v2/public_regression_candidate_v2.json` 的 Git blob 保持
  `cf263d7e980734d9967e0f6b819ca68033a509e3`；
- predecessor v3 candidate `evals/v2/public_regression_candidate_v3.json` 的 Git blob 保持
  `fe204f0aa011154e8a8c2b544f5a04c0bd7c6d0d`；
- supervised v1–v4 的既有 pack/review 文件均未改变；在 pilot pack/review 集合中，PR #19
  只新增 successor v5 pack/review；
- 既有 `docs/evidence/` 文件均未改变。

因此 v1/v2/v3 candidates、历史 pilot packs/reviews 与历史 evidence 保持原样；candidate
v4 明确 `prior_results_inherited=false`，v5 review 明确
`prior_pilot_results_inherited=false`。

## Next gates

1. 任何真实 Models metadata preflight 仍需用户显式提供 Key 与一次性联网授权。它只验证
   固定 endpoint 的认证与 exact model visibility，不调用 Messages/Completions，也不授权
   后续 pilot。
2. 只有用户明确提供 Key、封顶预算和一次性授权后，才可运行预承诺的小规模
   tool/usage/error-semantics pilot；结果不得用于调 prompt/scorer/tool/candidate，不并入既有
   Eval 成绩，也不使用 repo-local holdout 或已运行公开题。
3. 只有完成外部领域专家复核、R/SAS 独立 cross-check，以及 external custodian 的真实
   private 50 题评测与合规 aggregate completion 后，才考虑（不是自动）正式注册第二
   Provider 或启用 non-synthetic private evaluation。

当前 private custodian kit 仍为 synthetic-only；真实 non-synthetic release 固定拒绝。
Phase 6 仍不支持批准后在线恢复。

## PR scope

本长期证据 PR 只允许新增本页并更新 README、ARCHITECTURE、CURRENT_PROJECT_STATUS、
EVIDENCE、PORTFOLIO、handoff、Anthropic Provider 与 pilot verification 的当前状态入口。
它不得修改代码、workflow、依赖、candidate、prompt、scorer、tool schema、旧
packs/reviews、历史 evidence 或在线运行产物，也不得读取 API Key 或触发付费评测。
