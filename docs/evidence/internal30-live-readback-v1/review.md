# 离线证据整理与辅助回读：文件清单及审阅报告

状态：本次授权范围的离线实现、相关测试、原材料贯通及完整变更审阅完成；未commit、push、创建PR或调用Provider。当前没有发现未解决的本批实现blocker。发布需另行授权，不能将本报告当已发布状态。

## 归属与有限清单

隔离分支：`codex/internal30-evidence-readback-v1`，基于`551b9e252670be871ff75b0638b033b07d3c6f08`。仅新增以下6个文件：

| 文件 | 用途 |
|---|---|
| `scripts/internal30_readonly_evidence.py` | 复用immutable有界读取器；统计后目录/字节/身份检查；脱敏投影与默认有限验证；安全CLI |
| `tests/test_internal30_readonly_evidence.py` | WAL副作用复现、零副作用读取、故障注入、篡改、隐私及输出边界测试 |
| `docs/evidence/internal30-live-readback-v1/public_observation.json` | 30响应最小状态/usage行、累计用量/成本/事件和结论边界 |
| `docs/evidence/internal30-live-readback-v1/artifact_commitments.json` | 原payload/事故/缺陷脚本/副本及生成器的摘要与三段来源说明 |
| `docs/evidence/internal30-live-readback-v1/README.md` | 不省略事故的证据解读、复现路径及默认验证覆盖边界 |
| `docs/evidence/internal30-live-readback-v1/review.md` | 本清单、审阅、验证分层及尚未完成的门禁 |

原执行工作树、14文件现场、12文件副本、旧缺陷脚本、批准材料和原事故报告不修改。文件Key后继及AB兼容分支继续暂停；没有读取/初始化真实claim store。旧缺陷脚本没有被新的安全辅助代码覆盖。

## 完整diff审阅

| 范围 | 核验结果 |
|---|---|
| SQLite读取 | 新脚本无直接connect；复用既有immutable/query-only、预期原字节绑定、有限行数读取；未改变生产reader |
| 不变性 | 新的统计后检查覆盖精确文件集合、payload字节与dev/inode/size/mtime/nlink；附加文件或同字节mtime改动均失败；不删除故障现场 |
| 输出位置 | 审阅发现输出可以落在输入目录内部，已在本批修正为发送前/读取前路径检查：全新目录、在两个归档外、不跟随链接父目录；新增回归证明拒绝 |
| 原现场与副本 | 原14文件严格拒绝；副本由完整未修改verifier验真后才导出；前后比较两边输入及全部本地来源文件，不通过隐藏sidecar来取得绿色 |
| 公开allowlist | 只构造明确字段；不spread复制private对象；不输出授权ID/批准摘要/claim内容/原任务或模型正文/本地路径。保留原payload摘要不等于发布内容 |
| 隐私与错误 | 新JSON通过既有敏感字符串扫描；假Key/Authorization/绝对路径/traceback/email测试均拒绝；CLI失败只输出固定码，不回显异常/参数 |
| 验证能力 | 默认仅公开投影/指定字段/摘要自洽校验，不能认证完整私有归档；README及机器字段均明示。完整模式才读取用户提供的真实保留材料并调用生产verifier |
| 固定标准 | 无src/evals/原契约/旧evidence/STATUS修改；没有scope提级、线上重跑、降低断言、扩大mock伪造准入或新模型质量声明 |

本次审阅是同一内部AI助手完成，不冒称外部人员审阅。没有新增无关功能或对原共享运行栈重构。

## 验证记录（不能混称全量）

1. 第一版辅助脚本及AuditLedger相关测试：**18 tests / 0 failures / 0 errors / 0 skips / actual exit 0**，0.595秒。该结果先于输出目录和身份/mtime守卫的最后改动，保留为前检查点。
2. 最终新增/受影响测试：**20 tests / 0 failures / 0 errors / 0 skips / actual exit 0**，0.665秒。14项为新增辅助工具测试、6项为现有AuditLedger测试；含subtest但不将subtest再累加成独立tests。
3. 故障注入：先调用实际有界SQLite读取器，再在临时目录写一个空wal或修改mtime；分别命中集合拒绝/身份拒绝，验证reader确实被调用一次，不是打桩成功返回。旧mode=ro副作用也只在临时WAL数据库中复现。
4. 真实保留材料贯通：新工具对原14目录得到原错误、对副本完整verifier通过，统计后目录仍不变；排他生成两份公开JSON，actual exit 0。不是新Provider请求，也不是根目录unittest全套。
5. 默认公开包检查：`valid_public_projection_only`、`full_archive_reverified=false`，actual exit 0。
6. `git diff --check`及公开JSON敏感扫描通过。真实Key、授权原文、claim文件、原SQLite/模型输出或用户邮箱不进入6文件清单；测试中的敏感canary为明确合成假值。

没有本批全量运行、服务测试或新GitHub CI记录。本批没有改服务或生产src，验证范围是新离线脚本、共享审计读取行为及真实保留材料贯通；不会无理由重跑数小时的全量。未来发布应按新固定提交检查CI，不能用旧head绿色代替。

## 身份

- 原运行source commitment仍为`02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`；本批未进入运行source selector，无需改successor或历史承诺。
- 当前脚本/测试/CI验证输入SHA：`ed4597296c7c0693e7ffb746ef9b9d5a14fd2fe6f78e0bd22e4e048379875f1e`。它已不同于原2051项全量的`1fe00981bcbc68a29af999517f949bbf517a90f90894efcfb82bf31bf54f71e1`，因此不声称旧全量覆盖新工具。
- 新辅助脚本SHA：`c44bde0e37768d2d364b387f8f9be5f6b1267dcaa0cb44b1753ff40c0ed9ebf4`。
- 新测试SHA：`7e6da339ba37dd2f2d95bf660b7b19930e814135e8705ce68116ac3d2ad0b0b4`。
- 公开观测JSON SHA：`d19ae7b4eb0a0649a736c29557b7b7726083a2d5a06acfcbf6c344b862b04b58`。
- 公开来源JSON SHA：`9a36ef19d1f8d6de6f8b812f05b15307da3da49c44d1d85ee188d5898fbbdb56`。

旧缺陷脚本7006 B／SHA `4711c52196af33e59f92b247eeba4da022198d3d04c5a639ab6837cdf3ec0ff3`；事故receipt SHA `0b00c036e1ab9d134873c2f795beb3ad2886d5e7e5ae93b3ec76d059a34fa74a`；副本回验receipt SHA `a9dde14c1ce7671e8031e76ae5059809e2e0ed163b112b6f468cc1cb642830c1`，均继续保留且未改。

## 尚未完成／需要后续授权

- 本清单尚未暂存或提交；发布需用户明确授权commit/push/PR及后续合并范围。
- 公开包不是含完整原件的独立外部证据；若需对外提供可重放私有原件，需另行审查数据发布与授权边界，不能直接提交现有本地授权/claim/数据库。
- 原现场14文件的不变性事故不关闭、不删记录；独立副本通过仍须带事故说明。
- 原STATUS/T7、外部T6-B未完成，Depth-60 20/60不变；没有Provider注册或泛化/质量/SLA主张。
