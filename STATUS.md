# ResearchOps Agent — Current Status

> Snapshot: 2026-09-04 (Asia/Shanghai)
> Implementation base Git anchor: `main@20ad1da04aac9874f87d48ebe1ef2df1daa6245c`
> Base tree: `c4bd9afbed0906d3dfc23f42d3590f1af9643a42`

本页是给深度评审者看的状态账本，不是首页营销材料。机器合同、版本化 evidence 和冻结 artifacts 是最终事实来源；本页只做可读投影。`not_run`、`failed`、`unknown` 和 `0` 不可互换。

## 1. 当前结论

ResearchOps Agent 已完成 evidence-first 科研数据分析原型、确定性统计与控制面、DeepSeek 单 Provider 在线基线、单机 production-like vertical slice、Pilot staging 工程、synthetic private-custodian kit，以及外部审阅的冻结准备包。

尚未完成的是第二个正式 Provider、独立外部科研用户 Pilot、两位领域专家的实际审阅、独立 R/SAS 复算与比较、外部 custodian 持有的 private 50，以及任何生产 SLA 或未知分布泛化证明。

准确定位：**已有真实但集中于单一 Provider 的在线证据、尚未通过独立外部验证的 research prototype / portfolio。**

## OPEN ENGINEERING DEFECT — Provider-native completion telemetry is not live-validated

状态：`open / cross-cutting / causal attribution incomplete`。

同一个可观测性缺口跨越两份独立 evidence bundle：

- [Kimi controlled Pilot v2 failure](docs/evidence/kimi-controlled-pilot-v2-response-failure-v1/README.md)：未保留 raw response body；`causal_root_cause=undetermined_without_raw_provider_payload`。
- [DeepSeek Depth-60 output-cap audit](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/README.md#output-cap-attribution)：未保留 top-level response status、`incomplete_details` 或 Provider-native `finish_reason`；除了已标记的 DEV-043/060，silent truncation 不能被严格排除。

要求的修复不是保存完整 prompt/response body。Provider adapter 应把经过 allowlist、大小限制与脱敏的 response-level `status`、`incomplete_details`、native `finish_reason` 和 usage 一起写入 append-only event chain，并绑定 adapter/contract version。该修复只能改善后续运行的归因；不能追溯恢复既有 Kimi 或 Depth-60 payload，也不授权重跑已见任务。

当前 runtime 接线已完成 local offline implementation 与完整 diff 审阅，但尚未执行 live validation：generic Eval v2 与 self-pilot 尚未接入 verified runtime denominator plan、case-scoped telemetry session 和同一事件 usage，因此真实 Provider 路径在保存或使用 Key、构造 Provider client 之前以 `eval_v2_completion_telemetry_session_required` fail-closed。确定性测试仅在测试进程内局部替换 runner resolver；CLI 与生产模块不提供授权铸造入口，该测试替换不授权真实 SDK Runner、在线调用、历史回填或新质量主张。

T1–T4 已完成本地离线接线与审阅，尚未完成 live validation：T1 固定 record/missing/version/closure 合同；T2 实现写入前脱敏、UTF-8/结构限界、surface-aware mapping、opaque runtime authority 与动态分母；T3 在 Provider Adapter、Agent、append-only ledger bridge 和受控 runner 之间传递 case-scoped session，并把 accepted response 与 usage 写在同一个 reserved event；T4 使用 MockTransport、隐私 canary、终态状态机、双索引/SDK reconciliation 和 hash-chain tamper 测试覆盖这些路径。后续 T6-A 离线审计又修复了两个关闭门缺陷：任一预注册 case 没有 accepted response 时不再允许整批空过，动态 denominator 子产物也可从磁盘重新验证；`unmapped` 现在使用唯一的原子 terminal event，并与 normalized state 双向绑定。规范性 hardening successor 明确这些改动仍是 offline-only：外部预注册绑定、完整的 outer closure/ledger verifier、Adapter-path first-live validation 与 registry promotion 仍未完成。它们没有执行新的 Provider 请求或评测，也没有恢复 Kimi/Depth-60 的历史归因，不能据此关闭本缺陷、注册第二 Provider 或产生新的准确率数字。真实关闭仍要求预注册的全新未见任务、实际 live telemetry 完整分母、有效事件链及全部 `truncation_signal_source=native_status`。

T6-B 已通过 PR #38 合并到 `main@5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab`；merge tree 与固定审阅 head `cdcf4b0ef87e3f6054e0e74030b4f2acbe0d177a` 的 `30ecfd86ac00aecd8b67305a7c6ed2af88eeee10` 完全相同，合并后的 offline-quality-gate、pilot-staging-ci 和 production-slice-e2e 均成功，但尚未在线执行。已确认的 design commitment 为 `ddff10f30031faf77d6417dd695dd61dae4c6a45334efae7388ab4f2adc4a5bc`，implementation successor commitment 为 `187bb6b537f2bdffb4bf77581550ea0ca81d02fb52d44d110b22a450ae0dce10`。固定 commit 与后续完整 diff 审阅发现的 evidence/verifier blocker 已在 successor 中收紧：run 与 verify 都必须接收 artifact 外保存的 authorization binding v2；calculator/run/verifier 共用 CNY 1 最坏成本预留；verifier 从无 replace/no-lazy-fetch 的 execution-commit 快照重算完整 v5；full/manifestless 共用精确 ledger parser；terminal Key/cleanup/error 必须与执行阶段一致；manifestless 文件只能是固定 writer 前缀且每个已有 JSON 都验证 schema 与交叉投影；只有 started/send/record usage 完整双射才允许 top-level usage/cost 非空。受支持的公开 run API 不暴露 Key、clock、Git、artifact-root 或 transport 测试 seam；validation-only authority 不能进入普通 Ledger、generic Phase 6 或 Eval v2。330 秒 in-process 上限在同一预算内预留末 30 秒用于清理与终态，但无法抢占 kernel-blocked 同步 `fsync`；若威胁模型包含主机文件系统永久卡死，仍需外部进程 supervisor。固定运行仍必须显式绑定上述 merge commit、v5 source-integrity commitment、同日官方定价确认和新的单次用户授权。成功也只允许两个 completion projection shape 的 Adapter-path 归因，`closure_claim_allowed=false` 且不得自动提升 registry；当前四个 Provider triple 仍全部 `runtime_binding_allowed=false`。

T6-C 的 external preregistration 字段合同以 `main@5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab` 为基线提交独立 contract-only 审阅；本提交不含 verifier 实现，尚未合并或在线运行：design 为 65,751 bytes，文件 SHA-256 `3b16e6d65ec7030fb1f271b57cbde369a55fbf4f966ad57cb73e591d5333d61d`，semantic commitment `480a8511d35d295d8a99ba7561a19f742b189094dec589bbd8317899c1f833c8`；closure-evidence supporting contract 为 39,575 bytes，文件 SHA-256 `2c904737b2776ac1df2358df86e320e6421c6eed17b48ab97d49f7512d37e439`，semantic commitment `a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0`。14 个 strict schema 覆盖 candidate freeze、seen exclusion、外部 observation/ledger、授权消费、hidden synthetic task、transport send、postrun facts、完整或 prefix-only closure evidence；合同及回归验证以本 PR 固定 head 的离线 checks 为准。signed receipt 只保存 `pre_anchor_closure_eligible`，只有最终 verifier 在验证后续 witness entry 与外部 observation 后才可返回 `closure_claim_allowed`；quarantine 只能返回失败已封账，不得发布 artifact hash 或声称复验隔离内容。当前仍没有 external custodian task、真实签名、registry successor、one-time grant、Provider Key load 或网络调用，STATUS 缺陷保持 `open`。

### Depth-60 source-bundle successor（offline source integrity only）

历史 v1 plan 保持 5,398 bytes、文件 SHA-256 `f5d43283e3506663383359d24736bd3b82a910e45cc092954f94d86a80e6cd20`、plan commitment `8019ef294b5028ab4e44c006f01e02bddb5a3b67b1ed88b84945bf37e75c216e` 与 source-bundle commitment `914acbe89f4d99240aa653ecfe07fc0a2c129d08aa6abee9eb401e5f9d7a8d84`，未被覆盖。当前树与其冻结源码不同，因此历史 plan 在当前树上应以 `phase6_depth60_component_drift` fail-closed；这不是历史 commitment 失效。

v2 算法使用独立 domain，纳入直接子模块导入会执行的每一级 package `__init__.py` 及其依赖，并覆盖 namespace-package fromlist 子模块，保留 whole-package conservative closure，在相对导入逃逸 `researchops` 时拒绝。validator 只接受 v1/v2/v3/v4/v5 五个固定 plan 路径，并重新计算 predecessor 的完整 commitment。冻结 v2 source-bundle SHA-256 为 `cd46dc03771fc0ebca7ea50798fe2b32fa76248882881f7249c777cd3270ab25`。

冻结的 `phase6-deepseek-depth60-v2` plan 保持 2,170 bytes、文件 SHA-256 `fc4ca5cc2131efb36d82f1d739f65ad2a026e1c7534f0da9c873942a40c1002f`、plan commitment `3077a55e09f3f2137155a68d96a5bda60d8553cc9b5dd36ca83d33bbbc3dcf7e`；它对当前新增 telemetry/Adapter 树报告 component drift 是预期历史状态，不应被误判为 plan 损坏。

`phase6-deepseek-depth60-v3` 已在 PR-A/PR-B 字节稳定后重新生成并通过离线验证：plan 2,850 bytes，文件 SHA-256 `5602f940a8627b9c785a1b785d757119a616050ebbc2e31e9dd26aacf448c05e`，plan commitment `979202e96a5304ad1ba73c54e55f0d38f80baedf8278e37ea8535bb5560ce6af`；source/runtime/contract 三组件分别为 `f5af13c7475f7c152a3cbe2053c7b0f81f6d4b15034e8a58786ab9e7124a19bf`、`694c948a1d79fd38532304846577a94a4c75ca1410dd97c363df71a4ba944a63`、`c9c54c425932770254b9f460d7ab5120401ba02f6802626fb7399d3333700011`。它仍只允许作为离线源码/合同完整性承诺，不重验历史结果、不能作为在线授权；runner 必须以 `phase6_depth60_successor_plan_not_executable` 拒绝。因此当前仍没有可执行的 Depth-60 plan，也没有发生新的 Provider 调用。

T6-A hardening 改变了 v3 绑定的源码、runtime 与 contract，因此 v3 在当前树上报告 component drift 是预期历史状态。`phase6-deepseek-depth60-v4` 是只增不改的 offline hardening 后继：plan 3,087 bytes，文件 SHA-256 `ae961e069afa9c842c294f7fb6951e0cf3a4ad86dfcdd16cb96a2c264c232956`，plan commitment `c36dc0dd0487aa350dc2bd636b45bb494381e0c732c80be7b410be4b9beda612`；source/runtime/contract 三组件分别为 `4bd5a48be256b124a7c297f5f98ef4bbadd09df07a038067d7aca211e1fc772c`、`b0e6f0feb3af416ca73f04df9e8c1cc7f10b5c700e2a20c2a3a7c688273f42c2`、`1e4028e3bc9c128391a4a73c6753bb5da5e9d19079cbf1068d9acb64980fa56f`。v4 仍只允许作为离线源码/合同完整性承诺，不重验历史结果、不能作为在线授权；runner 必须以 `phase6_depth60_successor_plan_not_executable` 拒绝。因此当前仍没有可执行的 Depth-60 plan，也没有发生新的 Provider 调用。

T6-B 控制面使 v4 对当前树报告 component drift；v4 的 3,087 bytes 与文件 SHA-256 `ae961e069afa9c842c294f7fb6951e0cf3a4ad86dfcdd16cb96a2c264c232956` 保持不变。`phase6-deepseek-depth60-v5` 是新的 offline source-integrity 后继：plan 3,120 bytes，文件 SHA-256 `ff39dd5a1aa09b7bc92b27f9d800b5d51fbd2fd69c2a599b5bf0f25aed490aae`，plan commitment `8a5474db1e9ad59d501bf109d4a7ecbf616f40599763a20188581e336d379bd7`；source/runtime/contract 三组件分别为 `42ad232ad73792453ff6025d836cd3449d27974a633a1a59a019023d676c64a5`、`606913e5570769e8b5aa430c621c5761d557e747c8b3f82eca67e20540b97acf`、`7c2bcfba1d7f6d2195ec389c7f98e1ef6e2ef400c6d7101292f6bbc77942bc15`。其 scope 精确限定为枚举的 source dependency closure 加显式 telemetry runtime/contract bundles，而不是 whole-repository commitment。v5 同样 `online_execution_authorized=false`、`network_calls=0`、`model_calls=0`，不能替代 first-live 的单次用户授权。

历史 rejection-audit artifact 仍绑定旧的 3,301-byte `phase6_source_bundle.py` 与 v1 bundle hash；历史 evidence 文件未修改。其默认 verifier 在当前 successor 树上应以 `source commitment mismatch` 拒绝，而不是把当前 source 冒充历史 source。测试同时锁住这条 current-tree 拒绝，并在显式 test-only historical binding 下继续复核历史 artifact 的 1,390/1,541 项派生比较；该测试注入只使用 artifact 已记录的旧 commitment，不声称当前文件具有历史字节。要做默认有效重放，仍需历史绑定 checkout 或独立保留的历史 source bytes。

## 2. Evidence at a glance

| 层 | 已完成的可复核事实 | 不证明什么 |
| --- | --- | --- |
| 模拟科研分析 | 240 行模拟 RCT；ANCOVA/HC3 与 Welch 生成样本流、效应、CI、p 值、evidence ID 和聚合图 | 临床有效性、真实世界因果效应 |
| 人工审批 | Phase 4 验证暂停、批准、拒绝、过期、scope/source 变化与本地恢复 | Phase 6 批准后的在线恢复 |
| Phase 5 | `offline_deterministic / components_and_control_plane`：50/50、evidence 21/21、model calls 0 | LLM 规划准确率 |
| 审计 | 50 条评测审计链有效；事件链覆盖工具、attempt、错误、审批和副作用 | 外部不可篡改时间戳或完全防止数据库控制者重建 |
| Phase 6 DeepSeek | development 16/16；repo-local holdout 4/4 | 抗污染 holdout、未知请求泛化、生产 SLA |
| DeepSeek Depth-60 online v1 | 60/60 completed；严格 composite protocol 20/60；103 requests；290,339 tokens；P50/P95 7.52/16.77 s；估算 CNY 1.197393 | 语义准确率、英语能力、private/未知分布泛化、生产 SLA、模型单体质量或 Provider 实际账单 |
| Eval v2 DeepSeek public v1 | 31 个 Provider-behavior tasks × 3：93/93 完成、68/93 通过；141 model requests | 模型单体准确率、private holdout、完整 Eval v2 |
| Deterministic fault channel | 9 个本地 fault tasks × 3：27/27 | Provider 或模型质量；该分母与 68/93 分开 |
| Production-like slice | FastAPI → PostgreSQL lease queue → MinIO/S3 → OTel 的真实单机 Compose E2E | HA、云 IAM/KMS、备份恢复、生产负载与 SLO |
| Pilot staging | 邀请、consent、session、queue、withdraw、DLP、retention 与 aggregate-only summary 合同通过 | 正式外部用户可用性或专业正确性 |
| Supervised regression | 同一参与者：1 completed、6/6 terminal、4 feedback、2 technical failures、0 safety incidents | 独立外部参与者或 external validation |
| Eval v2 public foundation | 80 development + 40 public regression tasks 已 internal-ready；三个公开数据集的来源、许可与 selected-asset hash 已验证并被 candidate manifest 绑定 | 外部 golden review、campaign 层数据集注册或完整 freeze |
| Private custodian kit v1.1 | Ed25519 角色、外部 anchors、两阶段 ledger 与 synthetic aggregate verifier | 真实 private corpus、private 运行或 non-synthetic release |
| External-review package | 已冻结统一 package、role deliveries、75-field comparator、schemas 与签名说明 | 已邀请、已审阅、R/SAS 已执行或 evidence 已形成 |
| OpenAI | Adapter/错误路径已验证；最小在线请求实际到达 Provider 后返回 HTTP 429 | 有效模型响应、质量、成本或正式 Provider 注册 |
| Anthropic | CLI/offline adapter、Models preflight 合同与 MockTransport 测试 | 官方 Key 可用性、Messages/tools/usage/质量；post-lock 官方 origin 请求使用错误 CCTK token并返回 403 |
| Kimi Models metadata | 一次官方中国区 GET 为 HTTP 200，exact `kimi-k3` visible，0 generation tokens | Chat/tools/usage/cost/质量或正式 Provider 注册 |
| Kimi Chat/Pilot line | v6/v7 各一次首请求 fail-closed；v8 为离线诊断 successor，未运行且不可在线执行 | 成功兼容、根因归属、实际账单或模型质量 |
| Kimi K3 non-streaming handshake | 一次 synthetic attempt 在首请求 `kimi_chat_tool_protocol_invalid` fail-closed；1 call、283/116 tokens、0 tool execution、估算 CNY 0.017260 | Chat/tool/usage/error 兼容、模型质量、Provider 注册或 non-synthetic/private 能力 |

## 3. DeepSeek 在线基线

### Phase 6

| Split | 结果 | Requests | Input / output / total tokens | Agent P50 / P95 |
| --- | ---: | ---: | ---: | ---: |
| Development | 16/16 | 28 | 57,723 / 13,316 / 71,039 | 7.65 / 17.40 s |
| Repo-local holdout | 4/4 | 6 | 13,051 / 3,803 / 16,854 | 5.05 / 14.59 s |

Holdout 只有 4 题，任务和 golden 位于仓库内，且不含审批题。结果不能外推为未知生产集泛化。

Phase 6 没有绑定完整版本化价格表：`pricing.status=not_provided`、`cost.status=unavailable`、cost coverage `0`。不能把 token 数换算成已核验账单或零成本。

### Eval v2 public-regression v1

- Run: `PUBREG-5A75025255EC4443`
- Candidate commitment: `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`
- Provider behavior: 93/93 completed，68 passed，25 failed，case success rate 73.12%
- Repetitions: 23/31、22/31、23/31
- Agent latency P50/P95 by repetition: 4.921/13.093 s、5.900/10.180 s、5.687/13.493 s
- Stable across all three repetitions: 21/31 tasks
- Usage: 141 model requests，143,666 input tokens，53,016 output tokens
- Conservative estimated cost: CNY 0.908142；该估算使用当时峰时、全部 input cache miss 的保守价格，不是 Provider 账单硬上限
- Fault harness: 27/27，本地 deterministic channel，不计入模型分母

证据：[summary](artifacts/eval_v2_public_regression/deepseek-v1/public_regression_summary.md) · [report](artifacts/eval_v2_public_regression/deepseek-v1/public_regression_report.json) · [manifest](artifacts/eval_v2_public_regression/deepseek-v1/artifact_manifest.json)

### Depth-60 agent evaluation (`deepseek-v4-flash`, development split)

```text
strict end-to-end protocol pass    20/60 (33.33%)
completed / not started            60 / 0
holdout executed                   0
```

Strict end-to-end protocol result. Not semantic accuracy, not an English-capability score, and not a model-quality score.

#### Post-hoc interpretation audit v1

No rerun, no Provider call, and no official rescore:

- Mechanically, all 40 failure lists include `required_phrases`; only 38 delivered content-bearing
  final answers. DEV-043/060 are byte-empty, making their phrase and missing-ASSERT failures
  vacuous and textually non-discriminating. All 34 phrase-only failures are content-bearing.
- Applying the locked rejection predicates to all 40 failures gives the mutually exclusive
  first-gate histogram `ASSERT inventory 20 / numeric prose 12 / plain literal missing 3 /
  assignment prose 2 / enum prose 2 / malformed ASSERT 1`. Among the 34 phrase-only failures,
  30 contain every required literal and are then rejected by the structured contract; four omit
  at least one literal. The complete phrase-only/mixed × literals-present/missing cross-tab is
  mechanically `30 / 4 / 3 / 3`. Removing the two byte-empty rows for textual interpretation gives
  `30 / 4 / 3 / 1`, with two delivery failures reported separately. The plain-literal first gate
  likewise decomposes from mechanical 3 to one content-bearing omission plus two empty deliveries.
- Literal presence is not semantic correctness. Thirty-nine rows are byte-identical to the
  scorer-input hashes; `P6-DEV-057` is a sanitized path-redaction projection whose raw branch set
  cannot be recovered because the audit database retained only its hash.
- The audit reports at least three distinct, non-exhaustive findings: a content-bearing ASSERT
  instruction-following deficit (20/38 sanitized projection; 19/37 byte-exact), the demonstrated
  five-task lexical matcher defect, and two byte-empty delivery failures. It does not attribute all
  40 failures to either the scorer or ordinary instruction-following.
- `guardrail_accuracy` and `required_phrases` pass the same 20 tasks in this run. The composite carries no additional separation beyond that component here; this is run-level metric redundancy and a metric-design defect, not a standalone behavior finding. Outcome is 58/60 and safety is 60/60.
- Clarification/refusal 3/13 is not a stop-decision failure: outcome 13/13, zero tool calls 13/13, and safety 13/13. The deficit is exact marker/reason/internal-ID output-contract conformance.
- `ancova` and `itt-boundary` are 0/4 strict task pass each: three phrase-only failures plus shared DEV-043, whose final response was exact-cap incomplete and whose final answer was byte-empty. Tool sequence and arguments are 4/4 in both tags; locked completion/outcome/evidence/numeric check booleans are 3/4, with some evidence/numeric passes vacuous when no units were required. Deterministic ANCOVA did not fail.
- The English-tag aggregate gap, 2/21 versus 18/39, is cohort-confounded. In the comparable DEV-017..060 cohort the result is 2/21 versus 2/23; two-sided Fisher exact `p=1.0`. A language effect is not identifiable in this run.
- Five tasks, DEV-026/028/030/032/044, expose a `95%` lexical matcher defect. Natural standalone `95%` prose is rejected, but the contrived `x95%` token passes substring matching while evading the numeric boundary. The deterministic counterexample makes all five full `task_pass=true`; therefore the earlier “formally unsatisfiable” premise and its ≤55/60 argument are invalid. This does not prove an actual ceiling above 55 or joint 60/60 reachability.
- The counterexample records the complete generation `sys.version` and pins replay semantics to
  Python patch 3.12.13 plus Unicode database 15.0.0; patch/Unicode drift is rejected before scorer
  replay, while same-patch platform build strings preserve the recorded provenance. Windows main CI
  uses available Python 3.12.10; a separate Ubuntu/Python 3.12.13 job performs canonical replay.
- Output cap: 2,000 is per response, not per task. Across 103 responses, 101 were completed and two were incomplete. Only DEV-043/060 hit the cap exactly, and both delivered byte-empty final answers whose projection/scorer-input SHA is the empty-string digest; the next highest was DEV-044 at 1,821. No additional truncation was observed, which is not the same as excluded.
- DEV-044 is observationally confounded: it is both the only other near-cap response and one of the five lexical-defect tasks. Its live failure alone cannot adjudicate near-cap versus output-contract effects. Telemetry records it as completed; the synthetic counterexample independently isolates the matcher behavior.
- Diagnostic ablation that ignores only `required_phrases` yields 54/60. It exposes score
  concentration but is not an official rescore, corrected accuracy, or meaningful accuracy:
  the implemented check is not a valid numeric-presentation measurement instrument.

Frozen execution identity remains unchanged: Plan `phase6-deepseek-depth60-v1`, commitment `8019ef294b5028ab4e44c006f01e02bddb5a3b67b1ed88b84945bf37e75c216e`, scope `P6-DEV-001..060` once each, 103 requests, 235,943 input tokens, 54,396 output tokens, CNY 1.197393 conservative estimate, P50/P95 7.52/16.77 s, 60 valid audit chains, and no holdout execution.

证据：[sanitized summary](docs/evidence/phase6-deepseek-depth60-v1/README.md) · [public projection](docs/evidence/phase6-deepseek-depth60-v1/public_summary.json) · [opaque commitments](docs/evidence/phase6-deepseek-depth60-v1/artifact_commitments.json) · [post-hoc interpretation audit](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/README.md) · [rejection histogram](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/rejection_reason_histogram.svg) · [machine rejection projection](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/rejection_reason_histogram.json) · [95% lexical counterexample](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/95_percent_lexical_counterexample.json)。该结果不得用于修改同一 prompt/scorer/tool schema/task selection 后重跑，也不能覆盖锁定的 20/60 总结果。

## 4. Provider 状态

| Provider | 当前状态 | 已完成 | 当前边界 |
| --- | --- | --- | --- |
| DeepSeek | Eval v2 唯一正式注册 Provider | Phase 6、public-regression 与 Depth-60 在线证据 | 单 Provider、公开/仓库可见 development；无 private 结果；20/60 不外推 |
| OpenAI | external_blocked | 最小请求返回 HTTP 429 | 没有有效模型响应或质量结论 |
| Anthropic | `offline_contract_only` | Adapter、CLI、preflight contract 与离线测试 | 没有验证官方 Anthropic credential、Messages、tools 或 usage |
| Kimi | `planned_not_registered` | metadata-only 可见性；v6/v7 fail-closed 历史；新 non-streaming handshake 首请求 fail-closed | 没有成功 Chat/tool/usage/error-semantics evidence；授权已消费、禁止重试 |

### Kimi 历史合并记录

| 版本 | 实际发生 | 终态 |
| --- | --- | --- |
| Models preflight / v5 | 官方 `api.moonshot.cn` metadata GET：HTTP 200、1/1 call、exact `kimi-k3` visible、0 model tokens | metadata verified only；不授权 Chat |
| Chat/Pilot v6 | 一次授权、首请求；在本地 usage validation 阶段 fail-closed，0/3 scenarios，0 trusted tool calls，0 tool executions | `failed / planned_not_registered`；授权已消费，不重试 |
| Chat/Pilot v7 | 一次新授权、首个 required-tool 请求；在本地 response validation 阶段 fail-closed，0/3 scenarios，0 trusted tool calls，0 tool executions，0 usage observations | `failed / planned_not_registered`；raw payload 未保存，根因未定，授权已消费 |
| Diagnostic successor v8 | 保持 v7 request bytes/acceptance semantics，只新增固定本地诊断 branch code | `implemented_offline_tested_not_run / online_not_authorized`；v6/v7 已封存，当前没有可执行的在线 successor |
| K3 non-streaming handshake v1 | 一次独立授权 synthetic attempt；首个模型响应 usage 已记录，但本地 tool protocol validation fail-closed，第二请求/tool/400 probe 均未执行 | `failed / kimi_chat_tool_protocol_invalid / planned_not_registered`；1 call、0 tool execution、授权已消费且不重试 |

不能把 v8 写成第三次在线失败：v8 没有调用 Provider。也不能把 v6 写成 response-validation failure：v6 的精确阶段是 usage validation。新的 handshake 不继承 v6/v7/v8 结果或授权；它已经独立失败，仍不能写成“Kimi 已成功”。证据：[sanitized outcome](docs/evidence/kimi-k3-handshake-v1/README.md) · [public projection](docs/evidence/kimi-k3-handshake-v1/public_receipt_projection.json)。

## 5. Eval v2、Pilot 与 Private Holdout

```text
campaign                    design_only
development tasks           80/80 internal-ready
public regression tasks     40/40 internal-ready
external private holdout    0/50
registered Providers        1/2
external domain reviews     0/2 completed
independent R/SAS run       not run
comparison receipt          not run
```

完整 campaign 要求至少两个 Provider、每个 Provider 三次重复、两位外部领域专家、独立 R/SAS 实现与比较、外部 custodian 持有的 private 50。当前任何 Key 的存在都不会自动改变这些状态。

正式 external researcher Pilot 还要求 3–5 位独立参与者和预注册互动/覆盖门槛。现有 supervised regression 永久不计入 external validation。

Private custodian kit 当前是 synthetic conformance kit，不是实际 private runner：private corpus commitment 缺失、access authorization=false、real run=not run、non-synthetic release=denied/fail-closed。

## 6. External review preparation

- Public package anchor: `main@ef602ce8aec6364205e0a6537642d2ed646fdb22`
- Package commitment: `15bf3930e8073c8e7adac9d4892a117f3e433fd72f33cca88f770d306d4d8ebc`
- Domain A/B delivery: `a2c3b9ee2f27438852ebddb3ed4ff5bb577e4230d3b4d461ebb65acfd5ec724e`
- Statistical reviewer delivery: `2a15852ab70edfd70b45670936413a44e0cab3e7619ba35fe820b0e1f4a1efac`
- Comparison verifier delivery: `a87f7175f64a0f39766b4a3bb5a50618b5bffd9bb0cee82c36b7a87bb2baf02c`

机器状态：`frozen_pre_results_not_invited_not_run_not_evidence`。

Operator-side observation（只读核验于 `2026-08-31`）：四封 Gmail 草稿仍存在且未发送，并写入上述 Git/package/role commitments。该事实不由仓库 package 或 ledger 自身证明；补偿、邀请回复截止时间、审阅截止时间、external custodian、安全回传渠道和发件人身份尚未填写。pre-invitation governance anchor、第五位 governance verifier、资格/利益冲突核验与私有 roster/ledger commitments 也尚未完成。

## 7. Candidate lineage

这些 commitments 用于冻结和历史追溯，不代表后继版本继承前代在线结果。

| Candidate | Commitment | Predecessor | 在线结果继承 |
| --- | --- | --- | --- |
| v1 DeepSeek public | `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11` | — | 自身运行已完成 |
| v2 Completion Telemetry | `1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5` | v1 | false |
| v3 Anthropic offline | `22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9` | v2 | false |
| v4 Anthropic preflight | `1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7` | v3 | false |
| v5 Kimi metadata | `105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc` | v4 | false |
| v6 Kimi Chat/Pilot v1 | `57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641` | v5 | false |
| v7 Kimi Chat/Pilot v2 | `2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5` | v6 | false |
| v8 diagnostic successor | `b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c` | v7 | false |

## 8. Git、CI 与发布

| 项目 | 当前事实 |
| --- | --- |
| Remote main | `66f8c52dbe211adc8db9b9d3097f70bebff5f7ce`，PR #30 regular merge |
| Main offline | [run 33418902259](https://github.com/cedRiC874/researchops-agent/actions/runs/33418902259) on `66f8c52…`：success |
| Main pilot | [run 33418902346](https://github.com/cedRiC874/researchops-agent/actions/runs/33418902346) on `66f8c52…`：success |
| Latest production E2E | [run 33418902197](https://github.com/cedRiC874/researchops-agent/actions/runs/33418902197) on `66f8c52…`：success |
| Open PR | [PR #26](https://github.com/cedRiC874/researchops-agent/pull/26)：Kimi v8 main-CI evidence；checks success，尚未进入 main |
| Release | `v0.2.0 / phase6-deepseek-v1`；晚于该 tag 的工作尚未发布为新 Release |

### 历史 Phase 5 质量门事故

旧 main run `32568017243` 的 workflow conclusion 曾为 success，但 clean-LF 重建实际只有 44/50、evidence 10/21。根因是 LF/CRLF provenance 漂移，以及 evaluation exit code 被后续 verifier 的零退出码覆盖。PR #3 随后统一 LF lineage、分别保留两个 native exit codes，并加入 `phase5-ci-v1`，精确要求 50/50、failed=0、success rate=1、evidence 21/21。修复后的 main run [32571384757](https://github.com/cedRiC874/researchops-agent/actions/runs/32571384757) 通过，旧绿色 run 不再作为质量证据。

### Depth-60 解释草稿撤回记录

本地未提交草稿曾提出 DEV-026/028/030/032/044 `formally unsatisfiable`，并据此推导严格
上限 ≤55/60；机器反例随后证明非自然 `x95%` 逃逸可使五题完整通过，两个主张均已撤回。
Git 历史审计未发现含该主张的 commit、remote-tracking ref、branch reflog entry 或本地可见
PR head，因此状态是 `withdrawn_before_publication`，`superseded_public_commit=null`。仍保留
[显式纠正记录](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/README.md#interpretation-correction-record)
和[机器反例](docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/95_percent_lexical_counterexample.json)；若以后发现遗漏的公开副本，必须追加其不可变锚点，不得覆盖本记录。

## 9. Allowed and forbidden claims

可以准确表述：

- Phase 5 证明确定性组件、evidence binding 与控制面在冻结语料上的结果；model calls=0。
- DeepSeek Phase 6 是 20 个仓库可见任务的小样本在线基线。
- DeepSeek Eval v2 public v1 是锁定系统在 31 个公开 Provider-behavior tasks、三次重复上的 68/93。
- DeepSeek Depth-60 是冻结控制面在 60 个仓库可见 development tasks 上的一次性严格端到端协议结果：20/60；失败、usage、成本与延迟分母完整。该 composite rate 受 `required_phrases`/ASSERT 合同主导，不是语义准确率。
- Production-like slice 已通过真实单机 Compose E2E。
- Kimi/Anthropic 的失败与未运行状态被保留；上述受控 Models-preflight 与 Kimi Chat/Pilot 路径在已观测错误下 fail-closed。
- Kimi K3 non-streaming handshake 证明一次 synthetic attempt 在首响应 tool-protocol validation 处 fail-closed；不证明兼容。

不得表述：

- Phase 5 50/50 是 LLM 规划准确率。
- DeepSeek 16/16、4/4 或 68/93 证明未知生产集泛化。
- DeepSeek Depth-60 20/60 证明未知生产集泛化、模型单体质量、生产 SLA 或实际账单。
- English-tagged 2/21 证明系统英语能力较差；同 cohort 对照为 2/23，现有总分组受历史 16/16 混杂。
- Post-hoc `required_phrases` ablation 的 54/60 是正式修正准确率或新的质量 headline。
- 当前实现的 `required_phrases` 是有效的 numeric-presentation discipline 测量工具，或可通过
  正向、反向、删除该检查得到有意义的准确率（包括 54/60）：它会拒绝正确的常规表达，
  却接受没有沟通价值的非自然词法逃逸。唯一可辩护的新数字必须来自预注册的替代 evaluator
  和全新、未见任务。
- Depth-60 的 40 个失败全部由 scorer 缺陷造成，或全部是普通 instruction-following 缺陷：
  机械 first-gate 直方图中的 plain-literal 3 含两条 byte-empty delivery failure；去除两条空串
  后，content-bearing 38 题中 ASSERT inventory 为 20、普通 literal omission 为 1，另有五题
  已证明的词法 matcher 缺陷。这三项不是穷尽、互斥的 40 题因果划分。
- `ancova` 或 `itt-boundary` 0/4 表示确定性统计计算、工具规划、证据绑定和边界判断全部失败。
- `clarification_refusal_accuracy=3/13` 表示模型在 10 题中没有停下、没有询问或继续调用了工具；实际 outcome 与零工具均为 13/13。
- 除 DEV-043/060 外不存在任何截断；冻结遥测只能证明没有观察到第三个截断信号。
- Kimi 已验证 Chat/tools/usage/error semantics 或已注册。
- Anthropic 已成为正式第二 Provider。
- External review、R/SAS cross-check 或 private holdout 已执行。
- Private kit 已支持真实 non-synthetic release。
- Production-like Compose E2E 等同于生产 SLA、HA 或云安全验证。

## 10. Evidence map

- [Evidence index](docs/EVIDENCE.md)
- [Architecture and security boundaries](docs/ARCHITECTURE.md)
- [Portfolio narrative](docs/PORTFOLIO.md)
- [Eval v2 design](evals/EVAL_V2.md)
- [DeepSeek Phase 6 evidence](docs/evidence/phase6-deepseek-v1/README.md)
- [DeepSeek Depth-60 evidence](docs/evidence/phase6-deepseek-depth60-v1/README.md)
- [DeepSeek Eval v2 public evidence](docs/evidence/eval-v2-public-regression-deepseek-v1/README.md)
- [Kimi v6 failure](docs/evidence/kimi-controlled-pilot-usage-failure-v1/README.md)
- [Kimi v7 failure](docs/evidence/kimi-controlled-pilot-v2-response-failure-v1/README.md)
- [Kimi K3 handshake failure](docs/evidence/kimi-k3-handshake-v1/README.md)
- [Combined online evidence manifest](docs/evidence/online-depth60-kimi-20260901/evidence_manifest.json)
- [External-review package](evals/v2/external_review_pre_results_v2/README.md)
- [Private custodian guide](docs/PRIVATE_HOLDOUT_CUSTODIAN_KIT.md)
