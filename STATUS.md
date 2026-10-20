# 项目状态

- 更新日期：2026-10-20；最新业务观察为2026-10-04，二者不是同一天。
- 已验收工程基线为PR #51合并后的main；本次作品集整理的验证另见对应PR checks，不沿用旧CI作为新版本通过。
- 该固定main的6个workflow／8个job成功；root为2535项、0 failures、0 errors、11 skips。
- S10专项11项及bridge授权重跑455项实际exit0；root原生数值exit未知和日志归属限制保留。
- 第6项完成16道开发方已知合成题的32条业务观察，不是外部未见任务验证。
- 固定流程16 pass、0模型请求；Agent 13 pass／1 fail／2 unknown、30次Provider请求。
- 30响应的usage、原生完成状态及计时完整；归档回读通过，业务并非全部通过。
- 保守费用0.057960元，实际账单未知；旧授权已消费，不重试、不补跑或继承权限。
- Depth-60保持20/60，原外部T6-B／T7未完成。状态：`open / cross-cutting / causal attribution incomplete`。
- 2026-10-11：用真实客户端（ChatGPT桌面版Codex）经MCP网关完成一次合成数据录像：聊天中声称批准被拒绝，本地CLI批准后执行成功（[录像与说明](README.md#mcp网关真实客户端真实模型)）。试录中模型指出`phase3`证据包的数据哈希与仓库数据不同：生成证据包时CSV为CRLF换行，检出为LF，内容相同；该包被冻结评测引用，保持原样，网关现在会计算并标注。过期的待审批项可用`reject`清理。
- 2026-10-20：一位医学统计方向的老师在公开数据（UCI 帕金森远程监测）上试用现场提问页面，共 3 题：1 题完成，2 题先提问澄清；`verify` 校验 3 条记录，退出码 0。试用记录含个人意见，不进仓库；发现的已知局限见 README 的“安全覆盖与缺口”一节。
- 项目自本次更新起收尾，作为作品集保留，不再新增功能；数据清洗方向另起新项目。
- [最新证据](docs/evidence/main51-controlled-observation-v1/README.md) · [原完整账本](docs/archive/STATUS-before-portfolio-cleanup-20261009.md) · [文档入口](docs/README.md)。
