# Production Slice Linux CI — main v1

本目录保存 production-like vertical slice 首次进入默认分支后的脱敏、聚合证据快照。

## Provenance

- PR：[GitHub PR #2](https://github.com/cedRiC874/researchops-agent/pull/2)，
  2026-08-22 squash merged。
- `main` commit：
  `badb7169fff4d6f1a3a5952ac84032d75c059b01`。
- Ubuntu 24.04 workflow：
  [`production-slice-e2e` run 32568017244](https://github.com/cedRiC874/researchops-agent/actions/runs/32568017244)，
  `push` event，结论 `success`。
- 随后的显式
  [`workflow_dispatch` run 32568233292](https://github.com/cedRiC874/researchops-agent/actions/runs/32568233292)
  也在同一 `main` commit 上成功，证明默认分支的手动触发器可用。
- 同一 commit 的 Windows 离线门禁：
  [`offline-quality-gate` run 32568017243](https://github.com/cedRiC874/researchops-agent/actions/runs/32568017243)，
  结论 `success`。
- Workflow 文件：
  [`.github/workflows/production-slice-e2e.yml`](../../../.github/workflows/production-slice-e2e.yml)。

## 已验证结果

GitHub job `compose-e2e` 在 2 分 6 秒内完成；其中真实 Compose E2E 为
68.861 秒。运行使用 CI 内临时随机 secret 和确定性合成 Palmer registry，未调用
任何模型：

- 18/18 production-slice contract tests 通过；
- API、worker、PostgreSQL、MinIO、OTel collector 与 migration 组成的六服务
  Compose 链路启动成功；
- job `e7020f9f-ea43-4b2d-8c1c-a275cf6a65ce` 一次执行成功，状态经过
  `queued → claimed → publishing → succeeded`；
- aggregate profile 为 344 行、8 列；未暴露行级数据或文件系统路径；
- PostgreSQL 的 4 个 job events 哈希链有效，deterministic object key 已核验；
- MinIO metadata、SHA-256 与 byte size 均与数据库一致；
- 幂等重放复用同一个 job ID；
- API 与 worker 共享 Trace ID
  `bf5e3f3d0b480f9d60149d18a6a4d8b4`；
- secret value、Authorization、Bearer 与 API-key 日志扫描均为 0；
- sanitized artifact 上传、无 `-v` shutdown 与最终结果 gate 均通过。

机器可读快照：

- [`main_e2e_summary.json`](main_e2e_summary.json)
- [`compose_status.json`](compose_status.json)
- [`verification.md`](verification.md)
- [`artifact_manifest.json`](artifact_manifest.json)

`main_e2e_summary.json` 与 GitHub artifact 中对应的 `e2e_summary.json` 内容
完全一致，SHA-256 均为
`6491e7edcbcb7ae619143b9e491aeb54f271233a37b3bc75da07b311d81193d8`。
原始 GitHub artifact 的 digest 为
`sha256:7669f7cba91d185c2a8a7c213ef34941b650063e3bc8fe009f94ecfceaba108d`；
远端 artifact 将于 2026-09-05 过期，因此本目录保存长期可复核的脱敏副本。

## 声明边界

这是一台 GitHub-hosted Ubuntu runner 上的单机 development Compose 纵切证据，
不是 HA、云 IAM/KMS/TLS、备份恢复、负载容量或生产 SLA 证据。Local MinIO 未配置
KMS，server-side encryption 明确为 `false`。该运行没有 LLM、外部发布或批准后
恢复；完整批准与恢复能力仍属于 Phase 4，也不能把本结果并入 Eval v2 public
candidate 的 68/93。
