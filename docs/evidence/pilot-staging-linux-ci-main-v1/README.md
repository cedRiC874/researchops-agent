# Pilot Staging 无 Provider Key Linux CI — main v1

本目录记录 pilot staging 首次合入默认分支后的脱敏、聚合 CI 证据。该证据证明
clean Linux checkout 上的离线合同、真实 PostgreSQL migration/lifecycle 和无
Provider Key API Compose 链路通过；它不是模型质量、真实参与者或 production
staging 证据。

## Provenance

- PR：[GitHub PR #5](https://github.com/cedRiC874/researchops-agent/pull/5)，
  2026-08-22 16:47:53 UTC regular merged。
- `main` merge commit：
  `a20fdfd8ff6a2e4e29881aa6693589655e307e72`。
- Ubuntu 24.04 workflow：
  [`pilot-staging-ci` run 32585792915](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792915)，
  `push` event，结论 `success`。
- Job：
  [`offline-and-postgres` 97061841249](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792915/job/97061841249)，
  2 分 37 秒内完成。
- 同一 merge commit 的
  [`offline-quality-gate` run 32585792937](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792937)
  与
  [`production-slice-e2e` run 32585792929](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792929)
  也均为 `success`。
- Workflow 文件：
  [`.github/workflows/pilot-staging-ci.yml`](../../../.github/workflows/pilot-staging-ci.yml)。

## 已验证结果

成功 job 的 bootstrap 脱敏汇总明确记录：

```text
non_provider_secret_file_count=3
provider_secret_created=false
synthetic_dataset_count=3
registry_entry_count=3
secret_values_printed=false
```

随后完成：

- exact dependency installation 与 `pip check`；
- 41 个 pilot API/domain/config/script/CI/candidate/schema 离线合同；
- 1 个真实 PostgreSQL 17.6 migration/lifecycle/checksum 合同；
- Provider key 文件不存在的 Compose 配置检查；
- 只启动 PostgreSQL、migration 和 API 的 offline Compose，不启用 `online`
  profile，也不启动 worker；
- API readiness 与 participant 页面 HTTP 合同；
- Compose teardown，无 `down -v`；
- startup 与 teardown outcome 的最终 fail-closed gate。

同一提交的 Windows job 运行 246 个根测试并重建 Phase 5 50 题离线评测，
`phase5-ci-v1` verifier 为 `valid`；production slice job 的 18 项合同和真实
PostgreSQL/MinIO/OTel Compose E2E 也通过。

## 首次失败与恢复审计

PR 首次 run
[`32585060012`](https://github.com/cedRiC874/researchops-agent/actions/runs/32585060012)
在 migration 容器找不到镜像内显式 SQL 目录时失败，最终 gate 没有把失败冒充为绿色。
修复提交 `e18881037b2fbb30137bc773c534ba5a3453bef0` 将 migration 路径绑定到
`/app/pilot/migrations`，并增加 Compose 原生命令的 fail-fast 诊断；随后 PR clean run
[`32585571140`](https://github.com/cedRiC874/researchops-agent/actions/runs/32585571140)
与上述 `main` push run 均成功。

## 声明边界

该 workflow 会访问 GitHub、Python 包索引和容器镜像源，但没有创建 Provider Key、
没有启动 online worker，也没有发起模型请求或付费在线评测。测试使用 fake executor、
确定性合成 registry 和临时 PostgreSQL；没有 Tailscale Funnel、真实参与者、真实
Provider 回答或外部科研用户反馈。

因此本结果不能证明 LLM 规划准确率、领域正确性、未知生产集泛化、外部用户可用性、
公网安全、HA、备份恢复、生产 SLA 或批准后在线恢复。Supervised 预试仍永久不能进入
正式 external validation claim；完整批准与恢复仍属于 Phase 4。

GitHub 同时给出 Actions Node.js 20 弃用提示，并在 runner 上强制使用 Node.js 24；
这未使本次 job 失败，但属于后续 Actions 版本维护项。
