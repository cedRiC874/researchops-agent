# Supervised 同一参与者 UX Regression v2 — 脱敏证据

本页记录 2026-08-23 完成的一次同一参与者监督式 UX 修复回归。它验证的是邀请、
同意、完成页、人工反馈、故障排除和安全 telemetry 流程；不是第二位独立参与者证据，
不是正式 external pilot，也不是模型质量、LLM 规划准确率、private holdout 或未知生产
分布泛化证明。

本快照不包含参与者 ID、session/invite token、API Key、IP、邮箱、自由文本 notes、
Agent 答案正文、Provider 原始响应、文件路径或行级数据。

## Provenance

| 字段 | 值 |
| --- | --- |
| Campaign | `EXT-PILOT-15D41CA378E73503` |
| Campaign title | `ResearchOps same-participant supervised UX regression v2` |
| Purpose | `same_participant_ux_regression_only` |
| Participant relationship | 与 accidental-withdrawal v1 session 为同一参与者；独立参与者增量为 0 |
| Campaign status | `complete` |
| Created / frozen / completed (UTC date) | `2026-08-23` / `2026-08-23` / `2026-08-23` |
| Deployment Git SHA | `fda5abfdafe1d7908af521b4595bd56bf2a796b3` |
| Local Docker image ID | `sha256:c949767954bc2845be782bddbcb2fc4eb61ae88df66e285e3b6874025ad20a4d` |
| Candidate commitment | `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11` |
| Task-pack commitment | `83363291f30c7edd62d30e88da38fcf966b7d01c5ac16d3a2964ee9571555d72` |
| Provider / model / transport | `deepseek / deepseek-v4-flash / openai_compatible_responses` |
| Evidence status | `supervised_external_user_pretest_only` |
| External validation claim | `false` |

`pilot_pack.supervised_v2.json` 固定为 1 人、6 assignments、最多 6 次 Agent task
execution。该 assignment 上限不是 Provider 账户 API 请求数或费用上限；一个任务可以
包含多次模型请求。

## Clean CI 绑定

最终部署提交对应的远端检查均为 `success`：

- [PR #8](https://github.com/cedRiC874/researchops-agent/pull/8) 在本次执行时的部署 head
  为 `fda5abfdafe1d7908af521b4595bd56bf2a796b3`，merge state `CLEAN`；后续
  evidence-only 文档提交可以移动 PR head，但不能改变本 campaign 的部署绑定；
- [push offline-quality-gate run 32637329319](https://github.com/cedRiC874/researchops-agent/actions/runs/32637329319)：
  Phase 5 为 50/50、evidence 21/21，canonical numeric preflight 通过；
- [pull-request offline-quality-gate run 32637331529](https://github.com/cedRiC874/researchops-agent/actions/runs/32637331529)：
  独立 PR checkout 同样通过；
- [pilot-staging-ci run 32637331530](https://github.com/cedRiC874/researchops-agent/actions/runs/32637331530)：
  无 Provider Key 的离线合同、真实 PostgreSQL migration/lifecycle 和 Compose API
  链路通过。

这些 CI 运行没有执行本次在线 participant tasks。标准字段来自最终 campaign summary；
all-worker-started elapsed 与逐故障计数来自一次只读、脱敏 PostgreSQL projection。该
projection 只选择任务/scenario、稳定状态/reason、计数与 duration，不选择 participant、
答案、notes 或 Provider body，并保存为
[`aggregate_projection.json`](aggregate_projection.json)，SHA-256
`e4cdce88a649b3773fb9b3b65e1d4ffe89661f503f147ff134115f6d89953545`。

Campaign 完成后的 evidence-only CI rerun 又暴露了 Windows hosted CPU dispatch 对
raw-float evidence identity 的敏感性。该问题没有改变本 campaign 的部署源码或数据库
事实。后续提交 `8dc67706e4ffec8e48057747020f8c8d82f66bb5` 将 Phase 5 重建固定到
Nehalem/x86-v2 数值基线，并以实际 canonical ANCOVA ID 而不是 NumPy 内部 feature flag
作为前置门禁；当前 Phase 5 corpus lineage 为 ANCOVA `E-36034128278C`、chart
`CH-6D27DA2CB989`、corpus SHA-256
`ffa82ef11ff3e030a9b62cfa7801deab4930e131f180ab67539e774a7d88debf`。以下
post-campaign runs 均为 `success`：

- [push offline run 32639815327](https://github.com/cedRiC874/researchops-agent/actions/runs/32639815327)；
- [pull-request offline run 32639817573](https://github.com/cedRiC874/researchops-agent/actions/runs/32639817573)；
- [pilot-staging-ci run 32639817571](https://github.com/cedRiC874/researchops-agent/actions/runs/32639817571)。

这次 lineage 更新没有修改 `src/researchops`、prompt、scorer 或 Eval v2 candidate；
candidate commitment 仍为 `7744770a…f0d11`。它也不把历史 Phase 5 50/50 称为 LLM
规划准确率。

PR #7/#8 随后已 regular merge；当前默认分支为
`main@4a3f5cf81e44fe51fbefd099cc98aca8e6bb2300`。该精确 main commit 的
[offline run 32640814960](https://github.com/cedRiC874/researchops-agent/actions/runs/32640814960)
与
[pilot run 32640814963](https://github.com/cedRiC874/researchops-agent/actions/runs/32640814963)
均为 `success`。长期合并证据见
[pilot telemetry main CI v1](../pilot-telemetry-main-ci-v1/README.md)。Campaign 本身仍
绑定执行时的 deployment SHA `fda5abf…`，主干合并不能倒改该 provenance。

## 冻结任务与终态

6 个 source tasks 全部来自 `public_regression + ready + internal_reviewed`，与 v1 的
6 题零重叠；选择依据是三个数据集各 2 题和六个场景各 1 题，而不是模型成绩。

| 顺序 | Source task | 场景 | 数据集 | 终态 |
| ---: | --- | --- | --- | --- |
| 1 | `V2-PUB-028` | `standard_analysis` | Cleveland | completed + feedback |
| 2 | `V2-PUB-003` | `clarification_required` | Palmer | excluded after technical failure |
| 3 | `V2-PUB-027` | `safe_refusal` | Parkinsons | completed + feedback |
| 4 | `V2-PUB-031` | `unauthorized_resource` | Cleveland | excluded after technical failure |
| 5 | `V2-PUB-006` | `prompt_injection` | Palmer | completed + feedback |
| 6 | `V2-PUB-023` | `approval_pause` | Parkinsons | completed + feedback |

Campaign aggregate：

- eligible / started / completed / withdrawn participants：`1 / 1 / 1 / 0`；
- planned / started / terminal assignments：`6 / 6 / 6`；
- answer displayed / feedback completed：`4 / 4`；
- completed / excluded attempts：`4 / 2`；
- technical failures：`2`，均为稳定 reason `provider_output_incomplete`；
- active leases：`0`；安全 incidents：`0`。

`4/4` 只描述四个成功显示答案后的 UX feedback，不是 6 题模型通过率，也不评价答案
专业正确性。两个故障 assignment 没有进入可用性反馈分母。

## 执行 telemetry

| 指标 | 结果 | 口径 |
| --- | ---: | --- |
| Worker-started attempts | 6 | consented、non-withdrawn participant |
| Terminal attempts | 6 | 全部进入受控终态 |
| Executor model requests | 9 | SDK usage 聚合；不是账户侧 API/billing 总数 |
| Model-requested tool calls | 3 | 模型请求工具的次数 |
| Backend-executed tool calls | 2 | backend 实际执行次数；不等于模型规划正确率 |
| Telemetry coverage | complete | 6 observed、0 unknown |
| Executor wall-clock elapsed P50 / P95 | 6.6335 s / 25.3615 s | 脱敏 projection 的 6 个 worker-started attempts；数据库字段 `provider_latency_ms`，覆盖 SDK loop、多轮编排和本地 backend 执行，不是纯 Provider 延迟或生产 SLA |
| Failure reason | `provider_output_incomplete × 2` | controlled failure，未把空/不完整输出展示为答案 |

模型请求、模型工具请求与 backend 实际执行次数被分开记录；本地缓存或去重不能冒充模型
规划正确。Token usage、Provider 账单和成本没有进入本 summary，不能据此声称成本为 0。

## 聚合人工反馈

分母仅为 4 个成功显示答案并提交反馈的 assignments：

| 项目 | 结果 |
| --- | ---: |
| 容易理解 | 4/4 |
| 有助于决定下一步 | 4/4 |
| 希望专家进一步复核 | 1/4 |
| 发现明显问题 | 0/4 |
| 缺少继续工作所需信息 | 1/4 |
| 安全担忧 | 0/4 |
| Confidence | medium 2 / high 2 |
| 人工阅读时间 P50 / P95 | 71.5 s / 72 s |

澄清任务 `V2-PUB-003` 在答案展示前技术失败，因此 clarification usefulness 的反馈
分母为 0；不能把它记为“澄清无用”或“澄清成功”。

## Integrity、retention 与停止状态

- append-only campaign event count：`30`；
- append-only attempt telemetry binding：`valid`；
- participant projection binding：`valid`；
- scheduled purge confirmed：`true`；
- Windows retention task `ResearchOps-Pilot-Retention` 状态 `Ready`；最近一次运行日期
  `2026-08-23`、结果 `0`，下一次计划日期 `2026-08-24`；
- campaign 完成后 worker、API 和 Tailscale Funnel 已停止；
- PostgreSQL container 已移除但 volume 保留，供 retention 与审计使用；
- `secret_values_printed=false`。

这些内部 hash/event binding 不是外部数字签名，也不能单独证明云级不可篡改、HA、备份
恢复、生产安全或 SLA。

## 开放故障登记

两个 `provider_output_incomplete` 分开登记，避免把不同 tool/turn 轨迹压成一条模糊问题：

- [`PILOT-DIAG-20260823-001` — V2-PUB-003](failures/PILOT-DIAG-20260823-001.md)
- [`PILOT-DIAG-20260823-002` — V2-PUB-031](failures/PILOT-DIAG-20260823-002.md)

后续只能用合成/模拟 SDK completion fixtures 做通用故障诊断。不得使用本轮六题继续调
prompt，不得重放 withdrawn v1 数据，也不得未经新预算与新 candidate 授权重新运行付费
在线评测。

## Claim boundary

本轮永久保持：

```text
same_participant_retest=true
independent_participant_evidence_allowed=false
cross_campaign_aggregation_allowed=false
external_validation_claim_allowed=false
model_quality_claim_allowed=false
unknown_distribution_generalization_claim_allowed=false
professional_correctness_assessed=false
```

因此不得把本轮称为第二位 pilot、外部领域专家复核、LLM 规划准确率、模型 4/6、private
holdout、跨 Provider 结果或未知生产流量泛化。
