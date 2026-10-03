# Item6 experiment bridge v1 — implementation candidate

## 具名修订 item6-response-actions/1.1（有界离线候选）

本节是对响应动作选择的前瞻性修订，不重写已经执行的
`cfd590f33268654477907bbca76be2385b455589` 版本、其授权或失败证据。
该历史版本将工具调用与文本分片同现拒绝为 `item6_response_actions`；
新实现只有在另行完成源码承诺、固定版本验证及新的在线授权后才能用于线上。
artifact/archive仍为1.1，FCC结构v1.0/测量v1.1、任务、顺序、提示、工具、预算及权限合同不变。

响应形状上限分别计数function_call、message和output_text分片，不把message数量与文本分片数量混为一谈：

| function_call数量 | message数量 | output_text分片总数 | 本修订语义 |
| ---: | ---: | ---: | --- |
| 0 | 1 | 1 | 作为本次最终文本；原字符串不改写，空串/空白仍由原测量规则判定 |
| 1 | 0 | 0 | 执行合法工具并由SDK继续，尚无最终文本 |
| 1 | 1 | 0或1 | 允许伴随message；其中的文本是中间说明，不是最终答案、完成状态或允许证据 |
| 0 | 任意 | 0 | 无动作，拒绝 |
| 大于1 | 任意 | 任意 | 拒绝；本修订不开放多工具调用或扩大工具预算 |
| 任意 | 大于1 | 任意 | 拒绝，包含额外空message；避免SDK选择最后message而业务记录选择另一条 |
| 任意 | 任意 | 大于1 | 拒绝，不拼接、不丢弃分片后伪装成一个最终文本 |

function_call自身仍须明确completed，call_id不得重复，工具与参数必须来自原允许列表。
message仍须assistant/completed，content只允许output_text；reasoning、未知item、refusal part等原拒绝路径不变。
所有伴随文本（包括空串和空白）仍先经过原写入前隐私/限长检查，不能因其不是最终答案就绕过扫描。
返回给SDK的响应及内存replay保持原有item、顺序和值；不得删掉message、改写文本或错配function_call_output的call_id。

有工具调用时，不设置case的final_output/completion为最终状态。只有后续无工具调用、符合上表的最终响应
才能提供最终文本；若工具执行、后续请求或预算失败，中间说明不得被提升为final_output，已知失败保持优先。
不新增伴随文本正文的持久化，不扩展旧completion metadata的内容边界。
请求中的parallel_tool_calls=false继续保留，但它不是响应中不会出现message或多个工具的保证；本地门禁仍负责检查。

本批仅修订本节、实验专用解析及相关offline_test夹具/测试。不更改共享准入、真实claim store、评分规则、
旧manifest或CI锚点；项目源码承诺此时预期尚未接续。隔离测试生成的synthetic manifest/commit仅证明测试夹具，
不冒充项目发布或线上权限。旧失败不能由新规则追认成功，也不能使用旧授权补跑剩余用例。

## 工具拒绝错误归因的限定修订

设计缺失或冲突时零工具、先澄清的规则不变。本修订仅保留被锁定SDK包装后的具体拒绝原因：
精确的 `agents.exceptions.UserError` 直接 `__cause__` 为精确 `ExperimentError`，且其原生字符串
code 等于 `item6_tool_before_design_or_refusal` 时，归档保留该code，而不是泛化为 `item6_execution_failed`。
不读取异常正文、args或run_data；不沿任意cause链或context遍历，不接受子类、同名/module伪装或其他code。
其他包装仍保留通用拒绝。旧直接异常分支保持原语义，本修订不是对所有异常类型信任机制的全面加固。

修订不改变是否允许执行、停止规则、评分标准或归档schema。旧在线归档不回填专用code、不追认成功。
IC-11回归仅由保留动作计划构造MockTransport合成响应，并贯通实际Adapter/SDK、原Case.call门禁、
失败归档及独立回读；不是原始线上响应重放或新模型效果证据。正式源码承诺接续与线上授权另行处理。

## 四项复审修订（本包artifact/archive v1.1）

function_call必须明确status=completed才可进入工具计划或执行；顶层completed不能覆盖工具项incomplete、null或缺失。费用记录将usage观测与cost_settled分开：只有完整计数、总量关系及缓存/推理子计数均合法并实际计入账本，费用才已知。有效费用超预算仍保留真实结算和overshoot；未结算总额为null，已知小计单列。没有模型请求的空账本仍为已知零模型费用，固定路径usage不适用；这些都不是真实Provider账单。

source_manifest_sha256在入口源码核验时就与实际manifest文件比较，先于认领。该检查也在实际认领前再次执行，不依赖归档回读补救。

本包artifact/archive结构修订为1.1，评分器结构v1.0/测量v1.1不变。模型/清理phase在评分和审计导出之后结束；评分、导出、序列化、封存与最终记录写入前后均受批准UTC截止与剩余单调时钟预算检查。阻塞操作结束后发现越界时只记录失败，不返回completed。

sealed.json绑定experiment.json及audit.sqlite3。新的finalization.json在封存返回后记录真实观察时间、剩余预算/耗时、运行身份及seal摘要；它不写回原审计链，避免循环或改写已封存内容。成功结果另外返回finalization_sha256，verify必须同时提供来自成功运行结果的两个独立摘要。四文件目录才可申请完整回读。finalization-failure.json表示最终阶段失败，优先拒绝；已有三文件或候选completed状态不能单独作为最终成功。诊断写入失败也抛错，不能从目录自行计算摘要来把未成功返回的运行改称成功。旧r5三文件归档通过其原生产者版本回读，不能自动升级为本修订。

最终阶段失败保留原文件和32条部分观察。超窗后允许写失败诊断，不再评分、发送或伪造成功终态。最终记录是观察凭据，不是权限、真实批准或Provider成绩。

本合同与代码尚待本批离线验证及完整审阅；不是在线授权或效果成绩。基线5605396e165fc5140eba54340d5a9c93f67540dc，评分器FCC及其v1.1测量语义保持原字节。第一批16题、顺序、提示、工具数据与金标不改。

## 能力链

静态验证生成进程内Prepared；内部认领函数调用现有固定Windows backend的真实排他写入并消费fresh winner；只有该路径可注册Owner。调用者receipt、同名类、字典、序列化对象和scope字符串都不是权限。Owner绑定模式、批准摘要、源码、进程、时钟域、真实factory及当前case。认领失败或结果未知不可重试、恢复、释放或改路径。

live入口只接受live材料，offline_test入口是测试内部入口。测试OS locator必须指向系统临时目录中新的item6-test-store-*目录；默认真实locator不能进入测试认领。测试HTTP delegate必须是精确MockTransport；live只能用受控原生HTTP transport。生产CLI没有store、transport、跳过批准或测试模式开关。

只有两个共享模块新增限定分支：model_providers和surface_mapping。旧validate_model及_DEEPSEEK_MODELS含义不变；新模型只对真实实验session开放。新mapping由共享边界在验证真实owner及单次factory消费后签发，直接私有token调用也不能签发新范围。

## 来源及无循环依赖

source-only v3覆盖原src/evals、原显式依赖、新桥规范/代码/测试及第一批服务清单。v1/v2 manifest不改；v2历史锚点固定到5605396e/tree60a29c，逐blob验证。v3排除自身。会引用v3承诺的根CI及新窄CI不进入v3自哈希，另由实际执行commit/tree及freeze.workflow_sha256约束。freeze、批准、验证输出、真实store均在源码选择范围外。

只有源码稳定后才排他生成项目v3 manifest；测试fixture可在各自新的临时Git树内生成测试manifest和合成提交。它们不能冒充项目执行commit，也不发布。旧在线source拒绝、历史重放及原归因保持。

## 计划、预算、发送与停止

业务分母固定32条，模型分母固定16个case；原IC业务题号保持不变，freeze.model_case_handles以具名摘要绑定合法PCECASE遥测handle，禁止混用两种ID；上限不是实际请求数。固定路径无模型session。每次请求由SDK/Adapter建立attempt，预算预留、写前审计、实际请求内容/replay核验、send-intent和最后期限检查均在delegate调用前。没有SDK/HTTP重试、fallback、重定向、代理或后台上报。

只允许inspect_sources/read_aggregate读取冻结合成源。开始前拒绝not_executed，开始后失败observed，未执行后缀保留。一般业务read_failed和FCC fail/unknown不自动变成全批权限故障，不重试答案；权限、来源、传输、预算或审计故障停止新增工作。

policy的数字是实验工程天花板，非真实费用批准；live freeze需独立预算与官方价格依据。input的observed_stop不声称原生token/账单硬限。offline_test费用标synthetic_fixture；未知usage总账为null，已知小计单列，不倒改overshoot。

## 观察、隐私及归档

旧completion记录仍只含metadata。usage、native/normalized/source和timing复用同一终态事件；新业务观察独立保留final_output与必要工具证据。null、空串、空白不互换；原句不改写后再评分。金标只被源码完整性层哈希和事后评分使用，绝不进入请求、工具或脚本决策。

请求32768 bytes，响应100000 bytes，新业务字段131072 bytes，总归档8388608 bytes。正文写入前检查Key canary、身份/路径/Authorization/reasoning痕迹；拒绝正文时只保留安全代码与缺失事实，不把删改答案称为原文。

新命名空间排他创建，SQLite与JSON均进入封存摘要。独立新进程回读检查实际SQLite事件、只读mapping、动态分母、timing引用、预算、工具证据与FCC结果。同步重算外层hash不能消除语义矛盾。缺失或失败的清理、审计和封存不得标完整成功。回读只产生去权限结论，不能重建Owner。

全部测试必须显式标注临时store、假Key、synthetic approval/commit、MockTransport、provider_calls=0。共享环境、旧manifest、FCC、20/60及STATUS/T7保持。根全量、Git发布、真实Provider/Key/store和在线窗口不在本批授权内。

## 有界验证与交付命令

在已有锁定依赖的Python 3.12环境中，将仓库src和根目录加入PYTHONPATH，执行：

```text
python -B -X utf8 -m tests.item6_experiment_fixture suite NEW_RUN_ID
```

入口只选择新增6模块和直接受影响9模块，不做根目录discover。每个批次排他创建output/item6-bridge-validation/NEW_RUN_ID；失败、取消、未执行位置和各子进程实际退出码保留。临时Git提交与bundle明确是合成测试对象，不是项目Git发布或最终执行commit。较早记录不得改贴新源码。

源码稳定后使用 `python -B -X utf8 scripts/build_internal_source_integrity_v3.py --expected-base 5605396e165fc5140eba54340d5a9c93f67540dc` 排他生成项目v3。根CI当前锚点在v3选择范围之外更新；两份CI文件再由freeze和真实执行commit/tree分别约束，防止循环。旧manifest不能覆盖。

生产CLI的prepare仅生成未批准草稿，run只接受完整live合同与独立批准；offline_test只能走隔离harness。verify只读归档，不恢复owner。live原生input边界/observed-stop语义、价格及用户时间窗口仍待后续单独冻结，本批不能据此调用Provider。

测试进程的外部看门时间和CI预算只约束离线验证，不改变30秒请求、120秒任务或2700秒批次的生产工程天花板。实际运行预算还须由freeze明确给出，并受批准窗口更短的期限约束。
