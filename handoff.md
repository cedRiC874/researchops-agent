# ResearchOps Agent 跨会话交接

> 更新时间：2026-08-23（Asia/Shanghai）
> 目标：让一个没有历史上下文的新 Codex 会话安全、准确地继续本项目。
> 语言偏好：中文；先给结论，再给可执行步骤；不要夸大评测或生产化程度。

## 0. 新会话首先执行

1. 完整阅读本文件。
2. 在仓库根目录运行 `git status --short --branch`。
3. 阅读 [README.md](README.md)、[docs/EVIDENCE.md](docs/EVIDENCE.md)、[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 和 [docs/PORTFOLIO.md](docs/PORTFOLIO.md)。
4. 保留所有现有未跟踪文件，不覆盖、不清理、不重置。
5. 未经用户明确要求，不要 commit、push、重新运行付费在线评测或重复使用冻结 holdout 调参。
6. 不要读取、打印或记录 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` 的值。

## 1. 仓库与发布状态

- 本地路径：`C:\Users\付翔\Documents\ChatGPT\项目\researchops-agent`
- GitHub：https://github.com/cedRiC874/researchops-agent
- 本次 evidence-only 分支：`codex/pilot-ci-evidence`，从 `origin/main` 的
  `a20fdfd8ff6a2e4e29881aa6693589655e307e72` 创建。
- GitHub `main`：`a20fdfd8ff6a2e4e29881aa6693589655e307e72`；PR #5 已 regular merge。
- 版本：`0.2.0`
- Annotated tag：`phase6-deepseek-v1`，仍指向 `80ad08e…`，不是当前 `main`
- Release：https://github.com/cedRiC874/researchops-agent/releases/tag/phase6-deepseek-v1
- PR #1 已合并冻结 Phase 6；PR #2 已 squash 合并 production slice 与 Linux CI。
- PR #3 已 squash 合并，恰好 1 commit / 5 files；push run 32570797245、
  pull_request run 32570848599 与合并后的 main run 32571384757 均成功。
- `main` production-slice push run 32568017244 与手动 dispatch run 32568233292
  均成功；长期证据位于
  `docs/evidence/production-slice-linux-ci-main-v1/`。

PR #4 已用 regular merge 合并，保留独立证据/实现/状态提交；合并后 `main` 两条
workflow 均通过：offline run `32573214902`（246 tests、Phase 5 50/50、21/21、
audit/profile valid）与 production run `32573214910`（真实 344×8 Compose E2E）。

PR #5 已用 regular merge 合并 pilot staging 两个提交：`ce40353`（实现）与
`e188810`（Linux image migration path/fail-fast 修复）。合并后的同一 `main` commit
三条 workflow 均成功：pilot run `32585792915`、offline run `32585792937`、
production run `32585792929`。长期 pilot CI 证据位于
`docs/evidence/pilot-staging-linux-ci-main-v1/`。

本次 evidence-only PR 目标边界仅为新增上述长期快照并更新 `README.md`、
`docs/EVIDENCE.md`、`services/pilot_staging/VERIFICATION.md` 与本文件；不改代码、
workflow、candidate 或 Eval v2。

本轮没有修改锁定的 `src/researchops`，没有运行付费 Provider，也没有读取任何 API
Key。原有用户文件均已保留。PR #4 的本地提交边界曾为：

```text
cf5e9d1: 18 个 main CI 长期证据与状态文档路径
3e921ce: 39 个 Eval v2 locked candidate/self-pilot 冻结单元路径
879b7ca: 移除根 CI 未锁定的 pytest 依赖，并让 13 个 public-runner tests 真正被 unittest 收集
Local-only ignored: output/、sessions/data、tmp/、service .env/secrets（全部保留）
```

`output/email/*.eml` 含用户邮箱，绝不能提交；`output/` 继续本地保留，不删除。
Eval v2 candidate 的 source bundle 覆盖整个 `src/researchops/*.py`，因此当前
`cli.py`、self-pilot、Eval v2 与 `model_providers.py` 必须作为同一个冻结源码单元
保留。candidate verifier 当前仍为 `valid`，commitment：

```text
7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11
```

`main` 已包含完整 Eval v2/self-pilot、production slice 与 pilot staging；最新 Release 仍是
v0.2.0，尚未为 Eval v2 或 pilot staging 新建 Release。

## 1.1 当前 pilot-ready staging 状态（已合并、未上线）

独立服务位于 `services/pilot_staging/`，不把现有 localhost self-pilot 翻成
external，也不改锁定 candidate。已实现：

- 一次性邀请、HMAC token digest、HttpOnly session、CSRF、Host/body/rate 门禁；
- 页面展示完整 consent 文档后才允许运行；撤回立即撤销 session、停止排队调用，
  运行中结果写库前再检查并丢弃；
- 服务器端 Provider secret 只挂载到显式 `online` worker，API/参与者不接触 Key；
- 6 题 prepared public pack，覆盖 3 个数据集和 6 类场景；
- PostgreSQL queue、worker heartbeat/readiness、单题一次 Agent task execution、campaign assignment 上限；该上限不是模型/API 请求数或费用上限；
- output/notes DLP、safety pause、incident list/resolve、失败题排除后继续；
- participant 可在 Agent task execution 排队前或答案显示后跳题；skip 不重跑、不计技术失败；
- Provider latency 与人工阅读时间分开；Markdown 渲染时题目保持在答案上方；
- 追加式事件 hash chain、task-pack integrity、一致快照 aggregate summary、90 天/撤回
  7 天 purge 入口；
- summary claim gate 固定为外部科研用户在 prepared public data 上的可用性，专业
  正确性、private holdout、未知分布、生产 SLA 均保持 false/null。

新增 `execution_environment=supervised` 的 1–2 人监督预试通道：环境值持久进入
campaign/PostgreSQL，永久加入 `supervised_environment_not_claim_eligible`，以后用其他
配置读取也不能解除；一键脚本自动绑定 clean Git/image ID、Tailscale HTTPS、Secure
Cookie、worker heartbeat，并提供 status/invite/stop。主持指南、招募检查表和单场记录模板
已冻结为 6 题流程。

验证快照（无网络 Provider 调用）：38 个 API/domain/config/script/CI tests、3 个
candidate/schema tests、1 个真实临时 PostgreSQL lifecycle/migration/checksum test；
Linux Docker image `sha256:24ab5d3dfae6fdbce6f9ae7e176a106f0e09c955086d03de4f7304c879ab984b`
已成功构建并通过 candidate/pip check。临时 PostgreSQL 容器已停止并由 `--rm` 删除，未建
持久卷。

`.github/workflows/pilot-staging-ci.yml` 已由 `main` run `32585792915` 验证：42/42、
真实 PostgreSQL、offline API Compose、teardown 与最终 gate 均成功；bootstrap 记录
`provider_secret_created=false`、`secret_values_printed=false`，且没有启动 online worker。

这仍不是 production staging。1–2 人 supervised 预试可在本机 Docker + Tailscale HTTPS
下进行，但不能进入正式 external claim。正式 3–5 人 campaign 前仍需托管 PostgreSQL TLS+backup/PITR、Secret Manager、镜像
digest/部署 SHA、脱敏 telemetry/告警、外部 daily retention scheduler、回滚与伦理/IRB
判断（如适用）。完整清单见 `services/pilot_staging/README.md`。

## 2. 项目定位

ResearchOps Agent 是一个 evidence-first 科研数据分析 Agent 原型：

- 输入脱敏 CSV、研究问题和显式 `ResearchDesign`；
- 模型只负责规划和工具编排；
- 确定性工具负责数据质量、方法选择、统计计算和可视化；
- 逻辑资源 ID 隔离模型与真实路径；
- 危险写操作由中央策略和人工审批控制；
- 工具调用、attempt、错误、重试、审批和模型 usage 进入 SQLite 审计；
- 每个运行拥有独立的逐事件 SHA-256 哈希链；
- 报告中的 claim 必须绑定 evidence ID、指标路径、显示值和方向；
- Phase 5 评测组件与控制面，Phase 6 评测真实 Agent 工具轨迹和最终回答。

一句话口径：

> 这不是让模型自由编写和执行统计代码，而是让模型在可审计边界内编排经过验证的科研计算工具。

## 3. 关键架构与安全决策

### 3.1 Agents SDK 的定位

- 使用 `openai-agents>=0.21,<0.22`，冻结在线证据对应 0.21.0。
- SDK 负责 typed tools、Agent loop、tool items、approval interruption、usage 和可注入 Runner。
- SDK 不是安全边界；本地 `ControlledToolExecutor` 才是策略、授权、审批、幂等和审计的事实来源。
- `parallel_tool_calls=False` 只是一项 Provider 请求语义；所有 Phase 6 工具仍共享本地 `asyncio.Lock`。
- Phase 6 `RESUME_SUPPORTED=False`：只验证首次审批暂停；不要声称它支持批准后在线恢复。
- 完整批准、拒绝、过期、恢复与本地发布由 Phase 4 展示。

### 3.2 逻辑资源授权

- `LogicalAgentRequest` 将研究问题和授权 context 分开。
- 模型不会从自然语言问题“生成授权资源 ID”。
- 逻辑 ID 仅允许字母、数字、下划线和连字符，最长 64 字符。
- 工具缺少完整授权 ID 时通过 `is_enabled=False` 隐藏。
- `_authorized_arguments` 要求字段集合精确匹配，并逐字段等于本次 request 授权值。
- 本地 demo registry 只允许固定 ID，例如 `synthetic_trial`、`trial_primary`、`trial_unadjusted`、`phase3`。
- 后端将逻辑 ID 映射到路径，随后执行 `resolve` 与受控根目录包含检查。
- 当前只是静态 demo registry，不是多租户资源服务；生产需要 OIDC、RBAC/ABAC、租户隔离、授权撤销和动态资源生命周期。

### 3.3 风险、预算与串行化

- 风险类：read-only、controlled write、sensitive export、external action、destructive、arbitrary execution。
- 未知工具和未知风险默认拒绝。
- sensitive export 与 arbitrary execution 默认拒绝。
- controlled write、external action、destructive 要求审批。
- Phase 6 每次运行最多 16 次工具调用、8 个 turns、单工具 30 秒软超时、总运行 120 秒。
- 同一运行最多一个待审批 publish proposal。
- 同步工具通过 `asyncio.to_thread` 避免阻塞 event loop；这是软超时，不能强杀底层线程。

### 3.4 审批指纹

审批范围实际绑定：

```text
SHA256(canonical_json({
  call_id,
  tool_name,
  tool_version,
  policy_version,
  normalized_args,
  resources
}))
```

发布资源包含源 bundle SHA、图表 SHA 与目标逻辑资源 ID。恢复执行前重新计算参数、工具/策略版本、源 SHA、目标和 TTL。参数变化、源文件变化、目标变化、工具升级、审批过期或跨调用复用都会 fail closed。

### 3.5 幂等与不确定结果

- 已成功的同一 `call_id` 重放时返回账本结果，不再次调用 handler。
- 发布 manifest 绑定 `tool_call_id` 和文件 SHA；不同调用或内容冲突时拒绝覆盖。
- 瞬时错误只有在工具声明可重试且风险允许时才重试。
- 副作用结果未知时不盲重试，进入 `outcome_unknown`。
- 当前没有完整自动崩溃 reconciler；这是生产化待办。

### 3.6 审计链

- 链按 run 隔离，粒度为每个审计事件，不是每个工具调用。
- 每个事件覆盖 `run_id`、连续 sequence、事件类型、时间、actor、安全 payload 和 `prev_hash`。
- SQLite trigger 禁止普通 update/delete。
- 中间删除、payload 修改或前序哈希变化可由 verifier 检测。
- 内部 verifier 无法单独证明最后若干事件未被整体截断；完全控制 DB 的人也可重建整条链。
- 生产需要将 chain head 外部签名并写入不可变存储。

## 4. 统计实现与固定结果

模拟数据：240 行随机对照试验，每组原始 120；随访结局缺失 28，available-case 纳入 212（treatment 110、control 102）。请求人群是 ITT，但当前实现是 available-case，报告不得称为完整 ITT。

主分析 ANCOVA：

- 显式方向：`treatment - control`
- 基线协变量按分析集均值中心化
- OLS 点估计 + HC3 稳健协方差 + 残差自由度 t 推断
- 调整差：`-5.6069 mmHg`
- 95% CI：`[-7.9351, -3.2787]`
- p：`3.82e-6`
- Evidence：`E-7C87BB6C88EB`

敏感性分析 Welch：

- 未校正差：`-6.7887 mmHg`
- 95% CI：`[-10.8425, -2.7349]`
- p：`0.001134`
- Hedges g：约 `-0.454`
- Evidence：`E-B93CD9DC7751`

方法选择由显式 `ResearchDesign` 和确定性规则执行：

- 独立两组连续结局 + 预指定处理前协变量：ANCOVA 主分析、Welch 敏感性；
- 独立两组无协变量：Welch；
- 配对/重复测量走其他分支；
- 协变量时序不明或 post-treatment 时安全停止；
- 不根据单个正态性 p 值自动换方法；
- 交互显著时追加人工审查警告，不自动改变 estimand；当前仍会生成加性 ANCOVA evidence。

## 5. 已验证评测证据

### 5.1 Phase 5：确定性组件与控制面

准确名称：`offline_deterministic / components_and_control_plane`。

- 50/50 任务通过；
- 非预期工具错误：0/45 attempts；
- 主动注入且正确处理的工具错误：11/45，毛错误率 24.44%；
- 安全违规：0/50；
- 证据引用：21/21；
- P50/P95：100.38/411.03 ms；
- 模型调用 0；成本为 0 仅因为没有模型调用；
- 不能把 50/50 说成 LLM Agent 规划成功率。

当前 tracked 作品集证据：`artifacts/portfolio_baseline_provider/`。

### 5.2 Phase 6：冻结 DeepSeek 在线 Agent 评测

冻结配置：

- Provider：`deepseek`
- Transport：`openai_compatible_responses`
- Model：`deepseek-v4-flash`
- Runner：`1.6.0`
- SDK：`openai-agents 0.21.0`
- Max output：2000 tokens
- Max turns：8
- Case timeout：120 秒
- Task schema：1.2
- Source hash：`24a28a7a19fb4e8546f27af995c4baa24e20a2dadedbc9a7efc1926dfb10626c`
- Corpus hash：`7c478dd2f90ffb2796fd18dfe77129570a6ee9ea06b343a4cdf55f4e99500da0`
- Split hash：`d19bc5a0516649c27e1069f8f57f8ba8a0172a03524a8c8e8a10b6c31eabbe6e`

Development：

- 16/16；
- 工具、参数、证据、numeric CLAIM、安全和 completion integrity 全通过；
- 2 个 publish 审批题都停在 SDK interruption + 本地 scope-bound pending proposal；
- handler attempt、审批 decision 与发布副作用均为 0；
- P50/P95：7.65/17.40 秒；
- 成本保持 `null / unavailable`。

Repo-local non-secret holdout：

- 4/4；
- P50/P95：5.05/14.59 秒；
- 不含审批题；
- 任务和 golden 均在仓库内，可见、不抗污染；
- 不能表述为未知分布泛化或生产 SLA。

冻结脱敏证据位于 [docs/evidence/phase6-deepseek-v1/](docs/evidence/phase6-deepseek-v1/README.md)。只提交 report、summary、manifest 和 audit index；详细 SQLite/results 未提交，但其 hash/size 进入 manifest。

### 5.3 OpenAI 路径

- Agents SDK adapter 已实现并测试。
- Key 认证问题修复后，独立最小 API 请求返回 HTTP 429。
- 当前外部 API 计费条件阻塞；没有可发布的 OpenAI 模型质量结论。
- 不要把 429 解释为 OpenAI 模型质量。

## 6. 当前自动化验证

2026-08-23 的当前 `main@a20fdfd8` 分层验证：

```text
main offline run 32585792937: 246 root tests OK; Phase 5 50/50; evidence 21/21
main pilot run 32585792915: 41 offline contracts + 1 real PostgreSQL contract; offline Compose success
main production run 32585792929: 18 contract tests + real PostgreSQL/MinIO/OTel E2E success
```

重要 P1 历史事实：run 32568017243 的 workflow conclusion 虽为 `success`，其新重建
Phase 5 实际只有 44/50、evidence citations 10/21。根因已定位为 clean checkout
使用 LF CSV hash `7ae3…`，而 corpus golden 绑定本地遗留 CRLF hash `db7c…`；本地
CRLF 环境本次仍可重跑 50/50，且 workflow 让后续 verifier=0 覆盖了 eval-run=5。

PR #3 已完成：38 个 Phase 5 provenance 标量切换到 LF lineage，corpus
SHA-256 `dd591862…8e67`；`phase5-ci-v1` 精确要求 50/50 与 21/21；workflow 分别
保存 evaluation/verifier exit code。push run 32570797245 与 PR run 32570848599
均在 clean GitHub runner 得到 50/50、21/21、audit all valid；合并后的 main run
32571384757 再次通过 152/152、50/50、21/21、`phase5-ci-v1=valid` 与 50 条 audit
chain。修复前 44/50 产物会稳定 exit 1；P1 已关闭。详见
`docs/evidence/main-offline-gate-20260822/`。

Phase 6 契约校验：

```text
status=valid
task_count=20
development=16
holdout=4
task_schema_version=1.2
network_calls=0
```

常用命令：

```powershell
cd "C:\Users\付翔\Documents\ChatGPT\项目\researchops-agent"
$env:PYTHONPATH = "src"

.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m researchops.cli eval-validate
.\.venv\Scripts\python.exe -m researchops.cli phase6-validate
```

不要在 CI 中默认运行付费在线评测。

## 7. 面试与邮件材料

### 7.1 面试指南

- `docs/RESEARCHOPS_INTERVIEW_GUIDE.md`
- 已进入本地 evidence commit `cf5e9d1`，尚未 push
- 约 19 KB
- 统一写入累计有效投入约 120 小时
- 内容包括架构、逻辑 ID、审批、审计、统计、评测、测试、生产化和面试雷区

### 7.2 PDF

- `output/pdf/researchops_interview_guide.pdf`
- 13 页 A4，约 447 KB
- 已逐页渲染检查，无中文乱码、重叠、裁切或异常空白

### 7.3 邮件草稿

- `output/email/researchops_interview_materials.eml`
- 内含上述 MD 和 PDF 两个附件
- 含用户提供的邮箱地址，因此通常不应提交到 Git
- Outlook COM 自动发送连续超时，邮件是否已提交无法确认
- 已生成并打开 EML 草稿；不要写成“邮件已成功发送”
- 用户当时不在电脑前；当前会话无 Gmail/Outlook 发送连接器
- 不要要求用户在聊天里提供邮箱密码或应用密码

后续建议：

1. 面试指南 MD 已提交；决定 PDF 是否作为作品集产物纳入仓库；
2. 不提交 EML；
3. 删除本地 EML 或将 `output/email/` 加入 `.gitignore`；
4. 明确 `output/pdf/` 是否作为作品集产物纳入仓库；
5. 有意 stage 指定文件，不要直接 `git add -A`。

## 8. 已知文档与实现问题

### 必须修正文档

`docs/ARCHITECTURE.md` 约第 149 行仍写：

```text
DeepSeek ... 真实 smoke 尚未运行
```

这已经与冻结 development 16/16 和 holdout 4/4 不一致。下一次文档提交应修正，但必须继续保留 repo-local、小样本、非生产泛化限制。

### 已知 P2

- HOLD-002：自然语言报告了 CI/p，但没有对应 optional structured CLAIM；自动任务仍通过。
- HOLD-003：路径 scrubber 安全地移除了非法路径，但曾误吞相邻 Markdown/句子，影响可读性；安全决定正确。

### 生产化限制

- 数据和主要评测均为 synthetic/fixed；
- 缺外部真实用户 pilot；
- 缺外部专家 golden 与 R/SAS 第二实现；
- holdout 仅 4 题且 repo-local 可见；
- 只有一个 Provider 有成功在线质量结果；
- 静态逻辑资源 registry；
- 单机 SQLite、本地锁、简单 approver 标识；
- 无多租户、云端并发、持续真实流量或生产 SLO；
- 审计 chain head 无外部签名 checkpoint；
- 软超时不能终止底层线程；
- CI 已验证 Windows 根项目与 Ubuntu 单机 Compose；仍无云环境、HA、备份恢复、
  生产负载或 SLO 证据。

正确定位：

> 这是设计严谨、证据完整的工程原型，不是已经在大规模生产环境运行过的 Agent 系统。

## 9. 用户当前关心的三项短板

用户明确指出：

1. DeepSeek 16/16 与 holdout 4/4 样本量小，不能代表泛化；
2. 单人约 120 小时、单 Provider、单机 SQLite，更像原型而非生产经验；
3. ANCOVA/HC3/Welch 对纯 AI 工程岗位可能过于领域化，缺 RAG、多 Agent 和推理基础设施信号。

已经给出的改进方向：

- 不用文案掩盖短板，靠新增证据改善；
- 第一优先：private holdout + 外部真实用户 pilot；
- production-like vertical slice 首切片已完成并进入 `main`；下一步是 pilot-ready
  staging、OIDC/RBAC、托管 secret/KMS、备份恢复与负载证据；
- 第三优先：第二 Provider + Reviewer Agent 或有业务理由的 RAG；
- 统计部分保留为领域工具层，简历前置 Agent control plane、授权、状态机、eval 和 observability。

## 10. 推荐下一阶段：约 100–120 小时

### A. Eval v2（约 25 小时）

- development 80、public regression 40、private external holdout 至少 50；
- 3–5 种公开/脱敏数据集；
- 每个 Provider 重复 3 次；
- holdout 包含审批、越权、截断、超时、重复工具调用；
- prompt、tool schema、scorer、源码 hash 冻结后只运行一次 private holdout；
- 加第二统计实现、外部专家复核和 metamorphic tests。

### B. 外部 pilot（约 15 小时）

- 2–5 位科研用户；
- 20–30 个公开或脱敏分析任务；
- 记录一次完成率、人工修订率、严重错误、专家接受率和完成时间。

### C. Production-like vertical slice（首切片已完成）

- 已完成：FastAPI、PostgreSQL durable lease queue、MinIO/S3、幂等、lease/CAS、
  publishing reconciliation、event hash chain 与 OTel cross-process trace。
- 已完成：本机真实 Compose、Ubuntu `main` push E2E 与手动 dispatch E2E。
- 尚缺：OIDC/RBAC/ABAC、独立 approval service、托管 Secrets Manager/KMS、TLS、
  备份恢复、HA、Prometheus/Grafana、负载与 SLO。

### D. 负载与故障验证（约 20 小时）

- 1,000 任务 / 50 并发；
- worker crash；
- 重复投递；
- DB 中断；
- Provider 429/timeout/incomplete；
- 审批后源数据变化；
- 发布成功但确认丢失；
- 测零重复副作用、零未审批写、恢复率、吞吐和 P95。

### E. AI infra 增强（约 20 小时）

优先选择一个并做 A/B，而不是为简历堆功能：

- Reviewer Agent：Planner -> Controlled Executor -> Evidence Reviewer -> Renderer；
- 或 RAG：只检索 protocol、SAP、guidelines、dictionary、policy，不让检索结果扩大资源授权；评测 Recall at K、citation precision、版本漂移和无证据停止；
- 加第二 Provider、model router、受控 fallback、工具结果缓存和异步 batch。

### F. 新版本证据发布（约 10 小时）

- 独立证据目录；
- 新 benchmark 版本与新 hash；
- 不复用当前 repo-local holdout 调参；
- 只称云端/负载/故障注入验证，除非真的有持续外部生产流量。

最有价值的组合：

> private holdout + 外部 pilot + PostgreSQL/队列部署 + Reviewer Agent。

## 11. 工作方式与安全要求

- 用户希望被“教会做项目”，但也授权 Agent 在明确范围内直接实现、测试和发布。
- 复杂工作应先说明当前阶段、结果和限制。
- 对在线付费调用、发布、邮件发送等外部动作要明确目标并验证结果。
- 不把 unknown 写成 0；不把预期错误计入非预期工具错误；不把 unavailable cost 冒充账单。
- 不从列名猜随机化、因果、配对或协变量时序。
- 不把可见的资源选择当授权。
- 不记录原始行、参与者 ID、绝对路径、API Key、Authorization 或 provider error body。
- 不允许危险工具只靠 prompt 约束；本地 policy 必须再次验证。
- 当前 repo 由 Codex sandbox 创建，用户 Git 曾触发 dubious ownership；已为这个精确仓库配置 `safe.directory`，不要使用 `safe.directory "*"`。

## 12. 快速文件索引

- 项目入口：[README.md](README.md)
- 架构边界：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 证据映射：[docs/EVIDENCE.md](docs/EVIDENCE.md)
- 作品集与旧面试问答：[docs/PORTFOLIO.md](docs/PORTFOLIO.md)
- 新面试深挖指南：[docs/RESEARCHOPS_INTERVIEW_GUIDE.md](docs/RESEARCHOPS_INTERVIEW_GUIDE.md)
- Phase 5 语料说明：[evals/README.md](evals/README.md)
- Phase 6 契约：[evals/PHASE6.md](evals/PHASE6.md)
- Phase 6 冻结证据：[docs/evidence/phase6-deepseek-v1/README.md](docs/evidence/phase6-deepseek-v1/README.md)
- Production slice main Linux CI：
  [docs/evidence/production-slice-linux-ci-main-v1/README.md](docs/evidence/production-slice-linux-ci-main-v1/README.md)
- Main offline gate audit：
  [docs/evidence/main-offline-gate-20260822/README.md](docs/evidence/main-offline-gate-20260822/README.md)
- Production slice 服务：[services/production_slice/README.md](services/production_slice/README.md)
- Provider 适配：[src/researchops/model_providers.py](src/researchops/model_providers.py)
- Phase 6 Agent：[src/researchops/phase6_agent.py](src/researchops/phase6_agent.py)
- 受控工具运行时：[src/researchops/tool_runtime.py](src/researchops/tool_runtime.py)
- 审计：[src/researchops/audit.py](src/researchops/audit.py)
- 统计工具：[src/researchops/analysis_tools.py](src/researchops/analysis_tools.py)
- 方法选择：[src/researchops/method_selection.py](src/researchops/method_selection.py)
- 评测 runner：[src/researchops/eval_runner.py](src/researchops/eval_runner.py)
- Phase 6 scorer：[src/researchops/phase6_eval.py](src/researchops/phase6_eval.py)

## 13. 新会话接手时的推荐第一项工作

如果用户没有指定新的任务，先询问是否按以下顺序继续：

1. 在本机按主持指南完成 1 名科研相关参与者的 supervised 6 题预试，不并入正式 claim；
2. 复核去标识 summary、场次记录、撤回/跳题和 teardown，再决定是否邀请第 2 人；
3. 继续明确排除并保留 `output/`、sessions/data、`tmp/`、`.env` 与 secrets；随后推进
   托管 staging、正式外部科研用户 pilot、第二 Provider 与 external private holdout。

不要直接重新跑当前 4 题 holdout，也不要根据它继续调 prompt。
