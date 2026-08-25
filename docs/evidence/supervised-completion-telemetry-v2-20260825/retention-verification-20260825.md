# Supervised Completion Telemetry v2 — retention verification 2026-08-25

本页记录 campaign `EXT-PILOT-01A605022746D203` 完成后的脱敏 retention 核验。它只证明
本机 supervised pretest 的数据库 deadline、Windows Scheduled Task 和一次实际 maintenance
运行；不是生产 scheduler、90 天 SLA、云备份删除证明或不可篡改审计。

核验 SQL 没有选择、输出或记录 participant/attempt ID，也没有读取 feedback/Agent 正文、
notes 或 token；证据采集未读取或输出 secret values。Maintenance container 为连接 PostgreSQL
会读取其挂载的数据库 secret，但没有挂载/读取 Provider Key；全程没有启动 worker 或模型调用。

## Scheduled Task snapshot

| 字段 | 结果 |
| --- | --- |
| Task | `ResearchOps-Pilot-Retention` |
| Exists / enabled / state | `true / true / Ready` |
| Trigger | enabled daily，interval `1` |
| Start when available | `true` |
| Multiple instances | `IgnoreNew` |
| Execution time limit | `PT30M` |
| Previous successful run | `2026-08-24 16:36:01 +08:00`，result `0` |
| Verified manual run | `2026-08-25 15:07:17 +08:00`，result `0` |
| Follow-up latest-run snapshot | `2026-08-25 16:36:01 +08:00`，result `0`；本页未独立判定触发来源 |
| Task Scheduler `NumberOfMissedRuns` at snapshot | `0` |

Action 直接执行 Docker Desktop CLI：当前仓库的 `compose.yaml`、`maintenance` profile、
`run --rm retention`。它不经过 shell 拼接，不包含 online profile、worker、Provider Key、
`down -v` 或 volume delete。

## Aggregate database proof

只读 SQL 仅返回计数、布尔值和 retention 天数：

```text
participant_count=1
participant_lower_bound_valid=true
participant_upper_bound_valid=true
minimum_retention_days=90
maximum_retention_days=90
participant_records_due_now=0
feedback_count=4
feedback_deadlines_match_participant=true
feedback_records_due_now=0
campaign_status=complete
attempt_count=6
safety_incident_count=0
```

PostgreSQL 同时强制 `delete_by >= created_at` 和
`delete_by <= created_at + interval '90 days'`。Feedback deadline 继承 participant deadline；
participant 删除后 web session、attempt 与 feedback 通过外键 cascade 删除。

## Actual maintenance run

手动触发 Scheduled Task 后：

- task 返回 `Ready`，`LastTaskResult=0`；Task Scheduler 核验快照中的
  `NumberOfMissedRuns=0`；
- 当前没有到期 participant，因此 `1` 个 participant aggregate 与 `4` 条 feedback aggregate
  均未被提前删除；
- `delete_by <= 90 days` 与 feedback deadline binding 继续为 true；
- PostgreSQL volume 保留，临时 container/network 已移除；
- retention service 没有挂载 `provider_api_key`，`secret_values_printed=false`。

`LastTaskResult=0` 证明进程成功退出，不证明删除了某个具体记录，也不保证未来机器关机、
休眠或长期未登录时每天都能准时运行。`StartWhenAvailable=true` 只能在条件恢复后补跑。

## Deadline behavior and boundaries

Daily runner 使用未来一天的普通 retention cutoff，以及已撤回六天的 withdrawal cutoff，
目标分别是不晚于第 90 天和第 7 天。真实保证仍依赖：

- 机器与 Docker Desktop 可用；
- Scheduled Task 未被禁用或篡改；
- PostgreSQL volume 可访问；
- Provider、代理、备份和系统日志具有独立 retention 配置。

应用数据库 purge 不能冒充 Provider 或备份删除。若未来 private holdout 使用 participant-derived
数据，撤回或到期删除导致 corpus 内容变化时，原 corpus commitment 与 freeze 必须失效；不得
静默删题后继续或重跑同一 freeze。

[返回本轮长期证据](README.md)
