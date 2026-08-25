# Supervised Completion Telemetry v2 — 脱敏长期证据

本页记录 2026-08-25 完成的一次 Completion Telemetry v2 supervised pretest。它证明
新的安全 source、coverage、event binding、技术故障继续路径和非专家 UX feedback 能在
真实 Provider 运行中贯通；不是独立科研用户增量、正式 external pilot、领域专家复核、
模型质量评测、private holdout 或未知生产分布泛化证明。

本快照不包含 participant/attempt ID、session/invite/CSRF token、API Key、IP、邮箱、
Tailscale hostname、自由文本 notes、逐题反馈、题面、Agent 答案、Provider body/raw status、
`incomplete_details`、tool arguments、路径、行级数据或精确交互时间戳。

## Provenance

| 字段 | 值 |
| --- | --- |
| Campaign | `EXT-PILOT-01A605022746D203` |
| Purpose | `completion_telemetry_v2_supervised_usability_only` |
| Participant relationship | 未做 operator independence adjudication；不主张 same-participant 或新独立参与者增量 |
| Campaign status | `complete` |
| Created / frozen / completed (UTC date) | `2026-08-23` / `2026-08-23` / `2026-08-25` |
| Execution environment | `supervised` |
| Deployment Git SHA | `950a473e860d9987e96be87d2bb3fab51acb4c7b` |
| Local Docker image ID | `sha256:7058151885e5b979690a4b3284854e7865866e978d03bf0b83e8293f6f13dc5c` |
| Candidate commitment | `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5` |
| Candidate manifest SHA-256 | `89b317f00a4d9a4f8f81ee59fb6d82e7ca225fd5d00fac450499ee2ce73b9a38` |
| Completion contract / SHA-256 | `completion-telemetry-v2` / `65e0feb9186cb8134b5d695f1977d193963387f41cfe81916089fff124431832` |
| Task pack / file SHA-256 | `pilot_pack.supervised_v3.json` / `90e81bbc282e4eabb490c00795e62aa2938977cde6b85f7d880251b45f848346` |
| Task-list commitment | `83363291f30c7edd62d30e88da38fcf966b7d01c5ac16d3a2964ee9571555d72` |
| Provider / model / transport | `deepseek / deepseek-v4-flash / openai_compatible_responses` |
| Evidence status | `supervised_external_user_pretest_only` |
| External validation claim | `false` |

v3 与 predecessor supervised v2 使用相同的六项公开 task list，因此 task-list commitment
相同。数据库直接绑定 candidate commitment 与 canonical task-list commitment；v3 文件名和
文件 SHA-256 记录 deployed-source provenance，并区分 repo 中的 pack 版本。新 candidate
明确 `prior_results_inherited=false`，旧 68/93、旧 pilot feedback 和旧 subtype unknown
记录都不能迁入本轮。

## Clean CI 与只读 projection

部署提交是 PR #12 合并后的 `main@950a473e`。该精确 commit 的：

- [offline-quality-gate run 32650036143](https://github.com/cedRiC874/researchops-agent/actions/runs/32650036143)
  为 `success`：258 个 root tests、Phase 5 50/50、evidence 21/21、profile valid；
- [pilot-staging-ci run 32650036133](https://github.com/cedRiC874/researchops-agent/actions/runs/32650036133)
  为 `success`：51 个无网络 contracts、1 个真实 PostgreSQL contract 和无 Provider Key
  Compose 链路通过；
- 相邻 production-slice 的实现提交由
  [run 32648925726](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925726)
  验证；该 run 不是本轮 telemetry 或模型质量成绩。

CI 没有执行本轮 participant tasks。最终标准字段来自 campaign summary；事件数、active
lease 和两个技术故障的安全计数来自一次只读 PostgreSQL projection。查询没有选择任何
participant/attempt ID、正文、notes、token、Provider body 或 raw status。脱敏结果见
[`aggregate_projection.json`](aggregate_projection.json)，SHA-256
`ce0920ec46e2880e039737d8a50875f9c4943d357d3021eabad84d8bb9193f8d`。
本 evidence-only projection 与后续 PR 不发起新的 Provider 调用或付费重跑。

## Lifecycle 与终态

- summary eligible / started / completed / withdrawn participants：`1 / 1 / 1 / 0`；
- planned / started / terminal assignments：`6 / 6 / 6`；
- answers displayed / feedback completed：`4 / 4`；
- completed / excluded attempts：`4 / 2`；
- technical failures：`2`；active leases：`0`；安全 incidents：`0`。

`summary eligible=1` 只是 supervised summary 的聚合计数；由于没有 operator independence
adjudication，本证据不把它称为新的独立科研参与者。`4/4` 只描述成功展示答案后的 UX
feedback，不是模型 4/6、任务成功率或专业正确性。

## Completion Telemetry v2

| 指标 | 结果 | 口径 |
| --- | ---: | --- |
| Worker-started / terminal attempts | `6 / 6` | consented、non-withdrawn supervised participant |
| Completion-source applicable / observed / unknown | `2 / 2 / 0` | coverage `complete / 100%` |
| Stable reason | `provider_output_incomplete × 2` | controlled failure；未展示为答案 |
| Safe source | `response_output_item_incomplete × 2` | allowlisted 本地 observation；不是因果根因 |
| Executor model requests | `9` | SDK usage 聚合；不是账户侧 HTTP/billing 总数 |
| Model-requested tool calls | `3` | 模型请求工具次数 |
| Backend-executed tool calls | `2` | backend 实际执行次数；不是规划准确率 |
| All-started executor elapsed P50 / P95 | 约 `5.5 / 20.3 s` | 整个 executor loop；不是纯 Provider latency 或 SLA |
| Token / cost coverage | `not_collected / unavailable` | 不能写成 0 |

`response_output_item_incomplete` 表示 executor 观察到 SDK-normalized response 或 output item
的 incomplete 状态。系统只持久化 allowlisted source 和 versioned digest，不保存 Provider
body、raw status object 或 finish metadata；因此仍不能说明“为什么 incomplete”。

## 聚合人工反馈

分母仅为四个显示答案并提交反馈的 assignments：

| 项目 | 结果 |
| --- | ---: |
| 容易理解 | `4/4` |
| 有助于决定下一步 | `4/4` |
| 希望专家进一步复核 | `0/4` |
| 发现明显问题 | `0/4` |
| 缺少继续工作所需信息 | `0/4` |
| 安全担忧 | `0/4` |
| Confidence | `high 4` |
| 人工阅读时间 P50 / P95 | `92 / 94 s` |

澄清任务在答案展示前发生受控技术故障，因此 clarification usefulness 分母为 0；不能把
它记为“澄清无用”或“澄清成功”。这些反馈来自非专家 supervised 使用，不评价统计、医学
或其他专业正确性。

## 两个开放故障登记

- [`PILOT-CTV2-20260825-001` — V2-PUB-003](failures/PILOT-CTV2-20260825-001.md)：
  `2 model / 1 requested tool / 1 backend`；
- [`PILOT-CTV2-20260825-002` — V2-PUB-027](failures/PILOT-CTV2-20260825-002.md)：
  `1 model / 0 requested tool / 0 backend`。

两个 occurrence 的稳定 source 已知，但因果根因仍 unknown。predecessor campaign 的两个
旧 occurrence 继续保持 `actual_subtype_unknown`；本轮标签不能追溯回填、合并或用于 v1/v2
模型质量比较。不得使用这六题继续调 prompt、scorer 或工具 schema。

## Integrity、retention 与停止状态

- append-only campaign event count：`30`；
- digest-bearing terminal transition events：`8`，由 `4 succeeded + 2 failed + 2 excluded`
  组成；这是六个 attempts 的状态转换事件，不是八个 attempts，且均绑定
  `pilot-execution-telemetry-v2` digest；
- attempt telemetry / participant projection binding：`valid / valid`；
- scheduled purge confirmed：`true`；
- campaign 完成后 worker、API 和 HTTPS Funnel 均已确认停止；
- pilot containers/network 已移除，PostgreSQL volume 保留；
- `secret_values_printed=false`。

内部 event/digest binding 不是外部数字签名，也不证明云级不可篡改、HA、备份恢复、生产
安全或 SLA。

## Claim boundary

```text
supervised_pretest_only=true
same_participant_retest=not_asserted
operator_independence_adjudicated=false
independent_participant_evidence_allowed=false
cross_campaign_aggregation_allowed=false
external_validation_claim_allowed=false
model_quality_claim_allowed=false
professional_correctness_assessed=false
private_holdout_claim_allowed=false
unknown_distribution_generalization_claim_allowed=false
production_sla_claim_allowed=false
causal_provider_root_cause_claim_allowed=false
```

因此不得把本轮称为新独立 participant、正式 external pilot、领域专家复核、LLM 规划准确率、
private holdout、跨 Provider 结果、未知生产泛化或生产 SLA；也不得声称本 staging 支持批准
后在线恢复，完整批准与恢复仍属于 Phase 4。
