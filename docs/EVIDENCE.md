# 作品集证据索引

本页把 README 中的主要工程声明映射到可复核产物。仓库只允许提交聚合、脱敏的证据快照；SQLite、逐题 JSONL、临时目录和在线运行输出仍由 `.gitignore` 排除。

## 当前验证快照

验证日期：2026-08-23。

| 声明 | 当前结果 | 证据 |
| --- | --- | --- |
| Phase 5 历史作品集基线 | 对应其冻结 source/manifest 的 50/50，6 类均为 100% | [`eval_summary.md`](../artifacts/portfolio_baseline_provider/eval_summary.md)、[`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| Phase 5 P1 事故快照 | 旧 main run 32568017243 为 44/50、evidence 10/21，但 workflow 错误显示绿色 | [门禁审计](evidence/main-offline-gate-20260822/README.md)、[旧 GitHub run](https://github.com/cedRiC874/researchops-agent/actions/runs/32568017243) |
| 当前 `main` Phase 5 | `main@a20fdfd8` run 32585792937：246 个根测试通过；Phase 5 为 50/50、evidence 21/21、profile valid、50 条 audit chain 有效 | [current main run](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792937)、[修复与门禁证据](evidence/main-offline-gate-20260822/README.md) |
| 非预期工具错误 | 0/45 attempts，0% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 主动注入错误被正确处理 | 毛工具错误 11/45，24.44% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 安全违规 | 0/50 | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 证据引用 | 21/21，100% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 离线延迟 | P50 100.38 ms；P95 411.03 ms | [`eval_summary.md`](../artifacts/portfolio_baseline_provider/eval_summary.md) |
| 评测来源可复现 | 语料、源码、数据、依赖和产物 SHA-256 已记录 | [`eval_manifest.json`](../artifacts/portfolio_baseline_provider/eval_manifest.json) |
| 50 条审计链有效 | audit index 中全部 `valid=true` | [`eval_audit_index.json`](../artifacts/portfolio_baseline_provider/eval_audit_index.json) |
| 当前自动化测试 | `main@a20fdfd8`：根测试 246/246；pilot staging 42/42；production slice 18/18 | [offline run](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792937)、[pilot run](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792915)、[production run](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792929) |
| DeepSeek development | 16/16；28 requests；71,039 tokens；P50/P95 7.65/17.40 s | [summary](evidence/phase6-deepseek-v1/development/phase6_summary.md)、[report](evidence/phase6-deepseek-v1/development/phase6_report.json)、[manifest](evidence/phase6-deepseek-v1/development/phase6_manifest.json)、[audit index](evidence/phase6-deepseek-v1/development/phase6_audit_index.json) |
| DeepSeek repo-local holdout | 4/4；6 requests；16,854 tokens；P50/P95 5.05/14.59 s | [summary](evidence/phase6-deepseek-v1/holdout/phase6_summary.md)、[report](evidence/phase6-deepseek-v1/holdout/phase6_report.json)、[manifest](evidence/phase6-deepseek-v1/holdout/phase6_manifest.json)、[audit index](evidence/phase6-deepseek-v1/holdout/phase6_audit_index.json) |
| Eval v2 public foundation | 完整 campaign 仍为 `design_only`；120 public tasks 全部 internal-ready；private holdout 未授权 | [设计](../evals/EVAL_V2.md)、[campaign](../evals/v2/campaign.json)、[dataset manifest](../evals/v2/external_datasets.json)、[task schema](../evals/v2/public_task_schema.json)、[internal review](../evals/v2/internal_review.json) |
| DeepSeek public-regression candidate | 一次性运行 `complete`；Provider system 68/93（73.12%），三轮 23/31、22/31、23/31；fault harness 27/27；保守成本 CNY 0.908142 | [证据说明](evidence/eval-v2-public-regression-deepseek-v1/README.md)、[summary](../artifacts/eval_v2_public_regression/deepseek-v1/public_regression_summary.md)、[report](../artifacts/eval_v2_public_regression/deepseek-v1/public_regression_report.json)、[manifest](../artifacts/eval_v2_public_regression/deepseek-v1/artifact_manifest.json)、[candidate](../evals/v2/public_regression_candidate.json) |
| Production-like vertical slice | FastAPI → PostgreSQL lease queue → aggregate inspect → S3/MinIO → OTel；PR #2 已合并；`main` push 与手动 dispatch 的 Ubuntu 真实 Compose E2E 均通过 | [长期 CI 快照](evidence/production-slice-linux-ci-main-v1/README.md)、[服务说明](../services/production_slice/README.md)、[验证快照](../services/production_slice/VERIFICATION.md)、[Linux workflow](../.github/workflows/production-slice-e2e.yml)、[Compose](../services/production_slice/compose.yaml) |
| Pilot-ready staging | PR #5 regular merged；`main` 无 Provider Key Linux run 42/42，真实 PostgreSQL 与 offline API Compose/teardown/final gate 均通过；尚无参与者或在线 Provider 结果 | [长期 CI 快照](evidence/pilot-staging-linux-ci-main-v1/README.md)、[main run](https://github.com/cedRiC874/researchops-agent/actions/runs/32585792915)、[服务验证](../services/pilot_staging/VERIFICATION.md)、[workflow](../.github/workflows/pilot-staging-ci.yml) |
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
