# 项目状态

- 更新日期：2026-10-09；最新业务观察为2026-10-04，二者不是同一天。
- 已验收工程基线为PR #51合并后的main；本次作品集整理的验证另见对应PR checks，不沿用旧CI作为新版本通过。
- 该固定main的6个workflow／8个job成功；root为2535项、0 failures、0 errors、11 skips。
- S10专项11项及bridge授权重跑455项实际exit0；root原生数值exit未知和日志归属限制保留。
- 第6项完成16道开发方已知合成题的32条业务观察，不是外部未见任务验证。
- 固定流程16 pass、0模型请求；Agent 13 pass／1 fail／2 unknown、30次Provider请求。
- 30响应的usage、原生完成状态及计时完整；归档回读通过，业务并非全部通过。
- 保守费用0.057960元，实际账单未知；旧授权已消费，不重试、不补跑或继承权限。
- Depth-60保持20/60，原外部T6-B／T7未完成。状态：`open / cross-cutting / causal attribution incomplete`。
- [最新证据](docs/evidence/main51-controlled-observation-v1/README.md) · [原完整账本](docs/archive/STATUS-before-portfolio-cleanup-20261009.md) · [文档入口](docs/README.md)。

2026-10-11 网关演示修复：`phase3` 是 CRLF 来源的冻结夹具，数据集哈希为 `db7ce30ae0fdc9d455edfd6f107f974215aa7fc91209f73a7d1afdf208b9062c`，当前 LF 检出为 `7ae3c201ccb543b5c647c8c50b2a754294d1d62aaaa458d0f2fb4b0af990ca00`。Phase 6 冻结评测绑定该证据包文件哈希，测试引用固定 evidence ID，因此保留原产物。网关读取证据成功后按当前文件字节计算核对；本仓库直接调用的 `warnings` 条目为 `code=gateway_dataset_line_endings_only`、`relationship=line_endings_only`、`matching_line_ending=CRLF`，并列出 `current_dataset_sha256` 与 `evidence_dataset_sha256`。完全一致不告警，换行转换也不匹配时为 `gateway_dataset_sha256_mismatch`；[完整告警示例](services/mcp_gateway_v1/README.md#工具运行与错误)。过期待审批项显式标出 `run_expired`，可由本地 `reject` 清理，批准与执行继续拒绝。
