# 最终文本观察语义 v1.1：离线交付

工作树：`{DEVELOPMENT_ROOT}`。
分支仍为 `codex/internal-behavior-evaluator-v1`；基线commit仍为 `551b9e252670be871ff75b0638b033b07d3c6f08`，
tree仍为 `fa18ea712732425a0ed035439a97b93ee08e78ce`。没有提交或修改其他工作树。

## 已交付的明确契约修订

详见REVISION.md中的修订前后对照。单任务和汇总报告新增 `measurement_revision=internal-behavior-eval-v1.1`；
输入结构版本仍为v1.0，严格字段校验不放宽。未知文本不被转换成空字符串。

- null：文本未观察，delivery unknown / final_output_unobserved；必要格式为unknown / expression_unobserved。
- ""：确认零字节空字符串，delivery fail / empty_output。
- 非空且str.isspace()为真：仅空白文本，delivery fail / whitespace_output；不声称零字节。
- 非空文本：继续原有各维度与交付条件，不自动使任务通过。
- JSON包装内空/仅空白answer：仍失败，分别记empty_answer / whitespace_answer，不混淆外层文本。
- 已知超时、截断、未执行、缺产物、违规副作用或审批失败仍保留，按fail优先汇总。null不能掩盖这些失败。

已检查CLI：其实现保持字节不变，本来就直接传递解析后的JSON；新增真实IO测试验证三种文本状态不被强制转换，输入文件不变。
调用说明明确禁止采集端将未采集文本变成""、strip空白、用金标/猜测补齐，或无来源证据回填历史null。
原null语义blocker由此次用户确认和实现解决，没有自动迁移或重新计分历史结果。

## 验证结果（实际退出码）

| 新记录 | tests | failures | errors | skips | exit |
| --- | ---: | ---: | ---: | ---: | ---: |
| validation-before-implementation.json | 26 | 7 | 17 | 0 | 1 |
| validation-regression.json | 26 | 0 | 0 | 0 | 0 |
| validation-related.json | 5 | 0 | 0 | 0 | 0 |
| validation-full.json | 129 | 0 | 0 | 0 | 0 |

第一份是按新授权契约预先写定的测试在旧实现上的结果；17个error来自旧报告尚无measurement_revision字段，
不是对旧契约缺陷数量的统计。完整记录保留，没有改写。新增26项预期没有根据新评分器输出反向生成。
原测试仅JSON空answer一项按明确修订改变reason code，并新增“禁止empty_output、delivery/task仍fail”断言；
未降低其他断言，原61个fixture及24项P1回归保持字节不变。

完整套件在实现稳定后运行一次，范围仅本评分器的129项（原79 + P1的24 + 本次26），未运行根目录全量。
新增/直接相关验证先行完成。使用已有Python 3.12.14，以 `-B -X utf8` 运行，无安装或环境修改。

```powershell
& '{LOCAL_USER}/Documents/ChatGPT/项目/researchops-agent/.venv/Scripts/python.exe' `
  -B -X utf8 evals/internal_behavior_eval_v1/text_observation_v1_1/verify.py `
  --scope full --report evals/internal_behavior_eval_v1/text_observation_v1_1/validation-full.json
```

全部新验证报告记录各自运行前后SHA-256且一致。最终完整报告绑定12个文件：
三个包源码（含core/CLI）、三个本评分器测试、原fixture、CONTRACT、README、REVISION、
新示例输入及本次verify.py。交付审阅再次将报告摘要与最终字节比对。报告自身不纳入哈希；输出只允许新建。
原75/79/103项验证、P1失败记录、历史解释与报告均保留字节原样。

新example-plan.json/example-report.json通过实际CLI生成（退出0），10行均为独立合成输入，
planned=10、observed=9、pass=1、fail=7、unknown=2、not_executed=1。其用途是展示报告语义，不是Agent成绩。

## 最终文件清单与集成影响

相对上批23个文件，本批修改4个已有文件：

1. `src/researchops_behavior_eval_v1/core.py`
2. `evals/internal_behavior_eval_v1/CONTRACT.md`
3. `evals/internal_behavior_eval_v1/README.md`
4. `tests/test_behavior_eval_v1.py`（仅上述获授权reason code及加强断言）

新增13个文件，总交付文件数现为36：

1. `tests/test_behavior_eval_v1_text_observation.py`
2. `evals/internal_behavior_eval_v1/text_observation_v1_1/verify.py`
3. `evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md`
4. `evals/internal_behavior_eval_v1/text_observation_v1_1/pre-revision-snapshot.zip`
5. `evals/internal_behavior_eval_v1/text_observation_v1_1/pre-revision-files.json`
6. `evals/internal_behavior_eval_v1/text_observation_v1_1/validation-before-implementation.json`
7. `evals/internal_behavior_eval_v1/text_observation_v1_1/validation-regression.json`
8. `evals/internal_behavior_eval_v1/text_observation_v1_1/validation-related.json`
9. `evals/internal_behavior_eval_v1/text_observation_v1_1/validation-full.json`
10. `evals/internal_behavior_eval_v1/text_observation_v1_1/example-plan.json`
11. `evals/internal_behavior_eval_v1/text_observation_v1_1/example-report.json`
12. `evals/internal_behavior_eval_v1/text_observation_v1_1/HANDOFF.md`
13. `evals/internal_behavior_eval_v1/text_observation_v1_1/review.json`

新增8个JSON可能进入主线程的统一文件选择，文件数与摘要均可能改变。输出报告新增measurement_revision，
消费端应明确选择本修订；不把历史未标修订的报告与新报告视为同一契约结果。
本批没有更新source manifest、successor或CI锚点，没有调用/改写现有manifest生成器。
主线程负责基于稳定后的实际文件集合决定源码承诺接续、集成与全量回归。

## 完整diff审阅与blocker

按修订前23文件快照逐字节比较审阅本批diff；原文件仅上述4项变化，其他19项完整保留。
审阅覆盖未观察文本分支、已知失败优先、JSON内外区别、报告版本、全部相关调用、测试预期、
CLI真实IO、新文档/输入/报告和哈希包装器；结果保存在review.json。
P1容差比较函数及其两个调用点没有修改，完整24项回归保持通过。

本批剩余blocker：**无**。自然语言有限规则、可信采集来源、后续运行入口与人工复核接口仍是既有边界，
不因本修订获得通用语义验收或在线运行授权。当前成果等待主线程集成。
未commit/push/PR/merge，未调用Provider、读取Key/store/原始模型输出或操作其他任务进程。
旧评分器、20/60、Internal 30历史解释、STATUS/T7与外部冻结标准保持原样。
