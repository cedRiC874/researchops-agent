# Production Slice Verification Snapshot

验证日期：2026-08-22。

## 已执行

- 进程内无网络测试：18/18 passed。
- `pip check`：no broken requirements。
- 真实 aggregate backend 内存桥接：Palmer logical registry → worker →
  hash-bound in-memory object → hash-on-read result，最终 `succeeded`，
  `row_level_data_exposed=false`。
- Compose 与 OTel YAML 已由固定 PyYAML 解析；PostgreSQL、MinIO、collector
  和 Python 基础镜像均使用存在性已核验的 tag + digest。
- Docker Desktop 4.87.0、Engine 29.7.2、Compose 5.4.0 与 WSL 2.7.12
  已安装；真实六服务 Compose 已构建并启动。
- 真实 E2E job `1eb317c6-d38c-4e15-873a-21c5e05b8793` 完成
  `queued → claimed → publishing → succeeded`，Palmer profile 为 344×8，
  artifact 2,651 bytes；幂等重放返回同一 Job ID。
- PostgreSQL 两个 migration 生效，4 个 event hash chain 复算有效；MinIO
  metadata 的 SHA-256/bytes 与数据库一致。
- API 首次 POST 与 worker `inspection.execute` 共享 Trace ID
  `908a3fdc48f87dbd85f6371eb497149d`；日志未发现任何本地 secret、Bearer、
  Authorization 或 API-key 形态。
- 一键脚本 `scripts/run-e2e.ps1 -SkipBuild` 已真实自测通过，生成非覆盖运行
  `E2E-20260822T082439Z-4e692c9d`；其 manifest hash、事件链、对象 metadata、
  Trace ID 与 secret-persistence 检查全部通过。
- Windows PowerShell 5 Compose-status 解析加固后再次通过运行
  `E2E-20260822T085428Z-97f21074`；失败会保存稳定 `failure_stage/error_code`
  而不记录异常原文或 secret。
- Compose 5 状态读取最终改为 container ID + 无引号 `docker inspect` 模板，消除
  不同 Windows 用户下的 JSON/Go-template 转义差异；验证运行
  `E2E-20260822T090447Z-8fd401a8` 通过。
- Ubuntu 24.04 GitHub Actions workflow 已实现：固定 Action commit SHA、临时随机
  CI secret、确定性 344×8 registry、18 项测试、真实 Compose E2E、always-upload
  脱敏证据与无 `-v` shutdown。Workflow/fixture 已本地验证，尚未 commit/push，
  因而没有远端 run 结果。
- Eval v2 public candidate verifier 在新增独立服务后仍为 `valid`，commitment
  保持 `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11`。

## Bundle hashes

| Scope | Files | SHA-256 |
| --- | ---: | --- |
| `src/researchops_service/**/*.py` | 16 | `6c0a597269ba085bed4912431f2a1a9540afbedd500a1ba244d995d7c0b7d92d` |
| `tests/**/*.py` | 5 | `2bc186cc7572e22c4dabfd46aaa0e93206846406f7e828197a8d2e7528aa09fc` |
| Git ignore/Actions/Docker/Compose/OTel/migrations/dependency/bootstrap/E2E config | 14 | `293f713bb4b27303fca5d3d9c6121ae84a726b23b184dc4baa1d953a9121cba7` |
| `pyproject.toml` | 1 | `0c9586171e40c9f2be77645f15bab1bbd4713884a672925322074ed20038f073` |
| Full test lock | 1 | `863fd851a94af199dd15e6cfb3024b5d08979aa0dce60d70652775ef9be89a59` |
| Runtime lock | 1 | `fab0eb00e6d3072b8acefe513ba1f06561d75c28fcd0174fd772d6c573b473fd` |
| E2E evidence | 1 | `409157f68264bd574a9f7d80fc7ffbb76a35451095de79000042cdf89f6c0014` |

## 未执行与声明边界

本次只证明单机 development Compose 的真实纵切，不代表 HA、云 IAM/KMS/TLS、
备份恢复、生产 SLA 或负载容量。Local MinIO 未配置 KMS，服务端加密显式关闭；
不得把它描述为生产静态加密。该切片没有 LLM、外部发布或批准后恢复；完整批准
与恢复仍属于 Phase 4。本次 Eval v2 68/93 没有覆盖这个新服务层。机器可复核结果
见 [evidence/e2e-20260822.json](evidence/e2e-20260822.json)。
