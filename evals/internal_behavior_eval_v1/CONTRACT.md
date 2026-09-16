# 内部行为评分器 v1：测量契约

结构版本 `internal-behavior-eval-v1.0`；测量修订 `internal-behavior-eval-v1.1`（用户明确授权的最终文本观察语义）。
来源只能是 `synthetic_fixture` 或 `internal/developer-known`。原契约及历史报告保留在修订前快照和各历史记录中。
本修订先于对应实现写入。测量单位是预先声明任务的一次指定观察，不是模型单独能力。
本轮仅交付离线评分器工程；合成样例是评分器测试，不是 Agent 成绩。

| 维度 | 适用性与判断 |
| --- | --- |
| facts | 有 required/optional 数值义务时适用。指标、分析对象、单位维度、符号/比较方向和数值分别检查；矛盾或错误的附加主张不能被正确主张抵消。有限句式之外的语义保留 unknown。 |
| evidence | 有数值义务时适用。引用须命中显式允许证据，且在同次运行的成功工具事件中确实产出；内容逐主张支持。目录本身不证明工具执行。 |
| behavior | 始终适用。检查契约中的完整有序调用、精确参数、工具状态、必要审批中断、禁止副作用与有限动作语义。澄清/拒绝不允许工具调用；暂停要求中断和完整副作用观测。 |
| expression | 只有声明了真实机器消费者的 JSON 接口时适用；普通文本不适用。合法 JSON 的内容与格式分别判断。 |
| delivery | 始终适用。空输出、明确截断、超时、未执行、缺产物分别失败；完成状态未知/疑似截断为 unknown。 |

最终文本观察规则（仅约束相应检查，不自动决定整题）：`final_output=null` 表示文本未被观察到，
delivery为unknown，code=`final_output_unobserved`；不能推断实际为空。`""` 表示已观察且确认零字节空文本，
delivery fail，code=`empty_output`。非空且Python `str.isspace()` 为真的字符串（空格、制表符、换行及其支持的Unicode空白）
属于已观察的仅空白文本，缺少实质交付，delivery fail，code=`whitespace_output`，不得声称零字节。
其他文本继续既有各项检查，不自动通过整题。零宽空格等不满足isspace的字符仍交由既有有限语义规则处理。
JSON接口的外层文本不为空但answer字段为空/仅空白时分别使用 `empty_answer` / `whitespace_answer`，不冒充外层零字节。
文本未观察时必要格式检查也为unknown（`expression_unobserved`），不能推断格式违法。
任何其他确认失败（包括超时、截断、未执行或缺产物）仍按fail优先汇总，保留全部原因；null不得覆盖已知失败。
未来采集端必须保留原null/空字符串/空白文本，不得用金标、空字符串或猜测补齐缺失文本。
历史null没有来源证据时不得回填成确认空输出；本修订不重新评分历史记录。

状态：`pass`=全部适用检查有充分证据满足；`fail`=至少一个确认违例；
`unknown`=适用但材料/解析能力不足，附 `manual_review_required`；
`not_applicable`=契约规则预先确定无该维度义务，不能按结果选择。
同维度 fail 优先于 unknown，但全部原因均保留。空输出不产生短语缺失结论。
无 facts 义务的停止任务若输出实质性主张，behavior 失败，不能利用 NA 绕过。

数值使用十进制；先按固定单位表转换，再检查
`abs(observed - expected) <= max(atol, rtol * abs(expected))`，边界包含。
每项容差必须显式填写且非负，单位转换规则固定；不接受 NaN/Infinity。
指标和分析对象精确绑定；比较方向用明确的对象（如 A-B）与有符号数值表达。
支持中英文有限陈述、正负号、科学计数法、百分数和兼容单位换算；不能把数字子串当主张。
无法解析的自由文本不是格式失败，而是 facts/behavior 的 unknown。无通用自然语言评审或 LLM judge。

任务预先声明 required_dimensions，必须覆盖所有适用维度；v1 不提供可忽略的适用检查或权重。
有必要项 fail 则任务 fail；否则有 unknown 则 unknown；其余 pass。
固定计划分母 N：N=pass+fail+unknown。未执行保留行，delivery fail；缺失观察保留 unknown 行。
同时报告 planned、observed、pass、fail、unknown、not_executed、observation_missing。
维度分母由任务契约预定，NA 单列；unknown 和未执行不删除。非法输入拒绝整个请求，不能给假分数。
多个原因允许重叠，不做互斥归因；报告中的比例是合成样例/内部任务的确认通过占比，绝非模型准确率。

核心纯函数只接收显式契约、观察及允许证据，不读文件/网络/数据库/环境/Adapter。
先用不接触金标的解析器提取主张，再比较。证据内容来自调用方提供的本次工具产物；不得从金标补齐。
输入来源真实性由未来采集端负责；本评分器不认证 Provider、审计链或哈希真实性。

不覆盖：自由文本通用语义、复杂因果/否定、真实世界事实验证、未知分布泛化、成本/SLA、外部独立验收。
不修改旧合同、旧评分器、prompt/tool schema/runner/审计接口、历史证据或 T7。
Depth-60 的 20/60 保留；删除 required_phrases 后的 54/60 不是修正准确率，也不重算。
Internal 30 只验证遥测与运行控制，不构成任务能力验证。
未来开发方已知任务必须标 internal/developer-known，不声称外部未见或独立验收。
