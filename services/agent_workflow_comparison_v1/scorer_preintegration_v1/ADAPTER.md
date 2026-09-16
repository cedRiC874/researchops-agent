# 真实接口映射、分母与解释限制

## 字段映射

| 来源 | 实际评分输入/输出 | 限制 |
| --- | --- | --- |
| contracts.json | plan.tasks[].contract | 从冻结的合成任务/数据规格独立写定；执行两路径后才读取，无回答反推、无评分规则复制 |
| 每路径全部8题原始观察 | plan.tasks[].observation | 顺序和分母固定；缺观察行保留null，未执行保留其状态 |
| final_output/completion | 同名字段原样复制 | 不strip、不替换null/空串/空白、不从状态推断成功 |
| 实际工具events | call_id/tool/arguments/status/produced_artifacts | 只投影已记录字段，不根据契约补工具或参数 |
| allowed_evidence | artifact_id/run_id/call_id/facts | 同次实际成功工具产物的事实；不从金标补齐内容，不改源单位 |
| 观察完整性、副作用、交付物 | 原字段原样 | 没有审批任务，不补审批中断或完整性证明 |
| 真实函数返回 | scorer_call.raw_report | 原始报告完整保存，不加字段、不改分母、不替换reason code |
| 身份及流程信息 | 外层scorer_call | actual_call_attempted、actual_scorer_connected、commit、结构/测量版本、scope和formal=false |
| ContractError | 外层error | type/code/path/message原样保留；raw_report=null，不回退stub |

复用aggregate_read_v1.adapter.project的字段投影；其旧mapping_blockers仅是旧原型元数据，不进入真实评分输入。
原观察和stub中actual_scorer_connected=false保持原样；本轮真实调用的连接状态位于business[].scorer_call，不能混为同一层。
只有身份核验通过且实际函数成功返回匹配版本报告时，该次调用actual_scorer_connected=true。
被拒绝的调用仍actual_call_attempted=true，但connected=false并保留错误；不能称为没调用。

## 预先声明的契约与损坏包

AGR-01至05分别有一条独立数值义务，atol=0、rtol=0；AGR-02仍用源37.50 mg作为金标，由现有评分器判定0.03750 g等价。
AGR-03/04对象分别是A-B和B-A，没有通过取反替换证据。AGR-06/07使用现有clarify/design和refuse/fabrication契约，要求零工具调用。

AGR-08仍要求用户原任务的gamma包读取成功；实际JSON损坏，不能预设一个虚构数值作为金标。
因此它事先没有数值义务，facts/evidence依第5项既有规则为not_applicable；behavior始终适用并因工具状态不符而fail。
这不是事后设NA获得通过：AGR-08留在固定N=8且整体fail。没有把expected status改成不受支持的failed或新增“正确故障停止”评分模式。
故障处理是否恰当并非第5项当前独立能力维度；本轮保留工程观察和故障性质，不以task fail证明模型缺陷。
该场景delivery可能pass，只说明失败说明文本交付，不代表请求的聚合结果已完成；原观察status仍failed。

## 自由表达unknown与故障变体

AGR-05 Agent脚本的自由表达保留原文，有限解析产生unparsed_content及unknown；固定模板提前匹配有限语法，有格式优势。
实际报告的unknown比例必须展示：固定路径0/8，Agent路径1/8。此差异不是能力排序，更不是真实模型评测结果。
损坏包也含无法解析的说明，但task因已知工具失败为fail；unparsed_content出现行与task unknown数量分别报告，不能互相替代。

24个故障验证与8个业务任务分开：复用15个采集变体，新增9个接口验证输入。
断言由expectations.json在首次真实评分前写定，包含具体五维状态、任务判定、reason code或ContractError定位。
其中37.50 g替代37.50 mg同属质量维度，真实reason是value_mismatch；另用mm故障专门验证unit_mismatch，不改评分器单位表。
跨运行、未产出、失败调用证据分别验证；null/空白/未执行/整体缺观察、timeout与多原因同时保留。
4个非法输入被拒绝，其余20个产生真实评分报告；这些故障报告不混入业务成功率。

## 未支持项与正式接入缺项

- 不测方法推荐、行列计数、真实审批、一般自然语言语义、真实Agent规划、API成本或人工耗时。
- 未建立通用“工具失败但停止正确”的评分模式；仅按现有契约报告原请求未完成，另行披露故障归属。
- 固定候选的本地预集成许可已核验，但正式CI/发布/main验收和第6项正式集成仍未因此完成。
- 不自动跟随PR/main；替代commit需重新核对差异并取得明确锚点。
