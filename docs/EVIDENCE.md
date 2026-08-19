# 作品集证据索引

本页把 README 中的主要工程声明映射到可复核产物。仓库只允许提交聚合、脱敏的证据快照；SQLite、逐题 JSONL、临时目录和在线运行输出仍由 `.gitignore` 排除。

## 当前验证快照

验证日期：2026-08-19。

| 声明 | 当前结果 | 证据 |
| --- | --- | --- |
| 固定离线任务全部通过 | 50/50，6 类均为 100% | [`eval_summary.md`](../artifacts/portfolio_baseline_provider/eval_summary.md)、[`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 非预期工具错误 | 0/45 attempts，0% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 主动注入错误被正确处理 | 毛工具错误 11/45，24.44% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 安全违规 | 0/50 | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 证据引用 | 21/21，100% | [`eval_report.json`](../artifacts/portfolio_baseline_provider/eval_report.json) |
| 离线延迟 | P50 100.38 ms；P95 411.03 ms | [`eval_summary.md`](../artifacts/portfolio_baseline_provider/eval_summary.md) |
| 评测来源可复现 | 语料、源码、数据、依赖和产物 SHA-256 已记录 | [`eval_manifest.json`](../artifacts/portfolio_baseline_provider/eval_manifest.json) |
| 50 条审计链有效 | audit index 中全部 `valid=true` | [`eval_audit_index.json`](../artifacts/portfolio_baseline_provider/eval_audit_index.json) |
| 当前自动化测试 | 144/144 通过 | `python -m unittest discover -s tests -v` |
| DeepSeek development | 16/16；28 requests；71,039 tokens；P50/P95 7.65/17.40 s | [summary](evidence/phase6-deepseek-v1/development/phase6_summary.md)、[report](evidence/phase6-deepseek-v1/development/phase6_report.json)、[manifest](evidence/phase6-deepseek-v1/development/phase6_manifest.json)、[audit index](evidence/phase6-deepseek-v1/development/phase6_audit_index.json) |
| DeepSeek repo-local holdout | 4/4；6 requests；16,854 tokens；P50/P95 5.05/14.59 s | [summary](evidence/phase6-deepseek-v1/holdout/phase6_summary.md)、[report](evidence/phase6-deepseek-v1/holdout/phase6_report.json)、[manifest](evidence/phase6-deepseek-v1/holdout/phase6_manifest.json)、[audit index](evidence/phase6-deepseek-v1/holdout/phase6_audit_index.json) |

Phase 5 的 100.38/411.03 ms 是本机离线组件/控制面执行时间，模型调用为 0，因此 `$0` 只代表确定性离线模式。DeepSeek 行则是顺序在线评测中的 Agent 段延迟，同样不能解释为生产 SLA。

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

一键重新生成当前离线证据：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\portfolio_demo.ps1
```

对指定的新目录复核哈希、审计链和敏感 canary：

```powershell
.\.venv\Scripts\python.exe scripts\verify_phase5_artifacts.py artifacts\YOUR_NEW_RUN
```

CI 使用相同流程，从模拟数据和固定语料重建 50 题结果；它不读取 API Key，也不运行在线评测。
