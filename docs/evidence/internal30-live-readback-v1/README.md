# Internal 30：运行完成、回读事故与独立副本校验

这是同一次、预先固定的内部 synthetic 遥测验证，不是模型能力评测或外部未见题。三段证据必须一起阅读，不能只保留成功部分。

| 阶段 | 已观察事实 | 不能改写成 |
|---|---|---|
| 原在线运行 | 固定30题一次执行；30请求/30响应，运行时逐题检查30通过，实际进程exit0 | 外部独立验收或模型质量30/30 |
| 原现场的辅助回读 | verifier 返回后，额外统计代码以非 immutable 的只读SQLite连接新增shm/空wal；目录14文件，严格12文件回读失败 | 原现场从未被修改、整个最初回读已成功 |
| 独立校验副本 | 按原bundle承诺逐字节复制12个原payload，未修改的完整verifier通过，辅助统计后副本仍不变 | 删除原事故、重新执行或修补模型结果 |

## 运行观测

- Provider：DeepSeek；请求名：`deepseek-v4-flash`；`POST /responses`；真实transport为`native_http_transport`。
- 固定30题均为开发方已知synthetic；每题一次，tools/handoffs=0，没有追加preflight、探针、重试或补跑。
- 输入4,177、输出2,487 tokens，其中reasoning 2,413是输出子集。必需usage完整；可选`cache_write_tokens`未提供，保持null，不填0。
- 本地按输入2/输出8 CNY每百万tokens、不假设缓存优惠计算：`0.028250 CNY`。这是已批准费率下的观测核算，不是Provider账单；实际账单unknown/null。
- 30条均为HTTP200、native/normalized `completed`、source `native_status`；未识别/缺失/fallback没有被当作成功。
- 原审计链122事件：1个开始、30个请求开始、30个发送意图、30个同响应遥测、30个case关闭及1个运行终态。没有model body表行或工具执行。
- phase终态98.016秒；publication的封存观测0.468秒，其范围仅为`before_publication_write_not_process_exit`，不声称覆盖整个进程退出。

[public_observation.json](public_observation.json) 保留30条响应的最小状态/usage投影及累计结果，不包含输入/回答正文。完整原始元数据和审计归档保留本地，未复制到公开目录。

本轮授权时的官方文档称旧请求别名由DeepSeek-V4.1-Flash承接；该模型映射是文档层披露，不是本组遥测对后端权重/版本的独立证明。这里不把旧A04价格或模型身份当作新运行证明。

## 事故记录不会撤回

原辅助脚本`verify_completed_run_offline.py`的第52行直接调用SQLite `mode=ro`，没有`immutable=1`。在WAL模式下，读取仍可能创建辅助文件。原缺陷脚本逐字节留在本地，不用安全新版本覆盖它。

原14文件现场继续保留两个附加文件：32,768 B的`audit.sqlite3-shm`和0 B的`audit.sqlite3-wal`。原12 payload的字节和哈希未变，但目录集合已经不满足精确12文件规则。旧回读进程exit1和`external_closure_directory_file_set_invalid`依然有效。

[artifact_commitments.json](artifact_commitments.json) 记录原bundle、12payload、旧缺陷脚本、退出观察、事故记录和副本回验receipt的字节数/SHA。只发布摘要，不复制本地授权/claim内容或路径。原退出码0与副本verifier通过，不消除原现场目录不变性事故。

## 辅助读取加固

新脚本`internal30_readonly_evidence.py`不再直接打开SQLite。它复用既有有界、`mode=ro&immutable=1`、query-only、数据库字节绑定读取器；在额外统计之后重新核对目录集合、payload字节和文件身份/mtime。发现附加文件或任何变化即拒绝，保留文件，不自动清理。

导出时首先证明原14文件仍被未修改的verifier拒绝，然后完整验证独立12文件副本；之后才按字段白名单生成公开投影。导出目录须全新且在两个输入归档之外，已有目录或链接父目录拒绝。没有Key、环境配置、真实store初始化或Provider入口参数。

## 验证层级与复现边界

公开包可运行：

```text
PYTHONPATH=src python scripts/internal30_readonly_evidence.py --verify docs/evidence/internal30-live-readback-v1
```

**默认`--verify`不是完整归档认证。** 它检查canonical JSON、观测文件摘要和当前生成脚本摘要，重算30条投影的索引/状态/usage/金额/事件数及指定边界字段，并检查选定事故/副本标记。其他本地来源摘要是已发布的来源声明，默认路径不会取得或重算其原件，也不能证明HTTP发送事实、真实批准身份或全部原始链。返回中明确`full_archive_reverified=false`。

完整重算需要保留在本地的7项输入：原14文件目录、12文件副本、原bundle、原进程退出观察、事故receipt、副本receipt和旧缺陷脚本。调用脚本的对应`--original/--copy/--bundle/--exit-observation/--incident/--copy-receipt/--faulty-helper`参数，并指定一个全新的`--output`目录。完整路径会重跑**离线**生产verifier及原现场拒绝检查，再可逐字节比较生成的两份JSON；不发送网络请求。

不使用假的签名/角色，也不将本地JSON摘要当独立外部见证。公开材料无法单独替代私有原件的完整验真，这是主动保留的隐私边界，而不是已完成证明。

## 固定版本和结论边界

- 原执行main：`551b9e252670be871ff75b0638b033b07d3c6f08`。
- 原source commitment：`02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`。
- 原bundle commitment：`63d13254f77bf19e6eec4dc8aae02eeb1ea51e4233b13bf734979f909f25e956`。
- 本批只新增`scripts/`、`tests/`和本证据目录；不改`src/`、任务或冻结契约。当前运行源码承诺仍一致，不生成successor。
- 20项新增/受影响离线单测通过；不是本批全量回归。此前2051项全量记录属于原固定版本，不扩展为本批新增辅助代码的全量通过。详见[审阅报告与文件清单](review.md)。

准确结论：原bundle承诺的payload在明确重建的独立副本上通过未修改的完整Internal verifier，支持本次固定内部用例的遥测观察；原现场回读副作用/目录失败继续保留。不能无条件省略事故只说“原始归档完全成功”。

本次未新增Provider调用，没有Key/授权材料提交，没有恢复已消费授权或暂停分支。Depth-60 20/60不变、旧归因不补写；原STATUS/T7及外部独立审阅/见证/未见任务里程碑不关闭。Internal记录不支持统计质量、未知集泛化、模型排名、Provider注册或生产SLA声明。
