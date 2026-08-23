# Completion Telemetry v2 — main CI v1

本页固定 Completion Telemetry v2 进入默认分支后的长期工程证据。它证明同一个 clean
`main` checkout 上的 root 合同、pilot PostgreSQL/retention 合同和相邻 production-slice
回归均通过；它不是新的模型质量评测、Provider 因果根因分析、private holdout、外部
pilot、生产部署或安全认证。

## Merge provenance

| 项目 | 结果 |
| --- | --- |
| PR | [#11 Completion Telemetry v2: offline diagnostics and pilot compatibility](https://github.com/cedRiC874/researchops-agent/pull/11)，regular merge |
| Feature commit | `7e60f7190ed1a84125b97e86cda9e4dc4d529d9d` |
| Main merge commit | `094cb9b173e5d153f1aff9db2ce8a25e50a57f7d` |
| Tree comparison | feature tree 与 merge tree 均为 `21e07abadea5997ab988b317647e6b56d7c3a3b8` |
| Historical v1 commitment | `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11` |
| Completion Telemetry v2 commitment | `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5` |
| Release status | 尚未创建包含 v2 的新 Release；`phase6-deepseek-v1` 不包含本实现 |

历史 `public_regression_candidate.json` 在本次合并中保持字节不变，文件 SHA-256 仍为
`b7ea7416c56b52e301c84aaa9c687b3925a64f11f6b5ae21f155ec27d67b8bfb`。新 manifest
明确 `prior_results_inherited=false`、`model_quality_claim_allowed=false`、
`private_holdout_access_authorized=false`，因此 v1 的 68/93 与既有 pilot 结果不能归给 v2。

## Exact main runs

三条 run 都由 `push` event 在同一 head
`094cb9b173e5d153f1aff9db2ce8a25e50a57f7d` 上触发并以 `success` 完成：

- [`offline-quality-gate` run 32648925769](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925769)：
  - 258 个 root tests 通过；
  - Eval/Provider preflight 报告 `network_calls=0`；
  - Phase 5 `offline_deterministic / components_and_control_plane` 重建为 50/50，
    evidence citations 21/21，`phase5-ci-v1` 为 `valid`，model calls 为 0。
- [`pilot-staging-ci` run 32648925679](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925679)：
  - 51 个无网络 pilot contracts 通过，并显式执行
    `test_completion_telemetry.py`；
  - 1 个真实 PostgreSQL migration/lifecycle/constraint/retention contract 通过；
  - 无 Provider Key 的 Compose 配置、offline API startup、teardown 与最终 gate 通过；
  - online worker 与模型调用均未启动。
- [`production-slice-e2e` run 32648925726](https://github.com/cedRiC874/researchops-agent/actions/runs/32648925726)：
  - 18 个 production-slice contracts 与真实 PostgreSQL/MinIO/OTel E2E 通过；
  - E2E run `E2E-20260823T153432Z-fd1ad49d` 最终为 `succeeded`，
    `secret_values_printed=false`；
  - 该 run 证明 PR #11 没有破坏相邻服务，不是 Completion Telemetry 或模型质量的直接成绩。

## Merged engineering scope

主干现在包含：

- 四个稳定、allowlisted 的 `completion_failure_source`：
  `final_output_missing`、`response_output_item_incomplete`、
  `response_not_completed`、`output_limit_suspected`；
- 稳定 source/error/outcome 映射与固定优先级；
- legacy completion failure 的 observed/unknown coverage，缺字段不伪装成零故障；
- public checkpoint、repetition/channel/provider aggregation 与敏感字段拒绝；
- pilot PostgreSQL migration `0003`、v1/v2 双 telemetry digest、mixed-retention tombstone 和篡改检测；
- 独立 v2 candidate、summary schema 1.2 与 supervised v3 pack；
- 无 Provider Key 的 Linux CI 对新增 telemetry fixture 和真实 PostgreSQL 路径的覆盖。

`completion_failure_source` 只描述本地可观察到的受控分支，不是 Provider 内部或因果根因。
旧行保持 legacy unknown；旧 campaign 不回填，也不能与新 candidate campaign 聚合。

## Claim boundaries

- 本次 main CI 没有读取 Provider API Key、调用模型或运行付费评测。
- v2 仍只是 `candidate_locked`；完整 Eval v2 campaign 仍为 `design_only`。
- v1 public-regression 68/93、Phase 6 16/16 与 repo-local holdout 4/4 均不属于 v2 成绩。
- 本页不证明 private holdout、第二 Provider、领域专家复核、未知生产集泛化、生产 SLA、
  HA、云安全或外部科研用户满意度。
- production-slice run 仅证明同一 main commit 的相邻服务回归通过。

规范文件：[RFC](../../COMPLETION_TELEMETRY_V2_RFC.md)、
[machine contract](../../../evals/v2/completion_telemetry_contract.json)、
[v2 candidate](../../../evals/v2/public_regression_candidate_v2.json)。
