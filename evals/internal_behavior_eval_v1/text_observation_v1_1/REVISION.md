# 最终文本观察语义修订：internal-behavior-eval-v1.1

依据：用户明确确认null、空字符串、仅空白文本的区别，并授权本工作树有界离线实现。
这是一次公开记录的测量契约修订，不是对旧标准的隐式修复。结构版本保留v1.0；报告新增measurement_revision=v1.1。
旧75/79/103项验证、P1修复前24项5失败记录、原fixture和历史报告保持字节不变。
修订前23个交付文件已保存为pre-revision-snapshot.zip及自动计算摘要，可恢复并复核本批diff。

| 情形 | 修订前实现及定义情况 | 本次明确规则 |
| --- | --- | --- |
| observed + final_output=null | 原契约未定义；实现通过 `or ""` 作为空输出失败 | 最终文本未观察，delivery unknown / final_output_unobserved，不断言实际为空 |
| final_output="" | empty_output失败 | 保留失败，empty_output仅指确认零字节的外层空字符串 |
| 非空且str.isspace() | 经strip后统一记empty_output | 因无实质交付失败，记whitespace_output；不声称零字节 |
| 其他非空文本 | 继续语义、证据、行为、格式和交付检查 | 保留；不得仅因非空而通过整题 |
| JSON包装的answer为空/仅空白 | 均记empty_output失败，混淆外层与字段 | 仍失败，但分别记empty_answer / whitespace_answer，定位到#/answer |
| 必要格式、最终文本未观察 | required_json_envelope失败 | expression unknown / expression_unobserved，不推断格式违法 |
| JSON文本为 {"answer":null} | 格式失败 | 保留：这是已观察的接口内容，answer必须是字符串 |
| null叠加超时、截断、缺产物或其他确认失败 | fail | 保留所有确认失败与未知原因，fail优先 |
| 确认未执行，按原schema使用null | delivery fail / not_executed | 保留；null不掩盖未执行 |
| observation整体为null | 全部适用维度unknown | 保留整份观察缺失的含义，与局部文本未观察分开 |

空白判据明确为Python str.isspace()，且字符串非空；包括其识别的ASCII及Unicode空白。
Python版本随验证报告记录。零宽空格等非isspace字符继续有限语义规则，不推断为空白或空字符串。
本批不新增语言内容判据、数值容差、权重或计分题；P1容差修复的公式与回归完整保留。

## 测试预期变更声明

原61个fixture变体没有修改。原测试仅一处因本次授权修订变更reason code：
`test_json_empty_answer_is_delivery_failure` 从empty_output改为empty_answer，
新增明确断言旧empty_output不得出现，并保持/加强delivery与task均fail的断言。没有降低其他断言。
新增26项入口/CLI回归的预期在实现前写定，覆盖null、空、ASCII/Unicode空白、零宽字符、
非空正确/错误、已知失败组合、JSON格式、停止行为、固定分母、字段缺失和禁止回填。
首次执行时核心尚无新revision字段，记录中的相关KeyError是未实现新报告字段的结果；该记录保留，不包装成旧契约缺陷。

## 采集与CLI约束

未来采集端只有确认已观察零字节文本时才能提交""；未取得最终文本必须提交null。
不得用金标、空字符串、strip、布尔默认值或猜测补齐/替换原始字段，也不得无证据回填历史null。
CLI直接将解析后的JSON输入传递给评分核心；已通过实际IO回归验证null、""、空白均不被转换，输入文件不被修改。
核心parse_answer接受None并返回未观察状态，未创建假的空文本观测；其他维度继续各自规则。
没有自动迁移历史记录或重新评分旧结果。使用本修订的新报告必须保留measurement_revision和对应验证字节绑定。

本修订只解决原null待决项，尚未接入在线runner、采集Adapter或人工复核接口；这些仍由后续授权与主线程集成决定。
