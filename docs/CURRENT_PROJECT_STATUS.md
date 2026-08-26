# ResearchOps Agent 当前项目状态

> 2026-08-26 current note：本文件主体保留 2026-08-22 历史快照。PR #21 已 regular merge 至
> `main@c65ff65c`；offline run 32957003253、pilot run 32957003191 与 production run
> 32957003204 均为 `completed / success`。Candidate v5
> `105b7def…5165dffc` 是 Kimi 中国区 pre-call frozen snapshot。锁定后一次独立授权 metadata
> GET 于 `2026-08-26T09:41:49.967Z` verified：HTTP 200、attempts/network calls 1/1、exact
> `kimi-k3` visible、0 model tokens、cost null；receipt 不进入 candidate。一次性授权已消耗，
> 不得重试。Kimi Chat/通用在线入口关闭，Provider 仍 1/2，private 仍 0/50。Anthropic
> post-lock metadata 尝试曾因错误 CCTK token 返回 403/0 model tokens。PR #21 的长期边界见
> [Kimi Models preflight main CI 快照](evidence/kimi-models-preflight-main-ci-v1/README.md)，最新
> 跨会话状态以仓库根目录 `handoff.md` 为准。

> 状态快照：2026-08-22（Asia/Shanghai）
> 用途：集中记录当前已经完成、已经验证、尚未完成以及可以/不可以对外声称的事实。
> 重要说明：已发布的 `v0.2.0 / phase6-deepseek-v1`、已合并但尚未发布新 Release 的 production slice，以及已在本地提交但尚未 push/release 的 Eval v2 工作必须分开表述。

## 1. 当前结论

ResearchOps Agent 已经形成一个 evidence-first 科研数据分析 Agent 工程原型：模型负责规划和受控工具编排，确定性工具负责数据质量、方法选择、统计计算和证据生成；逻辑资源 ID、中央风险策略、人工审批、幂等、重试和追加式审计共同限制模型能力。

当前有三层证据：

1. 已发布的 Phase 5/6 基线：Phase 5 离线组件与控制面 50/50；冻结 DeepSeek Phase 6 development 16/16、repo-local holdout 4/4。
2. 本地已提交但未发布的 Eval v2 public foundation 与一次性 public candidate：3 个外部数据集、120 个 internal-ready 公开任务、受控数据准备、逻辑 registry、聚合 inspect backend、Provider executor、runner/scorer、三次重复聚合、原子 artifact writer；DeepSeek public Provider system 68/93，fault harness 27/27。
3. 已进入 `main` 的独立 production-like vertical slice：FastAPI、PostgreSQL lease queue、aggregate inspect、S3/MinIO 与 OTel；18/18 contract tests、本机 E2E、Ubuntu `main` push E2E 与手动 dispatch E2E 均通过。

完整 Eval v2 campaign 仍是 `design_only`：public candidate 已运行，但没有 private holdout、第二 Provider或外部领域专家复核结果。68/93 只属于锁定的 `DeepSeek + 控制面` public system，不能称为模型单体规划准确率或未知生产泛化。

Phase 5 P1 已关闭：旧 `main@badb7169` 的 offline workflow 曾在实际 44/50、证据 10/21 时错误显示绿色。根因是 LF/CRLF provenance 不一致及 `eval-run=5` 被后续 verifier=0 覆盖。5 文件 PR #3 已合并至 `main@b3f515a`；main run 32571384757 为 152/152、50/50、21/21、`phase5-ci-v1=valid`、50 条 audit chain 全部有效。

## 2. Git 与发布状态

| 项目 | 当前状态 |
| --- | --- |
| 当前 checkout 分支 | `codex/eval-v2-evidence-v2`，未设置 upstream |
| 当前分支关系 | unittest 兼容提交完成后比 `origin/main` ahead 4；最新冻结实现 commit 为 `3e921ce04c4f329e1229aa2d8ab74f67955b9f53` |
| `origin/main` | `b3f515a300855caef89efbf1f48859e91b27d925` |
| 本地 `main` 指针 | 仍为 `80ad08e835103eb5fb7c07e730580efa0206ce1a`，落后 `origin/main` 2 个提交；不要在 dirty tree 上强制切换/reset |
| 已发布版本 | `0.2.0` |
| Annotated tag | `phase6-deepseek-v1`，仍指向 `80ad08e…` |
| 已发布 Release | `ResearchOps Agent v0.2.0 — Frozen DeepSeek Phase 6 evaluation` |
| PR #1 | 已合并，两个离线质量门成功 |
| PR #2 | 已 squash 合并 production slice 与 Linux CI；merge commit `badb7169…` |
| PR #3 | 已 squash 合并 Phase 5 LF provenance 与 fail-closed CI；merge commit `b3f515a…` |
| 本地 evidence commit | `cf5e9d19e88fe041415ba46490977e3128497913`，18 个文档/长期证据路径 |
| 本地 frozen implementation commit | `3e921ce04c4f329e1229aa2d8ab74f67955b9f53`，39 个锁定 candidate/self-pilot 路径 |
| PR #4 | `Add locked Eval v2 public candidate and evidence`，3 个基础提交 / 57 个原始文件；Ubuntu compose E2E 已通过，Windows 首轮因测试文件依赖未锁定 pytest 失败，纯 unittest 修复正在提交 |
| 当前工作树 | 兼容提交完成后可见 tracked/untracked 变更 0、staged 0；本地运行数据与输出继续 ignored 并保留 |

当前 Release 不包含 production slice 或 Eval v2 新增实现。`main` 已包含完整 production slice 以及其依赖的 5 个 Eval v2 foundation 模块；完整 Eval v2 runner/scorer/provider/self-pilot/corpus/证据已进入 PR #4，尚未 merge/release，不能笼统说成“已进入 main”。

### 2.1 当前本地提交与排除项

| 分组 | 当前处理 |
| --- | --- |
| Eval v2 冻结单元 | 已整体提交为 `3e921ce`；candidate verifier 为 `valid`，commitment `7744770a…f0d11` |
| Self-pilot | Web/CLI、翻译、指南与测试与冻结源码一起进入 `3e921ce` |
| Phase 5/production CI evidence | 已独立提交为 `cf5e9d1`；main Phase 5 run 32571384757 与 production run 32568017244 均有长期快照 |
| 共享源码 | `cli.py` 与 `model_providers.py` 参与 Eval v2 source bundle，已原样进入 `3e921ce`，不得在后续 PR 中遗漏 |
| 本地输出 | `output/` 完整保留但不提交；其中 EML 含用户邮箱，必须继续排除 |
| 本地运行数据 | self-pilot sessions、准备数据、production E2E、`tmp/`、`.env` 与 5 个 secret 均继续 ignored，不清理、不提交 |

## 3. 已完成的核心分析能力

### 3.1 数据与研究设计边界

- 输入为脱敏 CSV、研究问题和显式 `ResearchDesign`。
- 不从列名猜随机化、因果关系、配对、重复测量或协变量时序。
- 模型只使用逻辑资源 ID，不接收真实文件路径或任意代码/SQL/shell 能力。
- 数据文件、方法建议和执行通过 SHA-256 绑定；输入变化时 fail closed。
- 行级标识符和可能标识符在 profile 中脱敏，不向模型返回样例值。

### 3.2 统计工具

主分析：ANCOVA。

- 对比方向：`treatment - control`
- 基线协变量按分析集均值中心化
- OLS 点估计
- HC3 稳健协方差
- 残差自由度 t 推断

敏感性分析：Welch 独立样本 t 检验，并报告 Hedges g。

固定模拟试验结果：

| 方法 | 估计 | 95% CI | p 值 | Evidence ID |
| --- | ---: | ---: | ---: | --- |
| ANCOVA + HC3 | -5.6069 mmHg | [-7.9351, -3.2787] | 3.82e-6 | `E-7C87BB6C88EB` |
| Welch | -6.7887 mmHg | [-10.8425, -2.7349] | 0.001134 | `E-B93CD9DC7751` |

源数据 240 行，随访结局缺失 28 行，实际纳入 212 行。请求目标人群是 ITT，但当前实现是 available-case，不能称为完整 ITT。

### 3.3 Evidence 与报告

- 每条统计结论绑定 `evidence_id + metric_path + displayed_value + direction`。
- 报告只消费聚合 evidence bundle，不直接读取原始 CSV。
- 数值不存在、方向错误、显示值不匹配或 evidence ID 未由本次工具输出支撑时 fail closed。
- 图表只包含聚合估计与置信区间，不包含行级散点或参与者 ID。

## 4. 已完成的控制面与安全能力

### 4.1 工具风险与授权

- 风险分类：read-only、controlled write、sensitive export、external action、destructive、arbitrary execution。
- 未知工具和未知风险默认拒绝。
- sensitive export 和 arbitrary execution 默认拒绝。
- controlled write、external action、destructive 默认要求人工审批。
- 授权字段必须与本次 request 的逻辑 ID 精确一致；可见资源选择不等于授权。

### 4.2 审批与恢复

审批 scope 绑定：

- call ID
- 工具名/版本
- 策略版本
- 规范化参数
- 源资源 SHA-256
- 目标逻辑 ID
- TTL

Phase 4 已验证批准、拒绝、过期、参数变化、源文件变化和本地恢复。Phase 6 只验证首次审批暂停，不支持批准后在线恢复。

### 4.3 重试、幂等与不确定结果

- 只重试声明为可重试的瞬时错误。
- 验证、策略、审批、路径和统计错误不重试。
- 成功的同一 call ID 重放返回账本结果，不重复执行 handler。
- 非幂等副作用结果未知时不盲目重试；可 reconcile 才核对，否则进入 `outcome_unknown`。
- 发布采用源/目标 hash、调用 ID 和原子 manifest 防止静默覆盖。

### 4.4 审计

- SQLite 记录运行、工具调用、attempt、错误、重试、审批、usage 和产物。
- 每个运行拥有独立、连续 sequence 的逐事件 SHA-256 链。
- trigger 阻止普通 update/delete。
- verifier 可检测中间删除、payload 修改和前序 hash 变化。
- 当前 chain head 尚未外部签名；完全控制数据库的人仍可能重建整条链。

## 5. 已发布的 Phase 5 证据

准确名称：`offline_deterministic / components_and_control_plane`。

| 指标 | 结果 |
| --- | ---: |
| 任务通过 | 50/50 |
| 非预期工具错误 | 0/45 attempts |
| 主动注入且被正确处理的错误 | 11/45，24.44% |
| 安全违规 | 0/50 |
| 证据引用 | 21/21 |
| P50/P95 | 100.38/411.03 ms |
| 模型调用 | 0 |

Phase 5 的 50/50 不能称为 LLM Agent 规划准确率。成本为 0 仅因为没有模型调用。

## 6. 已发布的 Phase 6 DeepSeek 证据

冻结配置：

- Provider：`deepseek`
- Model：`deepseek-v4-flash`
- Transport：`openai_compatible_responses`
- Runner：`1.6.0`
- Agents SDK：`0.21.0`
- Max output：2000 tokens
- Max turns：8

| Split | 结果 | Requests | Tokens | Agent P50/P95 |
| --- | ---: | ---: | ---: | ---: |
| Development | 16/16 | 28 | 71,039 | 7.65/17.40 s |
| Repo-local holdout | 4/4 | 6 | 16,854 | 5.05/14.59 s |

Development 包含 2 个审批暂停任务，均停在 SDK interruption 与本地 scope-bound pending proposal；审批 decision、handler attempt 和发布副作用均为 0。

限制：

- holdout 只有 4 题，任务和 golden 位于仓库内，不抗污染。
- holdout 不含审批题。
- 结果不是未知生产集泛化，也不是生产 SLA。
- DeepSeek 成本因价格模型不完整保持 `null / unavailable`。
- OpenAI 路径受独立 API 计费条件/HTTP 429 阻塞，没有 OpenAI 模型质量结论。

## 7. 本地已完成但尚未发布的 Eval v2

### 7.1 Campaign 与防污染合同

目标规模：

- Development：80
- Public regression：40
- External private holdout：至少 50
- 至少 3 个非 synthetic 数据集
- 至少 2 个 Provider
- 每个 Provider 3 次重复

Private holdout 的题面、golden、task ID 和 locator 禁止进入仓库；每次 freeze 只允许一次 private campaign。

### 7.2 三个外部数据集

| Dataset | 许可 | 固定结构 | 当前状态 |
| --- | --- | --- | --- |
| Palmer Penguins v0.1.0 | CC0-1.0 | 344×8，19 个缺失 cell | source/hash/结构已核验；外部 review 未完成 |
| UCI Parkinsons Telemonitoring #189 | CC BY 4.0 | 5,875×22，42 个 subject | source/hash/结构已核验；外部 review 未完成 |
| UCI Heart Disease Cleveland #45 | CC BY 4.0 | 303×14，6 个缺失 cell | source/hash/结构已核验；外部 review 未完成 |

公开资产已实际完成 3/3 内存下载复核：3 次网络请求、下载 436,356 bytes、写入文件 0。

### 7.3 受控数据准备与逻辑 registry

- 下载 archive 和 selected asset 在转换前重新核对 hash/bytes/结构。
- 原始下载只驻留内存，不持久化。
- Palmer 保留 curated 8 列并规范化缺失值。
- Parkinsons 用 SHA-256 派生的 `subject_key` 替换原始 `subject#`，删除原编号。
- Heart Disease 只选择 processed Cleveland 14 列，固定表头并规范化 `?` 缺失。
- 输出只能位于 `artifacts/` 下不存在的新目录。
- staging 完成 hash/结构校验后原子 rename，禁止覆盖。
- Registry 每次 `resolve(dataset_id)` 重验根目录包含关系、size 和 SHA-256。
- 模型侧固定 `aggregate_tools_only`，不公开路径、hash 或行级值。

### 7.4 Aggregate inspect backend

独立 `EvalV2InspectDatasetBackend` 将 registry 受控 CSV 接到确定性 `profile_csv`，只返回：

- 行列数
- 重复行和缺失计数
- 列名、语义类型、缺失率、唯一率
- possible identifier 风险
- 缺失模式
- 数据集分析边界

输出明确排除路径、文件名、SHA、`sample_values`、subject key 值和原始行。

### 7.5 Public task corpus

当前 corpus：120/120 internal-ready。

| Split | 总数 | Ready | Draft |
| --- | ---: | ---: | ---: |
| Development | 80 | 80 | 0 |
| Public regression | 40 | 40 | 0 |

三个数据集各覆盖 40 题。场景分布：

| 场景 | 数量 |
| --- | ---: |
| 标准分析/聚合检查 | 20 |
| 澄清 | 22 |
| 安全拒绝 | 19 |
| 越权资源 | 12 |
| Prompt injection | 11 |
| 重复工具调用 | 12 |
| Provider timeout | 6 |
| 输出截断 | 6 |
| 审批暂停 | 6 |
| 副作用结果未知 | 6 |

Internal review v4 精确绑定当前 public corpus SHA 和全部 120 个 task ID。它是内部工程复核，不是外部专家复核。

### 7.6 独立 runner 与 scorer

- Executor 只能收到 `EvalV2PublicTask.public_input()`，无法访问 expected golden。
- 每题创建独立 tool gateway，工具参数必须与授权 context 精确一致。
- Inspect 调用路由到 aggregate-only backend。
- Publish 只创建一次 `awaiting_approval`，固定零副作用。
- 未知工具、参数替换和重复发布提案 fail closed。
- Scorer 检查完整工具顺序、参数、outcome、required/forbidden 文本、evidence ID、closed numeric claim catalog、审批、安全和 completion integrity。
- 报告按 split、scenario、dataset 聚合成功率，并报告工具/参数准确率、审批控制、P50/P95 和 usage 完整性。
- Public-regression candidate 预承诺三次不同 task order，聚合按 task ID 对齐；31 道 Provider 行为题与 9 道故障 harness 必须分通道报告。

### 7.7 Provider executor

- 复用现有每次运行独立的 `ProviderAdapter`。
- 显式绑定 provider/model/transport。
- 未显式确认时 Provider client 不创建。
- API Key 不进入 prompt、repr、report 或错误。
- 第三方 Provider 强制关闭 OpenAI tracing。
- 使用锁定 Agents SDK 0.21 的 `Agent + Runner + function tools`。
- Provider timeout、输出截断和 Provider 错误映射为稳定、无响应正文的状态。
- Refusal/clarification 使用 512/768 专用短预算；因果澄清由本地双语模板固定包含 observational 与 association-analysis 问题。
- 重复工具调用分别记录模型请求、去重、gateway dispatch 和 backend execution；评分仍使用去重后 gateway trace，telemetry 仅用于诊断。

Provider executor 已在内部 self-pilot 与一次性 Eval v2 public-regression candidate 中联网使用，并完成注入 Runner 的离线合同测试。

Public-regression 配置作为独立 `candidate_locked` 锁定，commitment 为 `7744770a…f0d11`；一次性运行已完成，Provider system 68/93，三轮 23/31、22/31、23/31，fault harness 27/27，保守成本 CNY 0.908142。完整 campaign 仍为 `design_only`，未授权 private holdout，也没有模型质量或未知生产泛化声明。依赖环境与 58 个精确版本 pin 一致，但 lock 不含 wheel/sdist hashes。

### 7.8 每 Provider 三次重复与原子 artifacts

- `run_eval_v2_three_repetitions()` 顺序运行 repetition 1、2、3。
- 聚合器要求每个 Provider 恰好包含 1/2/3 三次报告。
- 所有 Provider/repetition 必须使用完全相同的有序 task scope。
- 聚合成功率均值/范围、逐题稳定率、三次全通过率、模型调用量和 usage 完整性。
- 原子 artifact writer 生成脱敏 report、summary、可选 repetition aggregation 和 manifest。
- Manifest 记录每个文件的 SHA-256/size 和 source-tree hash。
- 禁止 API Key、Authorization、绝对路径、final output、raw rows、sample values 和 traceback。
- 未冻结 artifact 固定 `model_quality_claim_allowed=false`。

### 7.9 内部 Self-Pilot Web / CLI

已实现六个面向项目使用者的命令：

- `self-pilot-create`：确定性选择代表性任务并创建不含 golden 的盲化 session。
- `self-pilot-next`：显示下一道待运行或待反馈任务。
- `self-pilot-run`：显式确认后运行单题 Provider；每题禁止覆盖和重跑。
- `self-pilot-record`：记录接受度、首次完成、修改次数、用时、严重错误和安全问题。
- `self-pilot-web`：首页选择受控 Provider/model 并输入 Key，经模型目录零-token 预检后，在仅绑定本机的双语页面中展示题目；点击后运行 Provider 并由服务器计时，以非领域专家角色记录可理解性、实用性、置信度、专家复核需求、明显问题、信息遗漏和安全担忧，再进入下一题。
- `self-pilot-summary`：生成 internal self-pilot Markdown 总结。

控制属性：

- Provider 运行前必须显式确认；未确认时不读取 Key、不创建 client。
- Task pack 只包含 task ID、prompt 和授权 context，不包含 expected/golden。
- 机器评分在人工反馈提交前不通过 CLI 展示。
- Provider 正文只显示在终端，session 仅保存 response SHA、outcome、usage 和机器评分，不保存正文。
- 绝对路径、Key/Bearer 和 subject key 形态触发输出遮蔽，原文不持久化。
- Session 目录不可覆盖，blinded task pack 与 public corpus 均有 SHA 绑定；schema 1.1 分离确定性 `pilot_pack_id` 与唯一 `session_instance_id`，旧 1.0 session 只读兼容。
- 总结固定标记 `internal_self_pilot`、`external_pilot=false` 和 `external_validation_claim_allowed=false`。
- Web 写请求使用进程内随机令牌；Key 不进入页面，Agent 正文只驻留当前进程内存。

截至 2026-08-21，使用者已完成两个旧 corpus 的 12 题内部 session：`session-01` 使用 legacy acceptance 表单；`session-02` 使用 `non_expert_usability_v1`，12/12 均认为可理解且有用、3/12 需要专家复核、1/12 有明显问题、0/12 报告安全担忧，严格机器合同通过 3/12。两轮均是内部 development self-pilot，不是专业正确性、外部验证或未知泛化证据。后续源码/语料修复要求新建 schema 1.1 session，不回写旧 session。

## 8. 当前自动化验证

2026-08-22 当前证据：

```text
main root: 152 passed (run 32571384757)
PR #4 local complete worktree: 246 passed (本次本地验证)
Production slice: 18 passed
```

同时验证：

- Phase 5 corpus：50 题，合同有效。
- Phase 6 corpus：20 题，development 16 / holdout 4，合同有效。
- Eval v2 public corpus：120 题，development 80 / public regression 40，120 ready / 0 draft。
- Dataset manifest、public corpus、campaign 和 internal review hash 全部匹配。
- Private task count in repository：0。
- PR #2 已合并至 `main@badb7169`。
- Ubuntu `production-slice-e2e` push run 32568017244：`success`；真实 E2E 68.861 秒，344×8、attempt 1、4-event hash chain、MinIO metadata、幂等复用、cross-process trace 与 secret leak 0 均通过。
- 手动 `workflow_dispatch` run 32568233292：`success`。
- Windows `offline-quality-gate` run 32568017243：workflow conclusion 为 `success`，144 项根测试 `OK`；但 clean LF Phase 5 重建只有 44/50、证据引用 10/21。本地遗留 CRLF 数据重跑为 50/50。当前 job 未检查质量阈值，这是数据/golden provenance 与门禁双重 P1，而不是当前 clean checkout 的 50/50 证据。
- 本地独立审计运行写入默认 ignored 的 `artifacts/local_phase5_audit_*`：50/50、证据引用 21/21、artifact verifier `valid`、model calls 0；该结果绑定 CRLF dataset hash `db7c…`，只用于定位根因，不作为 clean `main` 成绩。
- P1 预合并本地复核：Phase 5 corpus SHA-256 `dd591862…8e67`，规范 LF dataset `7ae3…`；当前工作树和独立 `origin/main` source 快照均为 50/50、21/21，`eval-run=0`、`phase5-ci-v1` verifier=0。修复前 44/50 产物由 profile 稳定拒绝并返回 1。
- PR #3 push run 32570797245：152/152、50/50、21/21、`phase5-ci-v1=valid`、50 条 audit chain 全部有效；artifact digest `sha256:7ecfe7a3…214f`。
- PR #3 pull_request run 32570848599：独立重复 50/50、21/21、audit all valid；artifact digest `sha256:06222e54…1d2c`。
- PR #3 已 squash 合并至 `main@b3f515a`；main run 32571384757：152/152、50/50、21/21、profile valid、50 条 audit chain 有效、unexpected errors 0、安全违规 0、model calls 0；artifact digest `sha256:d6cb19a4…2aa5`。

## 9. 当前 Eval v2 尚未完成

Campaign 仍为 `design_only`，`ready_for_freeze=false`；public candidate run 不等于完整 campaign freeze。主要缺口：

- Private holdout：0/50，必须由外部 custodian 保管。
- Campaign 正式注册数据集仍为 1/3；三个外部数据集只有 source verification，外部专家 review 未完成。
- 已注册 Provider：1/2；第二 Provider 未冻结。
- Public candidate 已绑定 source/prompt/tool/scorer/split/dependency hashes；完整 private campaign 的 custodian commitment 与 freeze hashes 尚缺。
- 外部领域专家 golden review：未完成。
- R/SAS 独立统计交叉检查：未完成。
- 外部科研用户 pilot：未完成。
- 内部 self-pilot：Web/CLI 已实现；三个 12 题内部 session 已完成，仍不是外部用户 pilot。
- Eval v2 Provider 在线运行：DeepSeek public candidate 已完成 3 次；private 与第二 Provider 未运行。
- 两个 Provider × 每个 3 次公开集评测：仅 DeepSeek 完成，第二 Provider 未执行。
- Private campaign：未执行。
- Eval v2 public 证据与冻结实现已提交为本地 `3e921ce`，尚未 push/merge/release。
- Production-like FastAPI/PostgreSQL lease queue/S3/OTel vertical slice 已合并至 `main`，18 项测试、本机 E2E、Ubuntu push 与手动 dispatch 均通过；仍未完成 HA、云 IAM/KMS/TLS、备份恢复和负载测试。

## 10. 正确对外表述

可以表述：

> 已保留与冻结 source/manifest 绑定的 Phase 5 历史 50/50，完成冻结 DeepSeek Phase 6 小样本在线评测，以及一次性 Eval v2 DeepSeek public candidate 三轮运行（锁定系统 68/93，fault harness 27/27）；production-like 基础设施首切片已合并并通过 `main` Ubuntu E2E。PR #3 已修复 Phase 5 LF provenance 与 fail-closed CI；当前 main run 32571384757 为 50/50、21/21、profile valid。

不能表述：

- Phase 5 的 50/50 是 LLM 规划准确率。
- DeepSeek development 16/16、repo-local holdout 4/4 证明未知生产集泛化。
- Phase 6 支持批准后在线恢复。
- Eval v2 public 68/93 是模型单体规划准确率、private holdout 或未知生产泛化。
- 120 个内部复核任务已经通过外部领域专家验证。
- 三个外部数据集已经完成正式外部统计审查。
- 系统已经在大规模生产环境运行。
- `offline-quality-gate` 的绿色状态等于当前 Phase 5 质量阈值通过。

## 11. 常用验证命令

```powershell
$env:PYTHONPATH = "src"

# 全部测试
.\.venv\Scripts\python.exe -m unittest discover -s tests

# Phase 5 / Phase 6 / Eval v2 合同
.\.venv\Scripts\python.exe -m researchops.cli eval-validate
.\.venv\Scripts\python.exe -m researchops.cli phase6-validate
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-validate

# 显式联网复核公开数据，不写数据文件
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-verify-datasets `
  --confirm-download

# 显式生成本地准备产物；目标目录必须不存在
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-prepare-datasets `
  --output-dir artifacts/eval_v2_datasets/local-01 `
  --confirm-download

# 通过逻辑 ID 查看聚合 profile
.\.venv\Scripts\python.exe -m researchops.cli eval-v2-inspect `
  palmer_penguins_v0_1_0 `
  --registry artifacts/eval_v2_datasets/local-01/logical_dataset_registry.json
```

不要在 CI 中默认运行在线 Provider 评测，不要把当前 repo-local holdout 重新用于 prompt 调优。

## 12. 下一步优先级

1. 完成 PR #4 的纯 unittest 兼容修复并取得新的 Windows clean run；继续排除 `output/`、sessions、数据、secret 与临时文件。
2. 找到外部领域专家或统计支持资源，完成 dataset/golden 外部复核，并使用 R 或 SAS 做独立数值交叉检查。
3. 建设 pilot-ready staging，并邀请真实科研用户 pilot。
4. 冻结第二 Provider。
5. 由外部 custodian 创建、冻结并一次性运行 private holdout。
6. 发布独立 Eval v2 版本证据。

## 13. 相关文档

- [README](../README.md)
- [架构与安全边界](ARCHITECTURE.md)
- [证据索引](EVIDENCE.md)
- [作品集手册](PORTFOLIO.md)
- [Eval v2 设计](../evals/EVAL_V2.md)
- [Eval v2 campaign](../evals/v2/campaign.json)
- [Eval v2 dataset manifest](../evals/v2/external_datasets.json)
- [Eval v2 public task schema](../evals/v2/public_task_schema.json)
- [Eval v2 internal review](../evals/v2/internal_review.json)
- [冻结 Phase 6 DeepSeek 证据](evidence/phase6-deepseek-v1/README.md)
- [Production slice main Linux CI](evidence/production-slice-linux-ci-main-v1/README.md)
- [Main offline gate audit](evidence/main-offline-gate-20260822/README.md)
