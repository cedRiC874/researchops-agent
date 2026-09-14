# Internal v1 固定版本离线审阅

全量前审阅结论（保留）：已完成本批完整变更审阅，当时未发现未解决的生产代码 blocker。该结论不是外部独立审阅，也不是全量回归或在线验收结论。

当前状态（2026-09-14）：修复后的第二轮不筛选全量已通过，2,051 tests / 0 failures / 0 errors / 15 skips，原进程实际 exit 0，源码与验证输入前后稳定。进入有条件的窄发布阶段，尚无本批 fixed-head CI 或合并后 main 通过结论。下文第一轮失败、局部修复和启动记录均为保留的历史检查点，不由新结果覆盖。

## 固定范围

- 基线 `c945d168ee084f82ab690a2f118912bf28956c27`；独立 `codex/t6-b-internal-v1` 工作树，项目尚未 commit/push。
- 当前源码承诺：`02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`。
- 当前 manifest 文件 SHA-256：`671a625a1092db071dfa01bc43bed29114d3fcef46a1d54a2ac1483eba0ab0d8`；绑定 361 个源码／配置／策略／任务等文件，不是 361 条测试覆盖声明。
- 共享生产修改仅为 `model_providers.py` 的精确 Internal session/transport 接点和 `surface_mapping.py` 的显式 Internal scope。原外部入口、签署合同、旧候选和历史证据不改。
- 新增 Internal 合同／任务、准入、预算适配、实际 SDK 运行、事件链、计时、排他归档及独立 verifier；新增测试与 source-only/CI 迁移。

## 审阅结果

| 范围 | 核对结论 |
|---|---|
| 准入与所有权 | 不使用外部假签名／角色；新 scope 不能进入原 campaign/first-live scope。真正读取 Key 的函数自身也验证 winning owner；没有 Key 文件路径或公开回调/路径绕过参数。 |
| 完整分母 | 固定 30 题、每题一次；传输层禁第二次发送。失败停止，未执行项保留，不补齐分母。 |
| 任务与隐私 | 无敏感 synthetic 全文及稳定 ID/顺序/input digest；精确旧题排除记录，不宣称语义或全球新颖。任务文件以原字节归档。解码后的 JSON 字符串扫描保留真实路径、转义秘密等拒绝；旧扫描器不修改。 |
| 响应归因 | 原生/归一化与 usage 同一响应事件；missing/null 保持区别。未识别、不提供、历史未存或 fallback 不成为成功。 |
| 时间与停止 | 响应/raw-cleanup 计时及引用该记录的完整 SDK request/client-close 事件；发送边界复核 deadline；独立回读计入封存时限。取消/超时不能解释为远端已停止。 |
| 预算 | 固定 512 输出预留，输入/费用仅响应后停止；unknown 不填 0，超额保留实际值；账单 null。当前价格材料仍待真实提供，不用旧价格当现价。 |
| 归档与回读 | 排他写入，不覆盖；原生记录、事件序列、分母、预算、timing 和容器哈希交叉核对。退出码须外部观察；离线 MockTransport 只能得到 engineering acceptance，不能得到在线 Internal acceptance。 |
| 历史接续 | v11 原字节/承诺在原 Git 树仍有效；当前树须拒绝旧 v11。新 current-tree 完整性转移到 Internal manifest，不生成外部 campaign v12、不手填旧哈希。 |
| 边界 | 原 A04 只证明旧版本；旧外部里程碑未完成，20/60 不变，STATUS/T7 不关闭。 |

开发中发现的 JSON 转义扫描、mapper 输入遗漏、完整请求清理计时、Provider 错误码泄漏风险、Key 读取函数自身所有权防护已处理。历史回放测试误用 256-path reader 已改用既有 320-path reader，未放宽旧 reader。失败记录见同目录 implementation-status.md。

## 验证分层

- 新增及受影响批次：95 tests，0 failures/errors/skips，exit 0，496.957 s。包含共享 Provider/adapter/ledger/mapping。
- 最后的 Key-owner 改动：2 项受影响测试通过，0 failures/errors/skips，exit 0，115.715 s。
- CI/current-anchor 迁移：3 项针对性测试通过，0 failures/errors/skips，exit 0，149.391 s。
- `verify_pre_v6_integrity.py`：当前 Internal 与两个历史快照核验 valid，exit 0；这是本地 CI 脚本核验，不是 GitHub checks。
- 相关服务：pilot candidate contract + completion telemetry 两个 pytest 文件，10 项通过，exit 0。未改服务部署、未启动或重启 Docker/MinIO。
- 根目录不筛选全量：2,047 tests / 1 failure / 0 errors / 15 skips，实际进程 exit 1。无 expected failures 或 unexpected successes。源码与验证输入前后稳定，仍属于失败结果，不是完整验收通过。
- 本轮于 2026-09-13T19:05:13.343848Z 结束，总耗时 13,554,125 ms（约 3 小时 46 分钟）；原 session 25703 的实际退出码与 receipt 一致。
- 15 个 skip：5 个因主机符号链接权限不可用；10 个为固定 Python 3.12.13 CI 环境的 counterexample replay 项。它们不计作通过，本次没有放宽或改写这些守卫。

## 尚未完成的门禁

1. 历史诊断回放修复已由后续单独授权的第二轮全量覆盖；没有第三轮全量或新的源码/测试修复授权。
2. Git 发布已有条件授权，本地验收条件现已满足；仍须核验最终完整 diff、窄提交、新 fixed-head 必需 checks、regular merge 及实际 main checks。不能以本地通过替代这些门禁。
3. 最终费用/模型映射材料、正式任务批准及新一次性在线授权；不得从本离线授权继承。

尚未调用 Provider、使用真实 claim 或读取真实 Key。全部集成请求属于临时隔离测试；它们不是新的在线证据。

## 唯一失败的只读定位与待授权最小方案

失败测试：`tests.test_t6c_admission_boundaries.AdmissionBoundaryDiagnosticTests.test_original_diagnostic_replays_its_bound_historical_first_live_bytes`。

`scripts/inspect_t6c_admission_boundaries.py:63-68` 在 `historical_first_live=True` 时只从固定 Git 读取 FIRST_LIVE；SURFACE、REGISTRY、PREDECESSOR 仍来自当前工作树。新增 Internal scope 使 SURFACE 从冻结 receipt 记录的 120,430 B 变为 120,490 B，逐字节报告比较正确检出差异。本次迁移没有为这项历史诊断完成隔离，不能把它排除来获得绿色。

待授权最小方案：先按冻结 receipt 的每项摘要确认对应历史字节，明确增加全输入历史回放或专用历史 fixture；同时保留当前态诊断、原 `--historical-first-live` 边界和冻结 receipt 字节，不用当前哈希替换旧证据。需要调整的诊断脚本/测试不属于既有 35 文件发布清单，不能凭发布授权直接修改。局部验证与是否再做全量应在新授权下决定。

完整失败日志和 receipt 保留于 `output/internal-offline-regression-v1/batch-20260913/`，不提交原始测试日志。`researchops-30` 已按失败结束门暂停并回读核验，等待用户指示；20/60、旧归因及 STATUS/T7 不变。

## 已授权的历史回放修复（2026-09-14）

仅修改 `scripts/inspect_t6c_admission_boundaries.py` 与 `tests/test_t6c_admission_boundaries.py`，并补记本节/批次状态；没有修改生产源码、冻结 receipt、验收标准或 source manifest。

先逐项比对确认原 receipt 是混合输入快照：FIRST_LIVE、REGISTRY、PREDECESSOR 的记录字节匹配 `5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab`（tree `30ecfd86ac00aecd8b67305a7c6ed2af88eeee10`）；SURFACE 的 120,430 B 和 SHA-256 匹配 `c945d168ee084f82ab690a2f118912bf28956c27`（tree `0ee6bae3a2c56ef1d8c6c9193d9030f1a8573553`）。不能把四项笼统归于一个提交；这些固定 Git 对象是逐项字节恢复来源，不是原始进程执行版本/时间的证明。

新增 `--historical-inputs`，从上述固定来源恢复全部四项输入，无当前树 fallback。旧 `--historical-first-live` 仍只替换 first-live，其余保持当前态；两个模式互斥。历史源码仅作为字节/AST 输入，不被导入执行；当前只读投影器仍负责计算，冻结报告的逐字节比较继续检测输出语义变化。

相关测试模块：**9 tests / 0 failures / 0 errors / 0 skips，实际 exit 0，81.869 s**。覆盖全部输入历史回放、禁止读取当前输入、缺少 Git 对象不回退、旧模式/默认模式仍观察当前输入变化、CLI 核验、模式互斥，以及既有只读/无运行权限/排他写入和篡改拒绝检查。完整修复 diff 已审阅，`git diff --check` 通过。

保持不变的产物：

- 原诊断 receipt SHA-256：`cf6ffacf80a13dfe95dd8767172125933b51728a24191f0dc69924419a573976`。
- 原失败全量 receipt SHA-256：`8d87d10f09351972c6d2d19cbae02d57e291eb97790485ce6dca1c4e02e82491`。
- Runtime source commitment：`02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`，已只读重算核验。
- Source manifest 文件 SHA-256：`671a625a1092db071dfa01bc43bed29114d3fcef46a1d54a2ac1483eba0ab0d8`。

脚本/测试身份已变化：当前 verification-input SHA-256 为 `51018236d9b6651c8e63bd373164b6190a5dd1e446f4e275aafa05e63045bf19`，不再是旧全量绑定的 `5ed8c38ef4b7c4cef7ae0ffae509d24fb24d072ca06e7973bb23f3f860b42840`。因此不把旧全量或本次 9 项局部通过改称当前全量通过。本轮未更新 successor、未重跑全量、未发布、未恢复监督、未调用 Provider。

## 后续新授权：repair1 全量已启动

上述局部修复后，用户另行授权一次修复后全量及每 30 分钟监督，并重申通过后的窄发布权限。为不覆盖旧结果，离线驱动只增加受限批次参数及批次/驱动摘要记录；旧驱动原字节已保存且摘要验证一致，测试发现与验收逻辑未改。这个驱动改动的完整 diff 已审阅，4 个非法批次输入检查通过；未增加测试筛选或改变冻结标准。

新批次 `batch-20260914-repair1` 于 `2026-09-13T20:01:21.452431Z` 启动，PID `28224`。当前验证输入摘要为 `1fe00981bcbc68a29af999517f949bbf517a90f90894efcfb82bf31bf54f71e1`，驱动摘要为 `34d8846383f29e6a7aaf4a59c8e20e203474171dcc4f9ff395757992c6406f63`；Runtime source commitment 仍为 `02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`。本条是运行中状态，不是通过结论。

监督已更新并回读为 ACTIVE，目标仍为主线程，首次计划检查北京时间 2026-09-14 04:33。当前发布清单明确为原 35 文件加历史回放脚本/测试两项，共 37 文件。只有新全量通过、源码/验证输入一致，才继续后续 Git 与 CI 门禁；没有新的 Provider、真实 claim、冻结判据修改或第三轮全量权限。

## repair1 终态及发布前核验（2026-09-14）

- `batch-20260914-repair1` 于 `2026-09-14T00:02:59.317661Z` 完成，耗时 14,497,844 ms（约 4 小时 2 分钟）。不筛选的根目录 `unittest discover -s tests -t .`：**2,051 tests / 0 failures / 0 errors / 15 skips**，expected failures / unexpected successes 均为 0；原工具 session `65015` 已回收并独立确认实际 exit **0**，与最终 receipt 一致。
- `full_regression_passed=true`、`source_stable=true`；发布前再次只读重算，源码承诺仍为 `02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`，manifest 文件 SHA-256 仍为 `671a625a1092db071dfa01bc43bed29114d3fcef46a1d54a2ac1483eba0ab0d8`，verification-input SHA-256 仍为 `1fe00981bcbc68a29af999517f949bbf517a90f90894efcfb82bf31bf54f71e1`。
- 本轮 receipt SHA-256：`5e67715858dddcac0a9c7d9259acd2f8d56da2a66ab5c91bd9c87c5c4b76d03d`。输出目录仅本地保留，不纳入 37 文件发布清单；第一轮失败 receipt 与原诊断 receipt 摘要再次核对不变。
- 15 项 skip 与已解释的环境边界一致：5 项 Windows 符号链接权限不可用，10 项仅在固定 Python 3.12.13 CI 中回放的 counterexample 测试。它们不是通过项；未增加 skip 或放宽守卫。2,051 相对旧 2,047 增加的 4 项来自已授权历史诊断测试扩展，不是修改全量分母来排除失败。
- 安全 fetch 后实际 `origin/main` 仍为基线 `c945d168ee084f82ab690a2f118912bf28956c27`，无基线差异；本分支尚无远端分支/PR。工作树改动逐项与 37 文件清单一致。受测源码、测试、驱动、CI 与此前完整审阅的稳定版本绑定一致；共享接点及历史回放修复的最终 diff 已复核，无未解决 blocker。只补记两份批次文档的终态，不改受测代码或冻结承诺。
- 下一门为明确文件暂存及 staged-byte 核验、提交/PR 固定 head 审阅与新 CI。本文不把尚未执行的发布或 CI 写成成功；无 Provider、真实 Key/store 操作，Internal 在线验收仍未执行，旧外部里程碑及 STATUS/T7 保持 open。
