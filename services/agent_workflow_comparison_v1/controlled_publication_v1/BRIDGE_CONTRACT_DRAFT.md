# 实验权限／遥测接桥接口草案（未实现，未授予发送权限）

本批只发布离线源码派生副本。以下是下一批待审接口，不能作为授权对象、claim、线上准入证明或可执行预算。

## 状态与所有权

候选状态：unprepared → validated → claimed → active → stopped/closed。每次失败终止新工作；没有自动重试、恢复或fallback。validated只表示静态合同检查，不授予发送权限。claimed必须来自批准摘要及真实一次性占用流程，测试字典、离线目录锁或MockTransport均不能生成该状态。

`validate_experiment(freeze, approval_digest)`：校验固定执行commit、完整源码清单、FCC文件、16题及顺序、两条路径、工具和数据范围、预算、价格依据、环境与期限。不读取Key，不操作store。

`claim_experiment(validated, ownership_receipt)`：未来由受控入口取得合法一次性所有权后创建精确类型owner。所有权必须绑定执行commit、授权摘要、源码清单、独立评分合同摘要及时钟域；拒绝重用、跨实验和过期。不得借用 Internal30 或旧 campaign 的所有权。

`open_case(owner, task_id, path_kind, run_id)`：在完整32条任务观察计划内选取唯一条目。Agent请求分母另建16个case与每题最多3次请求的计划；固定路径模型请求为0、不伪造模型case或响应。区分业务计划分母与实际请求分母。

`begin_attempt(case)`：发送前完成预算预留、写前审计、来源/期限/请求体核验；冻结origin、模型、reasoning、工具白名单、输出上限和无重试策略。任何审计写入失败必须阻止发送。

`finish_attempt(handle, terminal)`：唯一终态为accepted/rejected/http_error/no_response/cancelled/outcome_unknown。终态写入失败或发送结果不明时停止，保留预留和不确定性，不能重复发送。SDK记录、传输观察和遥测attempt按run/case/call关联。

## 复用与共享改动候选

复用 RuntimeCaseTelemetrySession、VerifiedRuntimeDenominatorPlanBinding、CompletionTelemetryCollector、AuditLedger、LedgerCompletionTelemetrySession 的现有能力和字段；不复制整套控制面。需要新实验专用owner、范围和精确session类型，不将内部v1或first-live范围当作本实验许可。

预计共享修改候选为 `src/researchops/model_providers.py`（精确session准入及模型/传输约束）和 `src/researchops_completion_telemetry/surface_mapping.py`（新的限定权限范围与受控绑定）。实施前提交具体补丁审阅；若还需修改审计schema、评分标准、旧runner或冻结合同，停止该扩展并报告。新范围不能仅靠字符串或私有token的直接调用获得权限。

现有模型允许列表不含deepseek-flash。后续只能以明确模型与合法实验会话的绑定准入，不静默换旧别名、不改成任意模型ID。主观声称“用户已确认”不能替代所有权校验。

## 正文与遥测

单独的实验观察采集契约保留final_output原文、null/空串/空白、工具实际返回、run_id/call_id/artifact_id及源哈希。只有实际成功工具可提供有效证据；金标只进入事后评分。不得将文本放进原T6元数据字段，也不扩大旧采集标准。

请求/用量/缓存字段有来源与可用性；缺失为unknown/null；已知失败保留。输出上限与原生token计数、缓存定价须另核验。不得保存认证头、Key、推理正文。当前合成哨兵测试不构成生产DLP证明。

## 下一批的有界离线验证

使用临时隔离存储与MockTransport，验证真实工厂/计划/ledger能力链。正路径必须走合法工厂；拒绝普通字典、篡改receipt、跨run、重复所有权、错误权限范围、过期、来源漂移、未知结果重试及审计故障。测试用receipt必须明确为临时测试命名空间，不可在真实store或线上入口消费。

另验证原Provider及Internal/campaign范围不扩大、原遥测schema不变、32条业务分母和请求分母正确、16题两条路径经过真实FCC评分。联合套件只包含新增和受影响模块；不能用本批89项覆盖未来接桥实现。

执行commit、原生token上界、价格/真实预算及第四步在线授权单尚未具备。本合同没有在线日期、批准摘要或可消费的授权内容。
