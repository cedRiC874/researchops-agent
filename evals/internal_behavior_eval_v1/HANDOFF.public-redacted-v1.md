# 离线交付与主线程交接

开发分支：`codex/internal-behavior-evaluator-v1`。
独立工作树：`{DEVELOPMENT_ROOT}`。
基线commit：`551b9e252670be871ff75b0638b033b07d3c6f08`；tree：`fa18ea712732425a0ed035439a97b93ee08e78ce`。
没有commit、暂存、push、PR、merge、在线调用或全量回归。所有工作保留为本工作树新增文件。

## 文件清单

| 新增文件 | 内容 |
| --- | --- |
| src/researchops_behavior_eval_v1/__init__.py | 独立公开函数导出 |
| src/researchops_behavior_eval_v1/core.py | 严格输入校验、有限解析、五维评分和固定分母汇总 |
| src/researchops_behavior_eval_v1/__main__.py | 与核心隔离的离线IO包装、独占新建报告 |
| tests/test_behavior_eval_v1.py | 61个fixture变体和18个局部不变量测试；可输出测试JSON报告 |
| evals/internal_behavior_eval_v1/CONTRACT.md | 先于实现编写的测量契约与边界 |
| evals/internal_behavior_eval_v1/README.md | 调用示例、严格字段说明、语法范围、reason codes与未来接口需求 |
| evals/internal_behavior_eval_v1/scorer_cases.json | 12组合成正反例、手工预期状态及理由 |
| evals/internal_behavior_eval_v1/example-plan.json | 六行显式合成输入，无测试预期标签 |
| evals/internal_behavior_eval_v1/example-report.json | 离线CLI实际生成的逐维与完整分母报告 |
| evals/internal_behavior_eval_v1/validation-initial.json | 首轮实际结果：75 tests，0 failures/errors/skips，退出0 |
| evals/internal_behavior_eval_v1/validation-final.json | 增加审阅边界测试后的结果：79 tests，0 failures/errors/skips，退出0 |
| evals/internal_behavior_eval_v1/validation-delivery.json | 最终交付代码结果：79 tests，0 failures/errors/skips，退出0；以此为准 |
| evals/internal_behavior_eval_v1/HANDOFF.md | 本交接记录 |

## 实际验证

使用已存在的主仓库 `.venv/Scripts/python.exe`（Python 3.12.14）只读执行标准库测试；没有安装依赖或改动虚拟环境。
所有Python命令使用 `-B`，最终验证另指定 `-X utf8`。没有导入或执行旧评分器、旧60题或Provider。

最终命令（在本独立工作树）：

```powershell
& '{LOCAL_USER}/Documents/ChatGPT/项目/researchops-agent/.venv/Scripts/python.exe' `
  -B -X utf8 tests/test_behavior_eval_v1.py `
  --report evals/internal_behavior_eval_v1/validation-delivery.json
```

实测 **79 tests / 0 failures / 0 errors / 0 skips；实际进程退出码0**。
报告文件使用独占创建，复跑需选择新的报告文件名。
5种故意破坏的评分实现均被手写fixture预期检出。额外检查覆盖纯函数无IO、不修改输入、确定性、
外部十进制上下文变化、轨迹部分缺失、null证据内容、严格类型/重复JSON键、CLI拒绝覆盖与固定分母。

真实CLI命令使用 `python -B -X utf8 -m researchops_behavior_eval_v1 --input .../example-plan.json --output .../example-report.json`，
实际退出码0。示例的人工计数预期另由测试约束：planned=6、observed=4、pass=2、fail=2、unknown=2、not_executed=1、observation_missing=1。
最终代码对该示例的复核与报告相符。这是评分器工程验证，不是Agent成绩。

## 完整新增diff审阅结论

改动仅为以上新命名空间中的新增文件，没有既有文件修改，也没有暂存内容。
逐项审阅了校验、解析、评分、汇总、CLI、测试预期和文档/机器报告；离线检查覆盖所有新增文件的diff空白问题。
没有发现阻塞本轮离线工程交付的问题。审阅中修复了：

- 部分轨迹可能缺失前序事件，不能按完整轨迹索引误判乱序；改为保守子序列判定并增加局部测试。
- 证据内容未知须与已知空内容区分；显式null为unknown，空列表为已知不支持。
- 数值输入的ASCII契约使用显式ASCII匹配，避免Unicode数字被输入校验默默接受。
- 检查定位绑定具体输入路径，证据失败补充目录/事件定位，报告保留run_id。

未解决的离线blocker：**无**。已知限制并非通用语义验收能力：
自由文本只支持列明句式，其他内容unknown/manual_review_required；不实现人工复核决议写回。
行为只覆盖只读执行、两种有限停止理由和审批前暂停，不处理批准后副作用。
观测来源、工具产物内容真实性和观测范围完整性依赖未来可信采集端，本评分器不认证模型自报或审计链。

## 主线程集成责任

1. 本次只读查看的 `phase6_source_bundle.py` 收集逻辑限定在旧 `src/researchops` 或指定遥测包根目录；
   不能假定旧源码承诺已覆盖新 `src/researchops_behavior_eval_v1`。没有运行旧承诺验证，也没有计算或手填successor哈希。
   主线程需决定新包如何进入统一源码承诺，处理任何预期锚点变化，不弱化旧冻结测试。
2. 现有setuptools的src包发现会发现本新包；没有修改pyproject或增加安装命令入口。
   主线程负责最终集成、打包范围复核、全量回归和发布。
3. 如未来接入真实内部评测，采集端须提供原始文本、明确run/call/产物绑定、实际工具内容投影、
   完整计划清单、完成状态、审批中断及任务范围内副作用/交付物可用性观测。缺失材料必须显式未知，不能从金标补齐。
4. 任何正式计分、runner转换器、Provider调用和人工复核集成都需另行授权。

Depth-60原20/60、解释审计、外部合同和T7保持原样；未重算54/60或其他替代成绩。
Internal 30仅为遥测与运行控制验证。未来已知题必须标记 `internal/developer-known`，不声称外部未见或独立验收。
没有读取Key、真实claim store、原始模型输出或本地授权材料；没有操作主线程或其他运行进程。
