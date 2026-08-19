# 作品集证据索引

本页把 README 中的主要工程声明映射到可复核产物。仓库只允许提交聚合、脱敏的证据快照；SQLite、逐题 JSONL、临时目录和在线运行输出仍由 `.gitignore` 排除。

## 当前验证快照

验证日期：2026-08-19。

| 声明 | 当前结果 | 证据 |
| --- | --- | --- |
| 固定离线任务全部通过 | 50/50，6 类均为 100% | [`eval_summary.md`](../artifacts/portfolio_baseline/eval_summary.md)、[`eval_report.json`](../artifacts/portfolio_baseline/eval_report.json) |
| 非预期工具错误 | 0/45 attempts，0% | [`eval_report.json`](../artifacts/portfolio_baseline/eval_report.json) |
| 主动注入错误被正确处理 | 毛工具错误 11/45，24.44% | [`eval_report.json`](../artifacts/portfolio_baseline/eval_report.json) |
| 安全违规 | 0/50 | [`eval_report.json`](../artifacts/portfolio_baseline/eval_report.json) |
| 证据引用 | 21/21，100% | [`eval_report.json`](../artifacts/portfolio_baseline/eval_report.json) |
| 离线延迟 | P50 87.77 ms；P95 288.80 ms | [`eval_summary.md`](../artifacts/portfolio_baseline/eval_summary.md) |
| 评测来源可复现 | 语料、源码、数据、依赖和产物 SHA-256 已记录 | [`eval_manifest.json`](../artifacts/portfolio_baseline/eval_manifest.json) |
| 50 条审计链有效 | audit index 中全部 `valid=true` | [`eval_audit_index.json`](../artifacts/portfolio_baseline/eval_audit_index.json) |
| 当前自动化测试 | 110/110 通过 | `python -m unittest discover -s tests -v` |

这些延迟是本机离线组件/控制面场景的执行时间，不是生产网络延迟。模型调用为 0，因此 `$0` 只代表这个确定性离线评测模式。

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

Phase 6 已实现 20 个自然语言任务、真实 SDK 工具轨迹采集、精确参数评分、结构化 claim 绑定和审批首次暂停协议。两次单题在线 smoke 均在首个模型响应前失败；独立最小请求确认 API Key 认证成功，但 API Platform 返回 429，且当前环境无法配置 API 计费。

因此对外状态固定为：

```text
online_evaluation_status = blocked_external_billing
online_agent_success_rate = unavailable
online_usage_cost_latency = unavailable
```

不能把离线 50/50 合并成在线 Agent 规划成绩，也不能从失败运行推断隐私行为、工具准确率或真实模型成本。

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
