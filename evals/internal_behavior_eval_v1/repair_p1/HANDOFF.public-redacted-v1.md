# P1容差修复交接与null待决项

工作树仍为 `researchops-agent-internal-behavior-evaluator-v1`，分支 `codex/internal-behavior-evaluator-v1`。
Git基线未变：commit `551b9e252670be871ff75b0638b033b07d3c6f08`，tree `fa18ea712732425a0ed035439a97b93ee08e78ce`。
本记录接续上一轮交付；原HANDOFF及75/79项报告保留，不覆盖或改称本批成绩。

## 已解决：跨单位证据绝对容差误用（P1）

通过实际 `score_case` 入口，在未修复核心上复现：金标/回答12.40mg，atol=0.01mg，证据0.02g，
原实现把绝对容差变成0.01g=10mg，错误放行相差7.60mg的证据。反向换算也错误拒绝合法边界。
先写人工预期回归并保存失败报告，再修改核心，未根据修复后输出生成预期。

`_compare`现在接收带单位的完整容差义务；回答、目标值和绝对容差分别转换至基准单位。
两个调用点均修复：事实比较和证据比较都显式传入同一金标义务。未列入契约的附加主张仍精确比较。
量纲不兼容时拒绝，原符号检查、包含边界、十进制精度与 `max(atol, rtol*abs(expected))` 公式均保留。
相对项的原参考操作数未改变：F为金标，E为工具证据值。区别两种参考值的测试在修复前已通过，修复后仍通过。

24项针对性回归覆盖：报告中的P1、同单位、跨单位等价、上下边界及刚超界、反向换算、零容差、
绝对/相对组合、相对参考值、量纲不兼容、已有回答换单位，以及未列入契约的附加主张。
各项均走实际评分入口，检查五维状态、总判定和原因；没有降低断言或扩大mock。

## 实际验证记录与字节绑定

| 记录 | tests | failures | errors | skips | 实际进程退出码 |
| --- | ---: | ---: | ---: | ---: | ---: |
| validation-red.json（修复前） | 24 | 5 | 0 | 0 | 1 |
| validation-green.json（修复后针对性） | 24 | 0 | 0 | 0 | 0 |
| validation-full.json（稳定后一次完整局部套件） | 103 | 0 | 0 | 0 | 0 |

完整局部套件是原79项加24项新回归，没有运行根目录全量测试。
使用已有Python 3.12.14，以 `-B -X utf8` 执行，不修改共享虚拟环境。

```powershell
& '{LOCAL_USER}/Documents/ChatGPT/项目/researchops-agent/.venv/Scripts/python.exe' `
  -B -X utf8 evals/internal_behavior_eval_v1/repair_p1/verify.py `
  --scope full --report evals/internal_behavior_eval_v1/repair_p1/validation-full.json
```

每份新验证报告包含运行前后SHA-256：core、CLI、包导出、原fixture、两个测试文件及verify.py，共7个文件。
所有运行的before/after一致。红到绿只有core字节变化，测试、fixture和验证包装器均未修改。
最终复核还将完整套件报告中的摘要与交付时文件字节比较。所有报告均独占新建，报告自身不纳入哈希。
预修复13文件另保存ZIP及自动计算的摘要，用于恢复与本批diff核对；这不是source successor或统一源码承诺。

## 仍待决：已观察运行中final_output=null的含义

原CONTRACT规定空输出失败，但没有定义 `final_output=null`。README只列“字符串或null”，
并明确未执行观察必须使用null；没有定义已观察运行的null是确认无输出还是文本未采集。
原fixture中的final_output=null只覆盖未执行；另有整个observation=null表示整份观察缺失，不能替代局部文本缺失。

现有字段不能无损区分这两种文本情形：completion描述完成状态，events_complete和side_effects_complete
分别描述事件与副作用覆盖，都不是文本采集状态。缺少字段会被严格校验拒绝，也不是一个合法替代编码。

当前实现用 `final_output or ""` 把已观察null按空输出处理。本批未修改此行为，也不将该实现提升为验收标准。
`null-review.json`保存5种实际输入/判定的特征记录，仅用于说明现状；修复后逐条复核保持一致。
**待主线程决定**：已观察null的规范含义，以及如何显式表示“已确认空文本”和“文本未取得”。
不得要求未来采集端用金标、空字符串、猜测或丢弃其他有效观察来补齐文本。

此待决项不阻止确认P1修复，但仍是接入可能缺失文本的实际采集数据前的语义blocker。

## 本批文件变化及统一源码选择影响

相对上轮13文件交付，本批修改2个已有文件：

- `src/researchops_behavior_eval_v1/core.py`：容差单位及两个调用点。
- `evals/internal_behavior_eval_v1/README.md`：明确已有比较操作数、修复验证入口和null待决说明。

新增10个文件（总交付文件数现为23，不沿用13）：

- `tests/test_behavior_eval_v1_tolerance.py`
- `evals/internal_behavior_eval_v1/repair_p1/verify.py`
- `evals/internal_behavior_eval_v1/repair_p1/pre-repair-snapshot.zip`
- `evals/internal_behavior_eval_v1/repair_p1/pre-repair-files.json`
- `evals/internal_behavior_eval_v1/repair_p1/validation-red.json`
- `evals/internal_behavior_eval_v1/repair_p1/validation-green.json`
- `evals/internal_behavior_eval_v1/repair_p1/validation-full.json`
- `evals/internal_behavior_eval_v1/repair_p1/null-review.json`
- `evals/internal_behavior_eval_v1/repair_p1/review.json`
- `evals/internal_behavior_eval_v1/repair_p1/HANDOFF.md`

其中6个新增JSON包括验证、核查和恢复记录；统一源码选择器可能将它们纳入选择范围，改变文件数与摘要。
本批没有执行/修改现有manifest生成器、source manifest、successor、CI锚点，也没有手填源码承诺哈希。
主线程须在本批稳定后重新审阅实际文件选择和承诺接续，不应直接沿用原选择清单或旧文件计数。

## 完整修复diff审阅

以预修复ZIP中字节为比较基准审阅本批diff，避免把未提交文件误当成“无diff”。
原13文件中仅core和README变化，其余11个（包括旧CONTRACT、fixture、CLI、测试及所有旧报告）保持逐字节一致。
审阅覆盖比较函数的全部调用点、新回归的人工预期、验证包装器、所有新JSON和记录；结果见review.json。
已解决blocker：P1跨单位绝对容差；仍待决blocker：observed final_output=null语义；无其他已发现修复blocker。

未commit/push/PR/merge，未调用Provider、修改其他工作树、读取Key/store/原始模型输出/授权材料或操作其他进程。
旧评分器、Depth-60 20/60、Internal 30历史解释、STATUS/T7及外部冻结标准均保持原样。
