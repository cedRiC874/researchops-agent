# 字段来源和缺失语义

`S` 指固定基线中 `docs/evidence/phase6-deepseek-depth60-v1/public_summary.json`；
`F` 指本目录 `catalog.mjs` 的人工合成展示 fixture。
字段来源只用于显示文本或核查，不作为文件读取入口。

展示字段采用 `{value, availability, note, source}`：

| availability | 页面 | 语义 |
| --- | --- | --- |
| known | 原值 | 仅此状态的明确数值 0 显示为 0；known + null/undefined 防御性显示未知 |
| unknown | 未知 | 真实数值无法确定，例如历史账单 null；不能累计成零 |
| not_provided | 未提供 | 来源缺少此字段/明细；缺少整个字段也显示未提供 |
| not_applicable | 不适用 | 合成展示没有真实在线账单等不适用项 |
| not_published | 未公开 | 来源明确省略的内容，例如历史逐运行标识 |

null 的语义由来源映射决定，不全局假定等于 0、未执行或不适用。
四层状态各自有标签、值和依据。历史描述属于来源的声明，不是本页新的验证结论。

| 页面字段 | 现有来源 / 映射 | 类型 | 缺失处理 | 脱敏及解释边界 |
| --- | --- | --- | --- | --- |
| 运行 ID | S 未提供；F 固定 DEMO ID | nullable string | 历史未公开 | 不以 public_evidence_id 冒充 run_id |
| 公开证据 ID | S.public_evidence_id | nullable string | F 不适用 | 是材料标识，不是工具 evidence ID |
| 数据来源 | 固定 catalog.kind + sourceLabel | enum | 不推断实时状态 | historical 与 synthetic 明确区分，全部固定快照 |
| 范围 | S.plan.split/holdout_selected、outcome.planned；F 合成范围 | string/counts | 未提供 | S 为 development 60 项、holdout 0；不显示原始路径 |
| 执行/展示版本 | S.plan.runner_version/model_alias；F 展示版本 | string | 未提供/不适用 | 模型别名可变；不查询或选择 Provider |
| 执行源码锚点 | S.repository_anchor.commit | nullable string | F 不适用 | 与读取基线 BASELINE 分开 |
| 材料记录时间 | S.documented_at_utc | UTC datetime | F 不适用 | 不是运行起止时间 |
| 起止时间 | S 无；F 无真实时间 | nullable datetime | 未提供 | 不从材料时间反推 |
| 执行状态 | S.outcome.status/completed/planned/runtime_failures/harness_errors | enum/counts | 未提供 | completed 60/60 不等于检查通过 |
| 逐题检查 | S.outcome.passed/failed_attempted；F 明示模拟 | counts/state | 明细未公开或未检查 | 历史 20/60；不生成逐题结果 |
| 归档回验 | S.integrity.all_audit_chains_valid/artifact_hashes_valid/consume_terminal_binding_valid | boolean claims | F 未提供 | 显示历史记录称通过；本页未回验 |
| 外部验收 | S 无对应回执 | nullable state | 历史未提供；F 不适用 | 不从 CI、链校验或执行通过推断 |
| 时间线 | S 无公开事件；F.events | event[] / nullable time | 空数组同时有缺失说明 | 历史不补造；F 每条标模拟；暂停不伪造终态 |
| 结果摘要 | S.outcome 聚合计数；F 人工聚合示例 | result[] | 失败 F 未提供 | 不展示原始模型正文、数据行 |
| evidence 与工具关联 | S 只有材料 ID；F DEMO-EV-001 → DEMO-TOOL-001 | nullable ID | 历史工具关联未提供 | F 明示人工关联；不依据顺序或正文猜测 |
| 模型请求 | S.usage.model_requests | integer | 未提供 | 103 为批次 SDK 请求口径，不是网络/账户总量；F 标模拟 |
| 输入/输出 tokens | S.usage.input_tokens/output_tokens | nullable integer | F 失败未知；F 部分完成不适用 | 235943/54396 不分摊到逐题 |
| 用量覆盖 | S.usage.coverage/status | percentage/state | 未提供 | 本历史覆盖 100%；不对未知用量补零 |
| 整批墙钟耗时 | S 无 | nullable duration | 未提供 | 不相加 P50/P95 估计整批时长 |
| Agent 段 P50/P95 | S.latency_ms.p50_nearest_rank/p95_nearest_rank | milliseconds → seconds | 未提供 | 固定保留 3 位小数；60 项；不当成 Provider 延迟或 SLA |
| 估算费用 | S.cost.estimated_cost/currency/estimate_method | decimal string + currency | 无价格未提供；F 不适用或未提供 | CNY 1.197393；冻结峰时、全部缓存未命中；不实时重新定价 |
| 实际账单 | S.cost.actual_provider_bill=null | nullable decimal | unknown；F not_applicable | 本地阈值不是实际账单硬上限 |
| 失败原因 | S.failure_reason_counts；F 稳定模拟错误码 | code/count/text | 根因未知或明细未提供 | 原因可重叠，不能合计当失败任务数；不显示原始错误正文 |
| 未执行项 | S.outcome.not_started=0；F 明示模拟 | count/text | 内部步骤未提供 | 0 只指任务未开始数，不表示所有内部步骤都执行 |
| 下一步及限制 | 固定公开边界说明、S.claim_boundary；F 说明 | safe text | 未提供 | 没有执行/恢复入口，不引申运行授权 |

所有动态文本使用 DOM/textContent。来源、标识符、错误说明均不会被解释为 HTML、Markdown 或 URL。
只复制白名单投影中的有限格式 ID；源数据目录、脚本、文档和原始副本没有 HTTP 下载路由。
