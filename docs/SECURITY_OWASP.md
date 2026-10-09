# 安全设计与OWASP风险对照

采用[OWASP LLM Top 10 2025](https://genai.owasp.org/llm-top-10/)的明确版本标识。这里列设计、已有证据和缺口，不表示OWASP认证、完整渗透测试或风险全部消除。

“已测试”仅指链接中的限定场景，不等于风险已消除；“仅设计”没有执行证据，“未覆盖”不计为通过。已有版本的通过记录见[Main51工程证据](evidence/main51-controlled-observation-v1/README.md)，本PR的新固定head仍须完整CI。

| OWASP项 | 覆盖状态 | 设计与测试／证据 | 仅设计或未覆盖的范围 |
| --- | --- | --- | --- |
| [LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) | **部分覆盖** | [公开攻击语料](../evals/v2/public_tasks.jsonl)；[工具描述投毒的离线边界测试](../tests/test_phase6_tool_description_poisoning.py)，已有本地4项通过 | **未覆盖**：真实模型对第三方工具描述投毒的抵抗能力；scripted Model不证明模型理解了攻击 |
| [LLM02 Sensitive Information Disclosure](https://genai.owasp.org/llmrisk/llm022025-sensitive-information-disclosure/) | **已测试**，限持久化边界 | [记录合同与缺失语义](../tests/test_provider_completion_record_contract.py)、[Key回显／incomplete_details写入拒绝及篡改](../tests/test_completion_telemetry_ledger.py) | **未覆盖**：完备DLP或所有未知隐私形态；不保存全量Provider body |
| [LLM06 Excessive Agency](https://genai.owasp.org/llmrisk/llm06-excessive-agency/) | **已测试**，限本地控制面 | [审批、过期、参数变化与幂等](../tests/test_tool_runtime.py)、[真实SDK审批暂停](../tests/test_phase6_agent.py) | **未覆盖**：真实Agent审批后恢复、生产多租户／分布式授权；Phase4与Phase6不合并声明 |
| [LLM09 Misinformation](https://genai.owasp.org/llmrisk/llm092025-misinformation/) | **已测试**，限证据校验与分层判定 | [统计值追证](../tests/test_reporting.py)、[Main51三题原判](evidence/main51-controlled-observation-v1/README.md)、[44/50事故](evidence/main-offline-gate-20260822/README.md) | **未覆盖**：保证所有生成内容正确；IC-04仍有错误，unknown不回填通过 |
| [LLM10 Unbounded Consumption](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/) | **已测试**，限冻结预算与停止 | [观测预算与未知usage](../tests/test_item6_experiment_budget.py)、[时钟与截止](../tests/test_completion_timing_clock.py) | **未覆盖**：实际账单硬封顶；输入／费用存在响应后观测与在途超额边界 |

## 已有的公开提示注入场景

项目不是“尚无prompt injection题”：

- Phase6中的P6-DEV-015／052／053包含跳过审批或伪系统指令；删除审计、伪证据等场景也已存在。文件与ID见[Phase6合同](../evals/PHASE6.md)及语料，不据此推造新在线成绩。
- [Eval v2公开语料](../evals/v2/public_tasks.jsonl)有`prompt_injection`分类，例如V2-DEV-006／013／025。部分题在用户prompt中转述“数据注释／工具结果要求泄露”，并非真的从恶意工具描述加载攻击。

展示应对齐攻击请求与执行轨迹，说明哪些工具允许、哪些写入仍需审批，不写“全部抵御注入”。

## 工具描述投毒的离线补充

定向测试在临时隔离环境、scripted Model及真实SDK／执行器边界下进行，只在内存替换description，不修改历史语料或发送真实Provider请求。记录恶意描述确实进入对应模型测试输入，再检查：越权逻辑资源拒绝、发布只进入待审批、未经批准处理器调用0次且无产物，同时保留正常正例。

本地候选已新增[test_phase6_tool_description_poisoning.py](../tests/test_phase6_tool_description_poisoning.py)：4项通过，0 failures/errors/skips，实际exit0；包含正常读取、正常／投毒发布均需审批、投毒越权资源拒绝，以及独立本地批准后的写入哨兵正例。发布用例中的clean／poisoned是子例，不另加到4项分母。

描述实际出现在SDK模型接口的tools输入中，权限、审批回调、执行器和临时SQLite账本保持真实；禁止Key和HTTP发送的guard没有替代这些门禁。6个直接绑定输入前后稳定，不冒充全树或根全量验证。没有Provider调用或真实store访问。

这类测试证明执行层如何处理被诱导的调用，不证明模型自行识别投毒，也不证明真实Provider收到过该描述；真实模型抵抗能力须另行预注册与授权。本地通过不等于新固定head的CI已通过。

这4项针对Phase6与ControlledToolExecutor边界，不是Main51专用发送门的新验证，更不是对其30次历史线上请求追加攻击或重算成绩。

若需修改生产源码、schema或进入source-only选择集的fixture，须明确接续范围，不借文档整理放宽门禁。
