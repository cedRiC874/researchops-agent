# ResearchOps Agent — Current Status

> Snapshot: 2026-08-31 (Asia/Shanghai)
> Public Git anchor: `main@ef602ce8aec6364205e0a6537642d2ed646fdb22`
> Tree: `5a01b2e0ba6f7a05cd49faea0bf74768f1e6522f`

本页是给深度评审者看的状态账本，不是首页营销材料。机器合同、版本化 evidence 和冻结 artifacts 是最终事实来源；本页只做可读投影。`not_run`、`failed`、`unknown` 和 `0` 不可互换。

## 1. 当前结论

ResearchOps Agent 已完成 evidence-first 科研数据分析原型、确定性统计与控制面、DeepSeek 单 Provider 在线基线、单机 production-like vertical slice、Pilot staging 工程、synthetic private-custodian kit，以及外部审阅的冻结准备包。

尚未完成的是第二个正式 Provider、独立外部科研用户 Pilot、两位领域专家的实际审阅、独立 R/SAS 复算与比较、外部 custodian 持有的 private 50，以及任何生产 SLA 或未知分布泛化证明。

准确定位：**已有真实但集中于单一 Provider 的在线证据、尚未通过独立外部验证的 research prototype / portfolio。**

## 2. Evidence at a glance

| 层 | 已完成的可复核事实 | 不证明什么 |
| --- | --- | --- |
| 模拟科研分析 | 240 行模拟 RCT；ANCOVA/HC3 与 Welch 生成样本流、效应、CI、p 值、evidence ID 和聚合图 | 临床有效性、真实世界因果效应 |
| 人工审批 | Phase 4 验证暂停、批准、拒绝、过期、scope/source 变化与本地恢复 | Phase 6 批准后的在线恢复 |
| Phase 5 | `offline_deterministic / components_and_control_plane`：50/50、evidence 21/21、model calls 0 | LLM 规划准确率 |
| 审计 | 50 条评测审计链有效；事件链覆盖工具、attempt、错误、审批和副作用 | 外部不可篡改时间戳或完全防止数据库控制者重建 |
| Phase 6 DeepSeek | development 16/16；repo-local holdout 4/4 | 抗污染 holdout、未知请求泛化、生产 SLA |
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

下一条更深证据目标是 **50–60 个独立 Provider-behavior tasks**，而不是继续增加 Provider 名称。它尚未冻结或运行；任何新在线评测必须预先锁定任务、scorer、source、三次顺序、请求/token/成本/总时限上限，并完整报告 attempted/completed/passed/failed 分母、usage、价格依据和延迟分布。

## 4. Provider 状态

| Provider | 当前状态 | 已完成 | 当前边界 |
| --- | --- | --- | --- |
| DeepSeek | Eval v2 唯一正式注册 Provider | Phase 6 与 public-regression 在线证据 | 单 Provider、公开/仓库可见任务；无 private 结果 |
| OpenAI | external_blocked | 最小请求返回 HTTP 429 | 没有有效模型响应或质量结论 |
| Anthropic | `offline_contract_only` | Adapter、CLI、preflight contract 与离线测试 | 没有验证官方 Anthropic credential、Messages、tools 或 usage |
| Kimi | `planned_not_registered` | metadata-only 可见性；Chat/Pilot fail-closed 历史 | 没有成功 Chat/tool/usage/error-semantics evidence |

### Kimi 历史合并记录

| 版本 | 实际发生 | 终态 |
| --- | --- | --- |
| Models preflight / v5 | 官方 `api.moonshot.cn` metadata GET：HTTP 200、1/1 call、exact `kimi-k3` visible、0 model tokens | metadata verified only；不授权 Chat |
| Chat/Pilot v6 | 一次授权、首请求；在本地 usage validation 阶段 fail-closed，0/3 scenarios，0 trusted tool calls，0 tool executions | `failed / planned_not_registered`；授权已消费，不重试 |
| Chat/Pilot v7 | 一次新授权、首个 required-tool 请求；在本地 response validation 阶段 fail-closed，0/3 scenarios，0 trusted tool calls，0 tool executions，0 usage observations | `failed / planned_not_registered`；raw payload 未保存，根因未定，授权已消费 |
| Diagnostic successor v8 | 保持 v7 request bytes/acceptance semantics，只新增固定本地诊断 branch code | `implemented_offline_tested_not_run / online_not_authorized`；v6/v7 已封存，当前没有可执行的在线 successor |

不能把 v8 写成第三次在线失败：v8 没有调用 Provider。也不能把 v6 写成 response-validation failure：v6 的精确阶段是 usage validation。三者不占 README 首页，只在这里保留。

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
| Remote main | `ef602ce8aec6364205e0a6537642d2ed646fdb22`，PR #27 regular merge |
| Main offline | [run 33168768778](https://github.com/cedRiC874/researchops-agent/actions/runs/33168768778)：success |
| Main pilot | [run 33168768834](https://github.com/cedRiC874/researchops-agent/actions/runs/33168768834)：success |
| Latest production E2E | [run 33106332748](https://github.com/cedRiC874/researchops-agent/actions/runs/33106332748) on `b905477…`：success；PR #27 未命中 production path filter |
| Open PR | [PR #26](https://github.com/cedRiC874/researchops-agent/pull/26)：Kimi v8 main-CI evidence；checks success，尚未进入 main |
| Release | `v0.2.0 / phase6-deepseek-v1`；晚于该 tag 的工作尚未发布为新 Release |

### 历史 Phase 5 质量门事故

旧 main run `32568017243` 的 workflow conclusion 曾为 success，但 clean-LF 重建实际只有 44/50、evidence 10/21。根因是 LF/CRLF provenance 漂移，以及 evaluation exit code 被后续 verifier 的零退出码覆盖。PR #3 随后统一 LF lineage、分别保留两个 native exit codes，并加入 `phase5-ci-v1`，精确要求 50/50、failed=0、success rate=1、evidence 21/21。修复后的 main run [32571384757](https://github.com/cedRiC874/researchops-agent/actions/runs/32571384757) 通过，旧绿色 run 不再作为质量证据。

## 9. Allowed and forbidden claims

可以准确表述：

- Phase 5 证明确定性组件、evidence binding 与控制面在冻结语料上的结果；model calls=0。
- DeepSeek Phase 6 是 20 个仓库可见任务的小样本在线基线。
- DeepSeek Eval v2 public v1 是锁定系统在 31 个公开 Provider-behavior tasks、三次重复上的 68/93。
- Production-like slice 已通过真实单机 Compose E2E。
- Kimi/Anthropic 的失败与未运行状态被保留；上述受控 Models-preflight 与 Kimi Chat/Pilot 路径在已观测错误下 fail-closed。

不得表述：

- Phase 5 50/50 是 LLM 规划准确率。
- DeepSeek 16/16、4/4 或 68/93 证明未知生产集泛化。
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
- [DeepSeek Eval v2 public evidence](docs/evidence/eval-v2-public-regression-deepseek-v1/README.md)
- [Kimi v6 failure](docs/evidence/kimi-controlled-pilot-usage-failure-v1/README.md)
- [Kimi v7 failure](docs/evidence/kimi-controlled-pilot-v2-response-failure-v1/README.md)
- [External-review package](evals/v2/external_review_pre_results_v2/README.md)
- [Private custodian guide](docs/PRIVATE_HOLDOUT_CUSTODIAN_KIT.md)
