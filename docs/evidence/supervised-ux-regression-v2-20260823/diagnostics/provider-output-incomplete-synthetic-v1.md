# `provider_output_incomplete` synthetic characterization v1

本页记录对两个 `provider_output_incomplete` open diagnostics 的无网络、tests-only
characterization。它没有使用 `V2-PUB-003`、`V2-PUB-031` 或本轮其他题目的 prompt，
没有读取 Provider response body、参与者数据或 API Key，也没有修改
`src/researchops`、prompt、scorer 或 locked candidate。

## Scope

| 字段 | 值 |
| --- | --- |
| Related records | [`PILOT-DIAG-20260823-001`](../failures/PILOT-DIAG-20260823-001.md)、[`PILOT-DIAG-20260823-002`](../failures/PILOT-DIAG-20260823-002.md) |
| Test file | `tests/test_eval_v2_provider_executor.py` |
| Synthetic task IDs | `V2-DEV-997`、`V2-DEV-998` |
| Dataset logical ID | `synthetic_aggregate_fixture` |
| Network calls | 0 |
| Provider Key read | no |
| Runtime source changes | none |
| Candidate commitment impact | none；仍为 `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11` |

Synthetic tasks 只在测试模块中构造，不写入 public/development corpus，也不成为新的
模型评测题。

## Characterized mechanisms

| Fixture | Synthetic mechanism | Final output | Stable result | Model / requested tool / backend |
| --- | --- | --- | --- | ---: |
| `test_synthetic_blank_final_output_preserves_zero_tool_telemetry` | SDK result 的 `final_output` 为空；无 raw response completion metadata | empty | `controlled_failure / provider_output_incomplete / output_truncated` | `1 / 0 / 0` |
| `test_synthetic_incomplete_output_item_preserves_inspection_telemetry` | 非空 synthetic output；一次成功 aggregate inspect；`raw_responses[*].output[*].status` 含 `incomplete`，每个 response token 均低于 limit | non-empty synthetic text | `controlled_failure / provider_output_incomplete / output_truncated` | `2 / 1 / 1` |

第二个 fixture 同时断言：

- model-requested tool count 来源为 `sdk_new_items`；
- gateway dispatched count 为 1；
- backend executed count 为 1；
- 唯一工具是参数受限的 `inspect_dataset`，status 为 `succeeded`；
- completion failure 不会把成功 backend 调用冒充为完整最终回答。

第一个 fixture 同时断言所有 tool/gateway/backend counts 均为 0，model count 为 1，
且空 final output 不会被提升为成功答案。

## What this resolves—and does not resolve

已确认当前 executor 中存在两个彼此独立的 synthetic mechanisms，它们都会安全、稳定地
折叠为 `provider_output_incomplete`，且三类计数按原值传播。现有源码行为与本轮
telemetry 的稳定 reason 一致，因此本轮不需要修改 production source 来“修测试”。

本 characterization **不能**判断真实 occurrence 001 或 002 究竟属于哪一个 mechanism：
生产 telemetry 有意不持久化 raw output/body，也没有安全 subreason 字段。它同样不能
证明 Provider、模型、classifier、output limit 或 prompt 是根因。

## Reproduction

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m unittest `
  tests.test_eval_v2_provider_executor.EvalV2ProviderExecutorTests.test_synthetic_blank_final_output_preserves_zero_tool_telemetry `
  tests.test_eval_v2_provider_executor.EvalV2ProviderExecutorTests.test_synthetic_incomplete_output_item_preserves_inspection_telemetry -v
```

该命令使用 fake Provider、fake SDK runner 和 fake aggregate backend；不会联网。

## Next decision gate

若未来需要在生产 telemetry 中区分 `final_output_missing` 与
`response_output_item_incomplete`，必须：

1. 设计不含正文的 allowlisted subreason；
2. 更新 schema/event digest/retention/summary 的 fail-closed 合同；
3. 因 `src/researchops` 会变化而创建新 Eval v2 candidate/version；
4. 不继承旧 public-regression 或本轮 supervised 成绩；
5. 使用不同任务和新预算做后续验证，不得用本轮六题调 prompt。

在该决策前，两个真实 records 保持 `open / actual_subtype_unknown`；synthetic 分支覆盖
已完成，但真实根因没有被过度推断。
