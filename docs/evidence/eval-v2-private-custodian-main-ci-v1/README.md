# Eval v2 private custodian kit v1.1 — main CI snapshot

本页固化 PR #14 合并后的长期工程证据。它记录 synthetic conformance kit、pilot retention
证据和两个 `main` clean runs；不包含 private 题面、golden、case ID、storage locator、
Provider body、API Key 或逐题结果。

## Merge provenance

| 字段 | 值 |
| --- | --- |
| Pull request | [#14](https://github.com/cedRiC874/researchops-agent/pull/14)，regular merge |
| PR head | `4d3cd7857feaed5aad4f2335e20ccfb8b58f9aab` |
| Main merge commit | `16a9133d480e3e1bdf9a53aed29ac884a44c3539` |
| Main offline run | [32829222869](https://github.com/cedRiC874/researchops-agent/actions/runs/32829222869)，`success` |
| Main pilot/PostgreSQL run | [32829223001](https://github.com/cedRiC874/researchops-agent/actions/runs/32829223001)，`success` |
| Model/Provider calls | `0`；custodian verifier 报告 `network_calls=0` |

## Main offline gate

Run `32829222869` 在 merge commit 上完成：

- exact locked dependencies 与 Nehalem/x86-v2 numerical baseline；
- Eval/Phase 6/Eval v2 contracts；
- `279` 项 unit/integration tests；hosted Windows 的目录 symlink 与 release mutation
  fail-closed 用例实际执行并通过；
- reviewed protocol/schema pins、`design_only`、private request/access 均为 false；
- non-synthetic release support 与 model-quality claim 均为 false；
- Phase 5 50-task offline suite 重建及 fail-closed quality profile；
- 仅上传脱敏 Phase 5 aggregate artifacts，临时 artifact 不作为永久存储。

初次 push run `32828026219` 暴露了测试钩子使用 Windows 临时路径表示比较的问题；
`4d3cd78` 将钩子改为八个固定 release 文件名后，PR push/PR checks 与上述 main run
全部 clean。该修复只影响攻击注入测试的跨环境触发，不放宽 verifier。

## Main pilot/PostgreSQL gate

Run `32829223001` 在同一 merge commit 上完成：

- 无 Provider Key 的 pilot offline contracts；
- 真实 PostgreSQL migration 与 lifecycle contract；
- Compose 配置不需要 Provider secret；
- offline API stack 启动、受控停止且不删除 volumes；
- final fail-closed result gate。

本轮本机 retention 证据仍单独保留：Scheduled Task `Ready`、手动 maintenance result `0`、
participant/feedback deadline 为 90 天上限，见
[retention verification](../supervised-completion-telemetry-v2-20260825/retention-verification-20260825.md)。

## Claim boundary

进入 `main` 只证明 v1.1 的 synthetic protocol/schema、Ed25519 role-separation 检查、调用方
提供的 external anchors、两阶段 ledger、aggregate/budget/suppression 算术与 fail-closed
CI 行为。

当前仍为：

```text
campaign_status=design_only
private_registered_case_count=0/50
registered_provider_count=1/2
private_request_allowed=false
private_access_authorized=false
non_synthetic_release_supported=false
model_quality_claim_allowed=false
```

它不证明已有 private corpus、真实授权或运行、第二 Provider、领域专家复核、R/SAS
cross-check、模型质量、未知生产集泛化或 SLA。完整边界见
[kit README](../../../evals/v2/private_holdout_kit/README.md) 与
[custodian guide](../../PRIVATE_HOLDOUT_CUSTODIAN_KIT.md)。

本证据 PR 只允许更新 Markdown 状态/索引；不得修改运行代码、workflow、candidate、prompt、
scorer、tool schema 或冻结 public tasks。
