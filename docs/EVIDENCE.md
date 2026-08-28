# 作品集证据索引

本页把 README 中的主要工程声明映射到可复核产物。仓库只允许提交聚合、脱敏的证据快照；SQLite、逐题 JSONL、临时目录和在线运行输出仍由 `.gitignore` 排除。

## 当前验证快照

验证日期：2026-08-28。

| 声明 | 当前结果 | 证据 |
| --- | --- | --- |
| Phase 5 历史作品集基线 | 对应其冻结 source/manifest 的 50/50，6 类均为 100% | [`eval_summary.md`](../artifacts/portfolio_baseline_provider/eval_summary.md)、[`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| Phase 5 P1 事故快照 | 旧 main run 32568017243 为 44/50、evidence 10/21，但 workflow 错误显示绿色 | [门禁审计](evidence/main-offline-gate-20260822/README.md)、[旧 GitHub run](https://github.com/cedRiC874/researchops-agent/actions/runs/32568017243) |
| 当前 `main` Phase 5 | `main@b9054774` run 33106332631：608 个根测试通过；canonical Nehalem/x86-v2 identity、Phase 5 50/50、evidence 21/21、profile valid | [current main run](https://github.com/cedRiC874/researchops-agent/actions/runs/33106332631)、[PR #24/#25 main 快照](evidence/kimi-v8-diagnostic-main-ci-v1/README.md)、[历史门禁审计](evidence/main-offline-gate-20260822/README.md) |
| 非预期工具错误 | 0/45 attempts，0% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 主动注入错误被正确处理 | 毛工具错误 11/45，24.44% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 安全违规 | 0/50 | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 证据引用 | 21/21，100% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 离线延迟 | P50 100.38 ms；P95 411.03 ms | [`eval_summary.md`](../artifacts/portfolio_baseline_provider/eval_summary.md) |
| 评测来源可复现 | 语料、源码、数据、依赖和产物 SHA-256 已记录 | [`eval_manifest.json`](../artifacts/portfolio_baseline_provider/eval_manifest.json) |
| 50 条审计链有效 | audit index 中全部 `valid=true` | [`eval_audit_index.json`](../artifacts/portfolio_baseline_provider/eval_audit_index.json) |
| 当前自动化测试 | `main@b9054774` 的 offline run 33106332631、pilot run 33106333010 与 production run 33106332748 均成功 | [offline run](https://github.com/cedRiC874/researchops-agent/actions/runs/33106332631)、[pilot run](https://github.com/cedRiC874/researchops-agent/actions/runs/33106333010)、[production run](https://github.com/cedRiC874/researchops-agent/actions/runs/33106332748)、[PR #24/#25 main 快照](evidence/kimi-v8-diagnostic-main-ci-v1/README.md) |
| DeepSeek development | 16/16；28 requests；71,039 tokens；P50/P95 7.65/17.40 s | [summary](evidence/phase6-deepseek-v1/development/phase6_summary.md)、[report](evidence/phase6-deepseek-v1/development/phase6_report.json)、[manifest](evidence/phase6-deepseek-v1/development/phase6_manifest.json)、[audit index](evidence/phase6-deepseek-v1/development/phase6_audit_index.json) |
| DeepSeek repo-local holdout | 4/4；6 requests；16,854 tokens；P50/P95 5.05/14.59 s | [summary](evidence/phase6-deepseek-v1/holdout/phase6_summary.md)、[report](evidence/phase6-deepseek-v1/holdout/phase6_report.json)、[manifest](evidence/phase6-deepseek-v1/holdout/phase6_manifest.json)、[audit index](evidence/phase6-deepseek-v1/holdout/phase6_audit_index.json) |
| Eval v2 public foundation | 完整 campaign 仍为 `design_only`；120 public tasks 全部 internal-ready；private holdout 未授权 | [设计](../evals/EVAL_V2.md)、[campaign](../evals/v2/campaign.json)、[dataset manifest](../evals/v2/external_datasets.json)、[task schema](../evals/v2/public_task_schema.json)、[internal review](../evals/v2/internal_review.json) |
| Eval v2 private custodian kit v1.1（已进入 main） | PR #14 regular merged 至 `main@16a9133d`；main offline run 32829222869 与 pilot/PostgreSQL run 32829223001 均成功；synthetic-only，真实 private release 固定拒绝，当前 request/access/claims 均为 false | [main 长期快照](evidence/eval-v2-private-custodian-main-ci-v1/README.md)、[kit](../evals/v2/private_holdout_kit/README.md)、[guide](PRIVATE_HOLDOUT_CUSTODIAN_KIT.md)、[protocol](../evals/v2/private_holdout_kit/protocol.json) |
| Completion Telemetry v2（已进入 main） | 四类受控 failure source、legacy unknown coverage、v1/v2 双 telemetry digest 与 PostgreSQL mixed-retention 已由 clean main 验证；candidate `1f6ac18e…e5ce5` 无在线成绩且不继承 v1 | [长期证据](evidence/completion-telemetry-v2-main-ci-v1/README.md)、[RFC](COMPLETION_TELEMETRY_V2_RFC.md)、[machine contract](../evals/v2/completion_telemetry_contract.json)、[candidate](../evals/v2/public_regression_candidate_v2.json) |
| Anthropic offline adapter / candidate v3（历史） | commitment `22c985e9…b2a9`，未在线运行；文件、commitment、旧 pack 与 historical evidence 保持不变 | [PR #15/#16 main 快照](evidence/eval-v2-anthropic-offline-main-ci-v1/README.md)、[predecessor contract](../evals/v2/anthropic_provider_contract.json)、[candidate v3](../evals/v2/public_regression_candidate_v3.json) |
| Anthropic Models preflight / candidate v4（历史 current-main snapshot） | PR #19 regular merged 至 `main@77911226`；冻结实现只继承 MockTransport 离线证据。其后一次 official-origin metadata 尝试使用了 CCTK token并返回 403，1 network call、0 model tokens；不构成 Anthropic或CCTK可用性/质量证据 | [main 长期快照](evidence/anthropic-models-preflight-main-ci-v1/README.md)、[Anthropic 边界](ANTHROPIC_PROVIDER.md)、[preflight contract](../evals/v2/anthropic_models_preflight_contract.json)、[candidate v4](../evals/v2/public_regression_candidate_v4.json) |
| Kimi 中国区 Models preflight / candidate v5（已进入 main 的 pre-call snapshot） | PR #21 regular merged 至 `main@c65ff65c`；candidate `105b7def…5165dffc` 在调用前锁定，只继承 MockTransport 离线证据；Chat/通用在线入口关闭，synthetic-only/private denied，post-lock receipt 与历史结果均不继承 | [main 长期快照](evidence/kimi-models-preflight-main-ci-v1/README.md)、[PR #21](https://github.com/cedRiC874/researchops-agent/pull/21)、[Kimi 边界](KIMI_PROVIDER.md)、[candidate v5](../evals/v2/public_regression_candidate_v5.json) |
| Kimi post-lock Models receipt（不属于 candidate 成绩） | `2026-08-26T09:41:49.967Z` 的一次独立授权 GET 为 verified / HTTP 200；attempts/network calls 1/1、requested/returned `kimi-k3`、认证与 exact visibility true、0 model tokens、cost null。它不授权 Chat/tools/usage/cost semantics/质量/注册/private；授权已消耗，不得重试 | [长期边界](evidence/kimi-models-preflight-main-ci-v1/README.md)、[脱敏 PR comment](https://github.com/cedRiC874/researchops-agent/pull/21#issuecomment-5423475486) |
| Kimi Candidate v6 / Chat-Pilot v1（历史调用前快照，已进入 main） | PR #23 regular merged 至 `main@27fad953`；commitment `57d0c1b0…f6f5641`；固定三场景、8-request/CNY 5 本地门禁和 synthetic-only 边界；public Provider 仍为 DeepSeek，历史结果不继承 | [PR #23 main 快照](evidence/kimi-controlled-pilot-history-main-ci-v1/README.md)、[Candidate v6](../evals/v2/public_regression_candidate_v6.json)、[Chat contract](../evals/v2/kimi_chat_completions_contract.json)、[Pilot contract](../evals/v2/kimi_controlled_pilot_contract.json)、[历史运行手册](KIMI_CONTROLLED_PILOT_RUNBOOK.md) |
| Kimi Candidate v6 post-lock failure（不属于 candidate 成绩） | 一次独立授权尝试在首请求 usage validation 阶段 fail-closed：1 request/call、0/3 场景、0 个可信解析出的 Provider tool calls、0 次 tool execution，usage/cost/Provider latency 未知；G4=`planned_not_registered`，授权已消费且不得重试 | [脱敏失败证据](evidence/kimi-controlled-pilot-usage-failure-v1/README.md)、[public projection](evidence/kimi-controlled-pilot-usage-failure-v1/public_receipt_projection.json)、[artifact commitments](evidence/kimi-controlled-pilot-usage-failure-v1/artifact_commitments.json) |
| Kimi Candidate v7 / Chat-Pilot v2（调用前离线锁定快照，已进入 main） | PR #23 regular merged 至 `main@27fad953`；commitment `2d0b9952…d223d5`；绑定三份官方 usage 文档 commitments、top/choice/both-reconciled parser、独立 Pilot v2 capability/artifacts/verifier 与 v6 CLI tombstone；其后独立 post-lock 尝试见下一行且不回填、不继承，Kimi仍未注册、Provider 1/2 | [PR #23 main 快照](evidence/kimi-controlled-pilot-history-main-ci-v1/README.md)、[Candidate v7](../evals/v2/public_regression_candidate_v7.json)、[Chat v2 contract](../evals/v2/kimi_chat_completions_contract_v2.json)、[Pilot v2 contract](../evals/v2/kimi_controlled_pilot_contract_v2.json)、[v2 runbook](KIMI_CONTROLLED_PILOT_V2_RUNBOOK.md) |
| Kimi Candidate v7 post-lock Pilot v2 failure（不属于 candidate 成绩） | 一次独立授权在首个 required-tool 请求的本地 response validation 阶段 fail-closed：1 request/call、0/3 场景、0 个可信解析出的 Provider tool calls、0 次 tool execution、0 usage observations；实际 tokens/cost/Provider latency 未知。G4=`planned_not_registered`，授权已消费且不得重试；无 raw body，根因未定 | [脱敏 v7 失败证据](evidence/kimi-controlled-pilot-v2-response-failure-v1/README.md)、[public projection](evidence/kimi-controlled-pilot-v2-response-failure-v1/public_receipt_projection.json)、[artifact commitments](evidence/kimi-controlled-pilot-v2-response-failure-v1/artifact_commitments.json) |
| Kimi Candidate v8 / Chat-Pilot v3 diagnostic successor（已进入 main，未在线运行且不可执行） | PR #25 regular merged 至 `main@b9054774`；commitment `b41269ac…1f9e962c`；v3保持v2 request bytes、接受谓词和优先级，仅为`kimi_chat_response_invalid`增加39项固定本地branch code；诊断子对象仅含schema+code，不含raw header/body、Provider ID/hash、字段值、offset、size、prompt/reasoning/tool payload、授权binding或free text。不继承v7失败或授权；public/Pilot online authorization固定为false；Kimi仍未注册、Provider 1/2 | [main 长期快照](evidence/kimi-v8-diagnostic-main-ci-v1/README.md)、[Candidate v8](../evals/v2/public_regression_candidate_v8.json)、[Chat v3 contract](../evals/v2/kimi_chat_completions_contract_v3.json)、[Pilot v3 contract](../evals/v2/kimi_controlled_pilot_contract_v3.json)、[v3 runbook](KIMI_CONTROLLED_PILOT_V3_RUNBOOK.md) |
| Pilot staging historical Candidate v6/v7 / Pack7/8 | Pack7/8 保持 DeepSeek、六题、双语内容、context 与顺序完全一致；task commitment `83363291…1555d72`。两次各 1-call post-lock failure 已发生但均不继承到 Candidate、Pack、质量或注册结论；active 配置保持 Candidate v5 / Pack6，当前源码执行继续 fail-closed | [历史状态与完整 lineage overlay](evidence/kimi-historical-status-overlays-v1/README.md)、[Pack v7](../services/pilot_staging/content/pilot_pack.supervised_v7.json)、[Pack v8](../services/pilot_staging/content/pilot_pack.supervised_v8.json)、[staging verification](../services/pilot_staging/VERIFICATION.md) |
| Kimi v1 opaque chain commitment publication boundary | v1 `event_chain.head_sha256` 是刻意公开、可与精确私有 artifact chain 关联的 opaque commitment；它不是授权 ID/hash 或授权 binding，也不授权重试或新运行 | [versioned disclosure overlay](evidence/kimi-historical-status-overlays-v1/kimi_v1_chain_linkability_disclosure.json)、[被绑定的 artifact commitments](evidence/kimi-controlled-pilot-usage-failure-v1/artifact_commitments.json) |
| DeepSeek public-regression candidate | 一次性运行 `complete`；Provider system 68/93（73.12%），三轮 23/31、22/31、23/31；fault harness 27/27；保守成本 CNY 0.908142 | [证据说明](evidence/eval-v2-public-regression-deepseek-v1/README.md)、[summary](../artifacts/eval_v2_public_regression/deepseek-v1/public_regression_summary.md)、[report](../artifacts/eval_v2_public_regression/deepseek-v1/public_regression_report.json)、[manifest](../artifacts/eval_v2_public_regression/deepseek-v1/artifact_manifest.json)、[candidate](../evals/v2/public_regression_candidate.json) |
| Production-like vertical slice | FastAPI → PostgreSQL lease queue → aggregate inspect → S3/MinIO → OTel；PR #2 已合并；`main` push 与手动 dispatch 的 Ubuntu 真实 Compose E2E 均通过 | [长期 CI 快照](evidence/production-slice-linux-ci-main-v1/README.md)、[服务说明](../services/production_slice/README.md)、[验证快照](../services/production_slice/VERIFICATION.md)、[Linux workflow](../.github/workflows/production-slice-e2e.yml)、[Compose](../services/production_slice/compose.yaml) |
| Pilot staging candidate v5/v6（已进入 main） | `main@c65ff65c` pilot run 32957003191 通过 51 项 offline contracts、1 项真实 PostgreSQL contract 与无 Provider secret Compose；v6 仍使用 DeepSeek，未运行 Kimi model，且不继承 post-lock metadata receipt 或历史结果 | [main 长期快照](evidence/kimi-models-preflight-main-ci-v1/README.md)、[main pilot run](https://github.com/cedRiC874/researchops-agent/actions/runs/32957003191)、[服务快照](../services/pilot_staging/VERIFICATION.md)、[v6 pack](../services/pilot_staging/content/pilot_pack.supervised_v6.json) |
| Supervised 同一参与者 UX regression v2（已进入 main） | 1 completed、0 withdrawn；6/6 terminal，4 feedback，2 个 `provider_output_incomplete`；model/tool/backend 计数 9/3/2；telemetry 与 participant projection binding 均 valid；不是第二位独立参与者或 external validation | [脱敏证据](evidence/supervised-ux-regression-v2-20260823/README.md)、[故障 001](evidence/supervised-ux-regression-v2-20260823/failures/PILOT-DIAG-20260823-001.md)、[故障 002](evidence/supervised-ux-regression-v2-20260823/failures/PILOT-DIAG-20260823-002.md)、[main 长期快照](evidence/pilot-telemetry-main-ci-v1/README.md) |
| Supervised Completion Telemetry v2（2026-08-25） | 1 completed、0 withdrawn；6/6 terminal，4 feedback，2 个 `provider_output_incomplete` 均安全分类为 `response_output_item_incomplete`；source coverage 2/2，model/tool/backend 为 9/3/2，安全 incidents 0；retention task Ready、一次手动触发 maintenance 运行 result 0、participant/feedback deadline 为 90 天上限；不主张新独立参与者或 external validation | [脱敏证据](evidence/supervised-completion-telemetry-v2-20260825/README.md)、[retention 核验](evidence/supervised-completion-telemetry-v2-20260825/retention-verification-20260825.md)、[aggregate projection](evidence/supervised-completion-telemetry-v2-20260825/aggregate_projection.json)、[故障 001](evidence/supervised-completion-telemetry-v2-20260825/failures/PILOT-CTV2-20260825-001.md)、[故障 002](evidence/supervised-completion-telemetry-v2-20260825/failures/PILOT-CTV2-20260825-002.md) |
| Phase 5 hosted-CPU containment（已进入 main） | `main@4a3f5cf8` 将离线重建固定到 Nehalem/x86-v2；main run 32640814960 为 246 tests、50/50、21/21、profile valid；仅重绑定硬件敏感 ID/corpus lineage，未改核心源码或 locked candidate | [main run 32640814960](https://github.com/cedRiC874/researchops-agent/actions/runs/32640814960)、[main 长期快照](evidence/pilot-telemetry-main-ci-v1/README.md)、[Phase 5 说明](../evals/README.md) |
| Internal self-pilot | 两个旧 corpus 的 12 题内部 session 已完成；`session-02` 可理解/有用 12/12、需专家复核 3/12、明显问题 1/12、安全担忧 0/12、严格机器合同 3/12 | 本地 `artifacts/self_pilot/session-01/02`（不提交逐题 state）；[使用指南](SELF_PILOT_GUIDE.md) |

Phase 5 的 100.38/411.03 ms 是本机离线组件/控制面执行时间，模型调用为 0，因此 `$0` 只代表确定性离线模式。DeepSeek 行则是顺序在线评测中的 Agent 段延迟，同样不能解释为生产 SLA。

历史 Phase 5 50/50 必须与其 source/manifest 绑定。2026-08-22 的
`main@badb7169` workflow 虽然 144 项根测试和 artifact verifier 均通过，但新重建
报告实际为 44/50、证据引用 10/21；6 个失败任务集中在 required evidence 与部分
dataset/chart exact 字段。根因是仓库规定的 clean LF CSV hash 为 `7ae3…`，而
corpus golden 绑定了本地遗留 CRLF 文件的 `db7c…`。该 main run 当时没有把
success/evidence 阈值接入退出码，因此
绿色 run 只能证明 workflow 和完整性检查完成，不能证明质量阈值通过。这一缺口列为
P1；本地 CRLF 环境的 50/50 也不能替代 clean checkout。详见
[main offline gate audit](evidence/main-offline-gate-20260822/README.md)。

PR #3 已把全部 Phase 5 corpus provenance 更新到 LF lineage，corpus SHA-256 为
`dd591862…8e67`；版本化 `phase5-ci-v1` 同时精确检查 50/50 与 21/21，workflow
也不再丢失 `eval-run` 的 native exit code。push/PR clean runs 均为 50/50、21/21；
合并后的 main run 32571384757 再次通过 152/152、50/50、21/21、profile valid 和
50 条 audit chain 有效。修复前 44/50 产物会以稳定 reason 和 exit 1 被拒绝，P1
已经关闭。

Eval v2 public-regression 只运行了锁定 public candidate：31 道 Provider 行为题各重复三次，另有 9 道纯本地 fault harness 各重复三次。68/93 归因于 `DeepSeek + 锁定控制面`，不是模型单体或 LLM 规划准确率；27/27 fault 结果也未进入模型分母。完整 campaign 仍非 frozen，所有报告继续禁止 private holdout、未知生产集和跨 Provider 泛化声明。内部复核覆盖 120 个 ready tasks，但不是外部领域复核；外部专家复核为 0/3，private corpus 和第二 Provider仍未完成。

Production-like vertical slice 位于独立 `services/production_slice/`；Eval v2 candidate commitment 在新增服务后仍通过冻结 verifier，反过来，本次 68/93 也没有评测该新服务层。PR #2 已合并至 `main@badb7169`。Ubuntu push run 32568017244 的真实 E2E 用时 68.861 秒：job 一次完成 `queued/claimed/publishing/succeeded`，344×8 aggregate profile、4-event PostgreSQL hash chain、MinIO metadata/hash/bytes、幂等复用及 API→worker Trace ID 均通过，secret leak 0、model calls 0；手动 dispatch run 32568233292 也成功。该结果仍不能外推为 HA、云 IAM/KMS/TLS、备份恢复、生产 SLA 或负载容量。长期证据见 [main Linux CI 快照](evidence/production-slice-linux-ci-main-v1/README.md)。

## 模拟试验分析证据

源数据是 240 行、10 列的完全模拟随机对照试验数据。随访收缩压缺失 28 例；两种分析均纳入 212 例。

| 分析 | treatment - control | 95% CI | p 值 | Evidence ID |
| --- | ---: | ---: | ---: | --- |
| ANCOVA，基线校正、HC3 | -5.6069 mmHg | [-7.9351, -3.2787] | 3.82e-6 | `E-7C87BB6C88EB` |
| Welch，未校正敏感性分析 | -6.7887 mmHg | [-10.8425, -2.7349] | 0.001134 | `E-B93CD9DC7751` |

- 完整证据：[analysis bundle](../artifacts/phase3/analysis_bundle.json)
- 聚合图表：[effect estimates](../artifacts/phase3/effect_estimates.png)

负值表示治疗组随访收缩压更低；只有研究方案另行定义 `beneficial_direction=lower` 时，系统才能进一步使用“获益”措辞。

请求的目标人群是 ITT，但当前实现是 available-case。证据包明确记录 `requested_population=intention_to_treat` 和 `realized_population=available_case`，不能把结果描述成完整 ITT。

## 审批、重试与发布证据

Phase 4 离线演示记录了：

- 聚合证据读取第一次出现 `artifact_temporarily_locked`，第二次成功。
- `publish_aggregate_results` 作为 `controlled_write` 在批准前暂停。
- 人工批准后恢复执行，发布 manifest 绑定源文件和目标文件 SHA-256。
- 运行最终完成，事件序列和哈希链可验证。

证据：

- [脱敏审计导出](../artifacts/phase4/audit_export.json)
- [发布 manifest](../artifacts/phase4/releases/demo-release/release_manifest.json)

## 在线 Agent 状态

Phase 6 已完成真实 `deepseek-v4-flash` 在线评测。冻结配置为 runner `1.6.0`、单次响应上限 2000 tokens、source SHA-256 `24a28a7a19fb4e8546f27af995c4baa24e20a2dadedbc9a7efc1926dfb10626c`、corpus SHA-256 `7c478dd2f90ffb2796fd18dfe77129570a6ee9ea06b343a4cdf55f4e99500da0`、split SHA-256 `d19bc5a0516649c27e1069f8f57f8ba8a0172a03524a8c8e8a10b6c31eabbe6e`。

| Split | 成功率 | Requests | Input/output/total tokens | P50/P95 |
| --- | ---: | ---: | ---: | ---: |
| development | 16/16 | 28 | 57,723 / 13,316 / 71,039 | 7.65 / 17.40 s |
| repo-local non-secret holdout | 4/4 | 6 | 13,051 / 3,803 / 16,854 | 5.05 / 14.59 s |

Development 覆盖 2 个审批暂停任务；holdout 不含审批任务。两组运行的工具名、参数、证据 grounding、安全、usage integrity 和审计链均通过冻结评分器。成本仍为 `null / unavailable`，因为尚未提供覆盖缓存命中、缓存未命中和输出 token 的完整版本化价格表。

OpenAI 路径保留独立事实：两次单题运行在首个有效模型响应前失败；修复 Key 后的独立最小 API 请求返回 HTTP 429，当前环境无法配置 OpenAI API 计费。这不影响 DeepSeek 成绩，也不能解释为 OpenAI 模型质量结论。

这些不是未知或抗污染泛化成绩：4 个 holdout 任务及金标均位于仓库内，样本量很小，也没有审批场景。延迟来自顺序评测中的 Agent 段，不能作为生产 SLA。

人工复核披露两个 P2：HOLD-002 的 CI/p 出现在 prose 中，但没有对应 optional CLAIM；HOLD-003 的非法路径经安全清洗后出现 `[PATH_REDACTED]`，拒绝和 reason code 正确，但文本可读性受损。

## 独立复核

### Pilot-ready staging 工程与 main CI 证据

`services/pilot_staging/` 当前有实现、本地验证与 GitHub clean Linux CI 证据，没有外部
参与者结果，也没有执行新的付费 Provider run。验证分为：

- 38 个 API/domain/config/script/CI 无网络测试：邀请、session/CSRF、完整 consent、双用户隔离、
  online kill switch/worker heartbeat、一次运行、只读轮询、实际 clarification outcome、
  failure exclusion、DLP pause、withdraw、rate/body limits 与正/负 claim gate；
- 3 个 locked candidate 与 JSON Schema tests：candidate commitment/preflight、summary 正反
  合同与 4 个 Draft 2020-12 schema 自检；
- 1 个真实临时 PostgreSQL test：migration、6 题完整 lifecycle、consent replay 幂等、
  REPEATABLE READ summary、audit chain、task-pack integrity，以及 migration checksum drift；
- Linux Docker image 构建成功，Provider 依赖为 exact version lock；没有读取 Provider Key。

Supervised mode 还将运行环境持久绑定到 campaign。测试覆盖在全部正向门槛均满足后，
supervised summary 仍固定 `external_validation_claim_allowed=false`，并验证以后由 local 或
staging 配置重新读取同一 campaign 也不能移除该 blocker。独立 Linux workflow 只创建
非 Provider CI secret、真实 PostgreSQL 和 offline API Compose；不会创建 Provider Key、
启动 online worker或调用模型。PR #5 合并后，`main@a20fdfd8` 的
[run 32585792915](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792915)
已通过全部 42 项、offline API Compose、teardown 与最终 gate；步骤级长期审计见
[pilot Linux CI snapshot](evidence/pilot-staging-linux-ci-main-v1/README.md)。

正式 3–5 人 claim 目前也保持 fail-closed：服务尚无 operator-reviewed eligibility
adjudication receipt，不能仅凭参与者自勾资格与离线招募表认定其独立性、利益冲突和
golden exposure。因此所有非 supervised summary 目前固定包含
`operator_eligibility_adjudication_not_implemented`；这是待实现门禁，不是已完成证据。

临时 PostgreSQL 容器使用 `--rm` 且没有持久 volume，验证后已停止。上述结果证明本地
staging contract 可继续部署复核，不证明公网安全、生产 SLA、外部科研用户满意度、领域
正确性、private holdout 或未知请求泛化。只有真实 cohort 达到预注册人数/交互/覆盖/
withdraw/安全/integrity 门槛后，才可在精确范围
`external_researcher_usability_on_prepared_public_data` 内报告聚合可用性。
精确命令边界、本地镜像 digest 与远端 clean run 见
[pilot staging verification](../services/pilot_staging/VERIFICATION.md) 和
[pilot Linux CI snapshot](evidence/pilot-staging-linux-ci-main-v1/README.md)。

一键重新生成当前离线证据：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\portfolio_demo.ps1
```

对指定的新目录复核哈希、审计链和敏感 canary：

```powershell
.\.venv\Scripts\python.exe scripts\verify_phase5_artifacts.py artifacts\YOUR_NEW_RUN
```

CI 使用相同流程，从模拟数据和固定语料重建 50 题结果；它不读取 API Key，也不运行在线评测。
