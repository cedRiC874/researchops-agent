# Eval v2：外部留出、多数据集与重复运行

> 当前状态：完整 campaign 仍为 `design_only`；DeepSeek v1 public-regression candidate 已于 2026-08-21 一次性运行完成。Completion Telemetry v2 是新的离线 candidate，尚未在线运行且不继承 v1 结果。本页不代表 private holdout、第二 Provider 或外部复核已经完成。

Eval v2 的目标是解决 Phase 6 小样本、repo-local holdout 可见、单 Provider 和单数据集证据不足的问题。它不会覆盖或重新解释现有 Phase 5/6 成绩，也不会再次使用当前 4 题 holdout 调整 prompt。

可执行设计清单位于 [`v2/campaign.json`](v2/campaign.json)，公开 task schema、draft corpus 和外部数据集清单分别位于 [`v2/public_task_schema.json`](v2/public_task_schema.json)、[`v2/public_tasks.jsonl`](v2/public_tasks.jsonl) 与 [`v2/external_datasets.json`](v2/external_datasets.json)。离线交叉校验命令为：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-validate
```

该命令只读取仓库内公开 contract、manifest 和 draft tasks，执行严格 schema、交叉引用与 readiness gate 校验，网络调用为 0。`status=valid` 只表示设计合同有效；只有 `ready_for_freeze=true` 才表示满足冻结前置条件。

公开数据下载复核必须显式确认。命令在内存中验证 archive/asset SHA-256、bytes、行列数和缺失计数，不保存外部数据：

```powershell
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-verify-datasets `
  --confirm-download
```

2026-08-19 的实际复核结果为 3/3 verified、3 次网络请求、下载 436,356 bytes、写入文件 0。未传 `--confirm-download` 时固定返回 `not_run`，网络调用为 0。

受控准备器同样要求显式确认，只允许写入 `artifacts/` 下尚不存在的新目录：

```powershell
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-prepare-datasets `
  --output-dir artifacts/eval_v2_datasets/local-01 `
  --confirm-download

.\.venv\Scripts\python.exe -m researchops.cli eval-v2-registry-status `
  --registry artifacts/eval_v2_datasets/local-01/logical_dataset_registry.json

.\.venv\Scripts\python.exe -m researchops.cli eval-v2-inspect `
  uci_parkinsons_telemonitoring_189 `
  --registry artifacts/eval_v2_datasets/local-01/logical_dataset_registry.json
```

## 1. 三层数据划分

| Split | 目标题数 | 存储与可见性 | Prompt 调优 | 用途 |
| --- | ---: | --- | --- | --- |
| `development` | 80 | repo-local development | 允许 | prompt、tool schema、scorer 和运行时迭代 |
| `public_regression` | 40 | repo-local public regression | 禁止 | 冻结后的公开回归与持续集成 |
| `private_holdout` | 至少 50 | external custodian | 禁止 | 运行前不可见的外部复核 |

private holdout 的题面、golden、task ID 和存储位置均不得进入仓库。仓库最多保存外部 custodian 提供的 SHA-256 commitment、任务数量和最终脱敏聚合结果。

“每个 Provider 重复 3 次”与“private holdout 只运行一次”并不冲突：一次 private campaign 包含每个冻结 Provider 的 3 次预承诺重复；同一 freeze 不得再次提交第二个 private campaign。修改 prompt、源码、tool schema、scorer、数据集或依赖后，必须形成新 freeze，旧结果不能继续证明新版本。

## 2. 任务覆盖

Eval v2 至少覆盖以下场景：

- 标准分析与 evidence-grounded 数值回答；
- 缺少设计信息时澄清；
- 敏感导出、伪造、删除审计或越权访问时安全拒绝；
- 受控写操作首次审批暂停；
- 未授权逻辑资源、路径注入和 prompt injection；
- Provider timeout、输出截断和 incomplete completion；
- 重复工具调用、幂等重放和调用预算；
- 副作用结果未知与 reconcile/fail-closed。

当前 Phase 6 的 20 题可以作为公开设计参考，但不能把其中 4 题 repo-local holdout 原样迁入 Eval v2 private holdout，也不能根据它继续调 prompt。

## 3. 多数据集策略

Campaign 保留 5 个逻辑数据集槽位：当前仓库内 synthetic trial、3 个已完成来源/许可/哈希核验的外部公开数据集，以及 1 个待外部 pilot 决定的 deidentified 槽位。

| Dataset | 许可 | 固定资产 | 结构核验 | 主要评测价值 |
| --- | --- | --- | --- | --- |
| Palmer Penguins v0.1.0 | CC0-1.0 | `penguins.csv`，SHA `f204db2c…` | 344×8，19 个缺失 cell | 非随机观察数据、多物种、非因果两组比较边界 |
| UCI Parkinsons Telemonitoring #189 | CC BY 4.0 | `parkinsons_updrs.data`，SHA `f2c7d502…` | 5,875×22，42 个 subject，无缺失 | 重复测量、subject ID、不能把行当独立样本 |
| UCI Heart Disease Cleveland #45 | CC BY 4.0 | `processed.cleveland.data`，SHA `a74b7efa…` | 303×14，6 个 `?` 缺失 | 二分类结局、健康数据、敏感导出拒绝与方法不支持 |

官方来源与许可：Palmer Penguins 项目页及 CC0 声明、UCI dataset 189/45 页面及各自 DOI。manifest 同时固定下载 archive 的 hash/bytes；下载 URL 发生内容漂移时复核命令会 fail closed。

冻结最低门槛是：

- 至少注册 3 个互异数据集；
- 至少 3 个必须为 public 或经过批准的 deidentified 数据；
- 外部数据在注册前完成许可核对和外部复核；
- 数据只通过逻辑 ID 暴露给 Agent，不把绝对路径加入任务或审计；
- private holdout 不能只依赖仓库内 synthetic dataset。

这 3 个外部数据集当前是 `source_verified + license approved + external_review planned`。自动下载复核不能替代领域专家 review，因此 campaign 仍将它们标为 `planned`，不会计入 registered dataset readiness。`deidentified_dataset_slot_05` 仍只是 pilot 需求槽位。

## 3.1 公开 task schema v2

公开 task schema 使用 JSON Schema 2020-12，并由 Python 严格解析器再次验证。每题必须包含：

- `V2-DEV-NNN` 或 `V2-PUB-NNN` 稳定 ID；
- development/public regression split；private split 在仓库内被拒绝；
- `lifecycle_status=draft|ready` 和 review 状态；未复核题不能标为 ready；
- dataset manifest 中存在且允许该 split 的 `dataset_id`；
- 与顶层 dataset 完全一致的授权 context；
- scenario、prompt、完整工具顺序/参数、outcome、evidence、numeric claim 目录和安全 golden；
- `public_input()` 只投影 task ID、prompt 和授权 context，不泄露 expected golden。

当前 public corpus 已达到 120 题：development 80、public regression 40。全部任务均已通过 hash-bound 内部工程复核并标为 ready。

内部复核记录位于 [`v2/internal_review.json`](v2/internal_review.json)，精确绑定当前 public corpus SHA-256 和全部 120 个 ready task ID。它明确标为 `project_internal_non_external`，不能替代领域专家或 private custodian 复核。当前 registered 数为 development 80、public regression 40。

120 个 public tasks 均覆盖三个数据集，整体场景分布为：标准聚合检查 20、澄清 22、安全拒绝 19、越权资源 12、prompt injection 11、重复工具调用 12，以及 Provider timeout、输出截断、审批暂停、副作用结果未知各 6。三类故障通过 `controlled_failure` outcome 与普通失败分开建模。

## 3.2 受控准备器与逻辑资源 registry

准备器对已核验 selected asset 执行固定转换：

- Palmer Penguins：保留 curated 8 列、规范化表头和缺失值，不引入 individual ID；
- Parkinsons：规范化 22 列表头，以 SHA-256 派生的 `subject_key` 替换原始 `subject#`，随后删除原编号；
- Heart Disease：只选 processed Cleveland 14 列，附加固定列名并把 `?` 规范化为空缺失值，不读取 unprocessed identifier columns。

控制属性：

- 下载 archive 与 selected asset 在转换前重新核对 hash/bytes/结构；
- 原始下载只驻留内存，不写盘；
- 准备目录必须位于 `artifacts/` 下且必须不存在；
- 先写同父目录 staging，全部校验后原子 rename；失败时只清理 staging；
- registry 只登记相对路径、prepared hash、source hash、转换版本和分析边界；
- `resolve(dataset_id)` 每次重新做受控根目录包含检查、文件存在性、size 和 SHA-256 校验；
- 模型侧 catalog 不包含路径、文件 hash 或行级值，`model_access=aggregate_tools_only`；
- 独立 `EvalV2InspectDatasetBackend` 通过 registry 解析受控 CSV，重新计算 profile，并只投影行列计数、缺失、类型、唯一率、identifier 风险、缺失模式和分析边界；
- inspect 输出明确排除文件名、路径、SHA、`sample_values`、subject key 值和原始行；
- 现有 Phase 6 的 `synthetic_trial` 静态 registry 保持不变，避免改变冻结证据所对应的代码边界。

真实公开资产已经通过下载复核；准备器的原子写入、伪名化、越界、覆盖、未知 ID 和篡改检测由隔离 fixture 测试覆盖。仓库不提交准备后的行级 CSV。

## 3.3 独立 runner 与 scorer

`run_eval_v2_evaluation()` 接收可注入 `EvalV2TaskExecutor` 和 `EvalV2InspectDatasetBackend`：

- executor 只能收到 `EvalV2PublicTask.public_input()`，不能访问 expected golden；
- 每题创建独立 tool gateway，`inspect_dataset` 参数必须精确等于授权 `dataset_id`；
- `publish_aggregate_results` 只生成一次 `awaiting_approval` 提案，固定零副作用；
- 未知工具、参数替换和第二个发布提案 fail closed，并进入观察轨迹；
- 当前仅支持 `injected_offline` / `scripted_regression`，报告固定 `evidence_status=harness_regression_only` 与 `model_quality_claim_allowed=false`。

Runner/scorer `1.2` 检查完整工具顺序、精确参数、outcome、required/forbidden 文本、evidence ID、closed numeric claim catalog、审批旁路、安全状态和 completion integrity。Forbidden phrases 保持严格 literal；forbidden assertions 逐次检查明确的局部否定，避免把 “not a continuous outcome” 当作正向断言，同时双重否定或后续未限定正向断言仍 fail closed。`provider_timeout`、`output_truncation`、`side_effect_outcome_unknown` 必须分别匹配稳定错误码与 completion 状态。报告按 split、scenario、dataset 聚合成功率，并记录工具/参数准确率、审批控制、P50/P95 和 usage 完整性。

Completion Telemetry v2 为 completion failure 增加 `completion_failure_source`，只允许 `final_output_missing`、`response_output_item_incomplete`、`response_not_completed` 和 `output_limit_suspected`。分类顺序固定，source 必须与稳定 `error_code` 配对；旧 artifact 缺字段时记为 legacy unknown，并显式报告 applicable/observed/unknown coverage。逐题 checkpoint、重复运行和跨 Provider 聚合只保存安全标签及计数，不保存 Provider body、raw status 或 incomplete details。这些标签是本地诊断分支，不是因果根因。

当前同时具有离线 runner/scorer/backend 证据与一次性 public candidate 在线证据。Provider-behavior 为 68/93，三轮 23/31、22/31、23/31；本地 fault harness 为 27/27，未归因模型、未进入模型分母。这不是完整 120 题 × 3 Provider campaign，也不是 private holdout 或未知生产集成绩。

Provider executor `1.2` 已按锁定的 Agents SDK 0.21 接口接入现有 `ProviderAdapter`：每次运行显式绑定 provider/model/transport 和独立 client，要求 `--confirm-online` 等价的显式确认，不记录或输出 Key；第三方 Provider 强制关闭 OpenAI tracing。SDK Agent 在工具创建前区分明显的拒绝、澄清、正常检查和发布请求：匹配的危险/未授权请求与待澄清设计不暴露工具，拒绝由本地输出合同补齐稳定 reason；合法检查缓存相同 dataset 的执行结果；发布上下文只暴露 publish。未被预分类的请求仍受逻辑 registry/gateway 参数授权与零副作用审批边界保护。该策略是 control-plane guardrail，public 结果归因于“模型 + 锁定控制面”，不应称为模型一次规划正确。

拒绝与澄清使用独立短预算：refusal 512 tokens、clarification 768 tokens；普通任务继续使用运行配置值。因果澄清由本地确定性双语模板规范化，固定说明数据是 `observational`、不能识别因果效应，并询问是否改写为 `association analysis`。原始 Provider 空输出或达到策略上限仍是 controlled failure，本地模板不得掩盖完成性失败。

重复工具诊断分开记录：`model_requested_tool_call_count`（优先取 SDK `new_items`，否则明确 wrapper fallback）、`deduplicated_tool_call_count`、`gateway_dispatched_tool_call_count` 和 `backend_executed_tool_call_count`。评分仍基于去重后的 gateway trace；这些计数只用于诊断，不能把 backend 一次执行表述为模型一次规划正确。

Runner 支持 `run_eval_v2_three_repetitions()`：同一 Provider 顺序运行 repetition 1/2/3，每次使用相同 task-ID 集合但允许不同的预承诺排列。跨 Provider 聚合器要求每个 Provider 恰好三次并按 task ID 对齐，计算成功率均值/范围、逐题稳定率、三次全通过率、模型调用量和 usage 覆盖；发布阶段可要求至少两个 Provider。

Public-regression candidate 使用三份预承诺 seed/task order；三次排列不同但 task-ID 集合相同，聚合器按 task ID 对齐，不再按列表位置 zip。40 题分成 31 道 Provider 行为题和 9 道 deterministic fault-injection harness 题，两个 execution channel 必须分开报告，故障注入结果不得归因于模型。

## 3.4 Public-regression candidate lock

历史 [`public_regression_candidate.json`](v2/public_regression_candidate.json)、[`public_regression_candidate_v2.json`](v2/public_regression_candidate_v2.json) 与 [`public_regression_candidate_v3.json`](v2/public_regression_candidate_v3.json) 分别保留 v1 一次性结果、Completion Telemetry v2 与 Anthropic offline-adapter commitments。当前 [`public_regression_candidate_v4.json`](v2/public_regression_candidate_v4.json) 使用 `candidate_locked`，不是完整 campaign 的 `frozen`。它继续把 public-run Provider 固定为 DeepSeek，同时绑定固定 Anthropic Models API preflight 实现/机器合同、predecessor adapter contract、campaign planned slot、source、prompt/scorer/tool、Completion Telemetry、40 题 split/三次顺序、public corpus/schema、dataset manifest、internal review、`pyproject.toml` 与 `requirements.lock`。完整 campaign 仍为 `design_only`，`full_campaign_frozen=false`、`private_holdout_access_authorized=false`、`model_quality_claim_allowed=false`、`prior_results_inherited=false`。

```powershell
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-verify-public-freeze `
  --verify-environment
```

`requirements.lock` 的 82 个版本全部精确 pin；`openai-agents[litellm]`、`litellm==1.83.0`、`httpx==0.28.1`、`httpcore==1.0.9`、`h11==0.16.0` 与固定 CA bundle `certifi==2026.7.22` 均显式锁定，但仍没有 wheel/sdist artifact hashes。历史 commitment `7744770a…f0d11` 已完成一次性 public-regression；v2 `1f6ac18e…e5ce5`、v3 `22c985e9…b2a9` 与 v4 successor 均只完成离线验证，不继承历史 68/93。Anthropic 仍为 `offline_contract_only / campaign_registered=false / online_calls_performed=false`；Models preflight 为 `implemented_offline_tested_not_run`，generic online entrypoints fail closed，完整 campaign 仍非 frozen。实现与使用边界见 [Anthropic Provider 指南](../docs/ANTHROPIC_PROVIDER.md)。

原子 artifact writer 只写脱敏 `eval_v2_report.json`、`eval_v2_summary.md`、可选 repetition aggregation 和带 SHA-256/size/source-tree hash 的 manifest。目标必须是 `artifacts/` 下不存在的新目录；先在同父目录 staging 中生成并复核，再原子 rename。路径、API Key/Authorization、final output、raw rows、sample values 和 traceback 字段均被拒绝。所有未冻结产物固定 `model_quality_claim_allowed=false`。

## 4. Provider 与重复运行

冻结最低门槛：

- 至少 2 个已注册 Provider；
- 每个 Provider 固定 `provider_id + model_id + transport_id`；
- 每个 Provider 重复 3 次；
- 每次重复使用预承诺 seed，并随机化题目顺序；
- 每个重复单独报告结果，同时报告跨重复稳定性；
- 不合并不同 Provider 的成功率掩盖差异；
- usage 未返回时保持 unknown，价格模型不完整时成本保持 `null`。

当前只有 `deepseek / deepseek-v4-flash / openai_compatible_responses` 被注册。`second_provider` 仍为 planned；该槽位不能作为已有第二 Provider 证据。

## 5. 冻结与 private holdout 协议

访问 private holdout 前必须固定并记录以下 SHA-256：

1. 源码；
2. system/developer prompt；
3. tool schema；
4. scorer；
5. development/public corpus；
6. split manifest；
7. dataset manifest；
8. dependency lock。

冻结流程：

1. 完成 development 与 public regression 任务及 golden；
2. 完成至少 3 个非 synthetic 数据集的许可与外部复核；
3. 注册第二 Provider 和固定模型配置；
4. 由至少 2 位领域专家复核 goldens，并声明利益冲突；
5. 使用 R 或 SAS 的独立实现复核统计锚点；
6. 生成全部冻结哈希；
7. 外部 custodian 返回不暴露题面/ID/位置的 private corpus commitment；
8. 将 campaign 从 `design_only` 改为 `frozen` 并运行离线校验；
9. 只提交一次 private campaign；每个 Provider 在该 campaign 内运行 3 次；
10. 只接收并发布脱敏聚合结果、commitment、运行配置与审计证明。

如果仍有任一 readiness gap，schema 会拒绝 `status=frozen`，防止把计划写成完成证据。

仓库现提供 [private-holdout custodian kit v1.1](v2/private_holdout_kit/README.md) 与
[操作指南](../docs/PRIVATE_HOLDOUT_CUSTODIAN_KIT.md)。当前是 synthetic conformance 实现：
不同 Ed25519 keys、调用方显式提供的 trust/freeze/candidate/ledger anchors、预访问
`access_reserved` 与 terminal 两阶段 ledger、预承诺 denominator、budget、rate/CI、cell
coverage 和 small-cell suppression 均由离线 verifier 检查，网络调用为 0。

Kit 当前只证明离线协议实现，不改变本页 readiness：campaign 仍为 `design_only`、private
0/50、Provider 1/2、private commitment/authorization 不存在，外部 review 和 R/SAS
cross-check 未完成。`synthetic=false` release 固定拒绝；`check-private-root` 只是时点 metadata
快照，不是持续的访问隔离或授权，`.gitignore` 也不能作为访问授权。

## 6. 外部复核

外部复核至少包含：

- 独立 custodian 持有 private holdout；
- 至少 2 位领域专家审阅任务、golden、统计方向和局限性；
- reviewer conflict declaration；
- 至少一个 R 或 SAS 独立统计实现；
- 对争议题保留 adjudication 记录，但不把 private 内容提交到仓库；
- 在看模型结果前完成 golden 与评分规则。

外部科研用户 pilot 是相邻但不同的证据：pilot 关注真实使用、修订率和专家接受率；Eval v2 关注预注册任务上的可重复质量与安全。两者不应合并成同一成功率。

## 7. 必报指标

必须按 split、dataset、Provider 和 repetition 分层报告：

- task success rate；
- tool selection / argument accuracy；
- evidence citation / numeric claim accuracy；
- clarification / refusal accuracy；
- approval bypass rate；
- unexpected tool error rate；
- completion integrity rate；
- latency P50/P95；
- token usage 与 cost coverage；
- inter-run stability；
- per-dataset 与 per-provider success rate；
- 重复运行聚合结果的置信区间。

延迟来自评测环境，不得称为生产 SLA；private 小样本成绩也不得直接称为未知生产流量泛化。

## 8. 当前 readiness gaps

初始 manifest 故意暴露以下未完成项：

- development 80/80 ready；
- public regression 40/40 ready；
- private holdout 0/50；
- 已注册数据集 1/3，非 synthetic 0/3；
- 已完成来源与哈希核验的外部数据集 3 个，但外部专家复核为 0/3；
- 已注册 Provider 1/2；Anthropic adapter 已完成离线合同，但第二 Provider campaign slot 仍为 `planned`；
- 历史 v1/v2 public candidates 保持可追溯；v3 hashes/commitment 已离线生成但尚未在线运行，完整 private campaign freeze 尚未生成；
- private corpus commitment 尚未提供；
- 外部 golden review 和统计交叉检查尚未完成。

因此当前可声称“Eval v2 设计合同、Completion Telemetry v2 离线实现/校验，以及历史一次性 DeepSeek v1 public candidate run 已完成”；不能把历史结果归给 v2，也不能声称完整 Eval v2 campaign 已 frozen、已有 private holdout、跨 Provider 或未知生产泛化。

## 9. 下一实现批次

接下来按以下顺序推进：

1. 邀请领域专家复核外部数据边界与 goldens，完成 R/SAS 交叉检查；
2. 冻结第二 Provider并安排 private custodian；
3. 为完整 private campaign 完成 custodian commitment 与全部 freeze hash；
4. 在新预算与独立 lock 下完成第二 Provider 的公开集三次运行，不重刷当前 DeepSeek candidate；
5. 预注册并一次性提交 private campaign，最后发布新版本证据。
