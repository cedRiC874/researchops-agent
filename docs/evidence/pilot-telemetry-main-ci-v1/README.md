# Pilot failure telemetry + supervised UX — main CI v1

本页记录 failure telemetry、retention integrity、supervised UX 修复和同一参与者 UX
regression 证据进入默认分支后的长期快照。它证明 clean `main` checkout 上的离线合同、
真实 PostgreSQL migration/lifecycle 与无 Provider Key Compose 链路通过；不是模型质量、
第二位独立参与者、正式 external pilot、生产部署或安全认证。

## Merge provenance

| 项目 | 结果 |
| --- | --- |
| PR #7 | [Bind supervised services to one image ID](https://github.com/cedRiC874/researchops-agent/pull/7)，regular merge |
| PR #7 merge commit | `9e5dc9508d75f6ba9ab6b706d0fd8c4cc94ac085` |
| PR #8 | [Fail closed on pilot failure telemetry](https://github.com/cedRiC874/researchops-agent/pull/8)，retarget `main` 后重新 clean，regular merge |
| Current main merge commit | `4a3f5cf81e44fe51fbefd099cc98aca8e6bb2300` |
| Release status | 尚未创建包含本实现的新 Release；`v0.2.0 / phase6-deepseek-v1` 不包含它 |

Stacked PR 使用 regular merge，因此 PR #7 的 head commit 保持为 `main` 祖先；PR #8
retarget 到 `main` 后只包含其真实增量。合并前用不改文件树的空提交
`6e1eb504722456b8dfb8d28ff16d7b9983ca9809` 强制生成新的 main-base PR checks。

## Exact main runs

- [`offline-quality-gate` run 32640814960](https://github.com/cedRiC874/researchops-agent/actions/runs/32640814960)，
  `push` event，head `4a3f5cf81e44fe51fbefd099cc98aca8e6bb2300`，`success`：
  - OpenBLAS `Core: Nehalem` 与 canonical x86-v2 ANCOVA identity 通过；
  - 246 个根测试通过；
  - Phase 5 `offline_deterministic / components_and_control_plane` 为 50/50；
  - evidence citations 21/21，`phase5-ci-v1` 为 `valid`；
  - model calls 为 0。
- [`pilot-staging-ci` run 32640814963](https://github.com/cedRiC874/researchops-agent/actions/runs/32640814963)，
  `push` event，同一 head，`success`：
  - bootstrap 明确记录 `provider_secret_created=false`、
    `secret_values_printed=false`；
  - 45 个 pilot offline contracts 通过；
  - 1 个真实 PostgreSQL 17.6 migration/lifecycle/constraint contract 通过；
  - 无 Provider Key 的 Compose 配置、offline API startup、readiness、teardown 与最终
    fail-closed gate 通过；
  - online worker 和模型调用均未启动。

Production-slice workflow 没有因本次路径集触发；其 18 项合同和真实 Compose E2E 仍由
独立的 [main Linux CI 快照](../production-slice-linux-ci-main-v1/README.md) 支撑，不能
把该历史 run 冒充为 `main@4a3f5cf8` 的新运行。

## Merged engineering scope

主干现在包含：

- migrate/API/worker/retention 使用同一实际 Docker image ID；
- failure 路径以稳定 reason 持久化安全 telemetry，未知值保持 `null`，不伪装为 0；
- executor model requests、model-requested tool calls 与 backend executions 分口径；
- SQL 非负约束与真实 migration validation；
- attempt telemetry append-only binding、participant projection binding 与 claim
  fail-closed schema；
- deletion-first retention、privacy-safe tombstones、withdrawal count 保留和 lifetime
  participant cap；
- 完成页避免把“关闭页面”误操作为正式 withdraw，同时保留明确撤回入口和安全事件例外；
- `pilot_pack.supervised_v2.json` 的 public/internal-reviewed、v1-disjoint 六题 UX
  regression pack；
- Nehalem/x86-v2 Phase 5 numerical lineage，当前 ANCOVA ID
  `E-36034128278C`、chart ID `CH-6D27DA2CB989`、corpus SHA-256
  `ffa82ef11ff3e030a9b62cfa7801deab4930e131f180ab67539e774a7d88debf`。

Eval v2 locked candidate verifier 仍为 `valid`，commitment 保持：

```text
7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11
```

Phase 5 lineage 更新没有修改 `src/researchops`、prompt 或 scorer。

## Supervised participant evidence boundary

真实 supervised UX regression 发生在部署 commit
`fda5abfdafe1d7908af521b4595bd56bf2a796b3`，早于主干合并；数据库 summary、脱敏
projection 与两个 open diagnostics 已随 PR #8 进入 `main`：

- [同一参与者 UX regression v2 脱敏证据](../supervised-ux-regression-v2-20260823/README.md)
- [PILOT-DIAG-20260823-001](../supervised-ux-regression-v2-20260823/failures/PILOT-DIAG-20260823-001.md)
- [PILOT-DIAG-20260823-002](../supervised-ux-regression-v2-20260823/failures/PILOT-DIAG-20260823-002.md)

其结果永久保持：同一参与者、1 completed、0 withdrawn、6 terminal、4 feedback、
2 个 `provider_output_incomplete`，model/tool/backend 计数 9/3/2，两个 integrity
bindings valid，安全 incident 为 0。它不能计作第二位独立参与者，不能跨 v1/v2
campaign 聚合，也不能称为 external validation、模型 4/6 或 LLM 规划准确率。

## Claim boundary

本 main 快照没有读取 Provider Key、没有重新运行付费任务，也没有恢复 withdrawn v1
数据。它不能证明：

- private holdout、跨 Provider 或未知生产集泛化；
- 答案专业正确性或模型单体规划准确率；
- 正式外部科研用户可用性、领域专家复核或独立参与者重复性；
- 云 IAM/KMS/TLS、HA、备份/PITR、负载容量或生产 SLA；
- 批准后在线恢复；完整批准与恢复仍属于 Phase 4。

两个 `provider_output_incomplete` 后续只允许先用不含原题文本的 synthetic SDK
fixtures 诊断；不得使用本轮六题调 prompt，也不得未经新 candidate/预算重新运行在线
评测。
