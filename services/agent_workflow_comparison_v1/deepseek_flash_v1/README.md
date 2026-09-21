# DeepSeek Flash 原生 Responses 离线连接 v1

本目录验证真实 Agents SDK / OpenAI SDK 的 Responses HTTP 序列化、函数工具往返与观察采集。传输严格限定 httpx2.MockTransport，使用显式测试 Key；启动时核验历史基线关系、实际源码身份及 FCC 文件，随后在本进程禁止网络、DNS、UDP 与子进程。没有在线开关，也不读取真实 Key 或 claim store。

这是**实际评分器对 SDK＋FakeModel 等合成开发观察的离线评分**。不是实际 DeepSeek 服务连通、规划能力验证、能力排名、正式对照结果或真实 API 成本。固定规则和脚本均为工程测试驱动，不能以模板均通过推断效果相同。

## 运行

在本独立工作树根目录、Git 文件所有者上下文，用已有 Python 环境运行；不安装依赖、不改 safe.directory。每个 RUN_ID 必须未使用。

```powershell
python -B -X utf8 services/agent_workflow_comparison_v1/deepseek_flash_v1/verify.py --run-id NATIVE-REVIEW-NEW
python -B -X utf8 services/agent_workflow_comparison_v1/deepseek_flash_v1/run.py --run-id NATIVE-RUN-NEW
```

verify 仅执行本目录新增测试，并保存全16题双路径观察、FCC 原始报告和单独故障报告。run 只运行16题双路径。不得重复历史批次、覆盖 outputs 或将 CLI 退出0理解为正式实验通过。

## 来源与输入

开发基线 dfb568d26f0745e397f6cc06dc020ad1ee870de2；评分器固定 fcc2026c60943de6016495ad244291689a9d491d；结构 v1.0、测量 v1.1。导入仅来自本工作树 src，启动逐一比较 FCC 的评分器及契约 blob。

沿用未改动的 controlled_comparison_v1：16个内部已知任务、顺序、工具数据、独立事后评分合同和合成响应脚本。两条路径相同合法输入、只读工具与范围；交替先后顺序。脚本不读取评分合同。独立 fault 注入只用于适配器验证，不并入业务成功率。工具损坏的 fail 带 source_or_tool_not_model_verdict 归因。

选择文件固定请求 deepseek-flash，来源为2026-09-17官方文档映射记录，不能视为不可变权重版本。新增官方 OpenAI 函数调用参考为 https://developers.openai.com/api/docs/guides/function-calling 。2026-09-21重取 DeepSeek Responses 页面超时，本轮不声称重新核验了线上映射或服务能力。

## 观察投影

| 原生/执行来源 | 保存位置 | 约束 |
|---|---|---|
| SDK 发出的请求 JSON | native_wire_observation.fixture_requests | 仅合成输入；不保存 headers/认证；完整历史及工具 schema |
| 本次 function_call.call_id | native_tool_links.sdk_call_id | 映射本次 run_id、执行 call_id、artifact_ids；拒绝重复ID |
| Session.call 实际返回 | events.result / allowed_evidence | 仅成功实际返回可产出证据；内容哈希；不从金标补齐 |
| output_text.text | final_output | 原样保留；空串、空白分别记录；原生 null text 属非法响应、保持未观察 |
| status / incomplete_details | completion / known_failures | 已知截断优先保留，即使 usage 同时缺失；不伪造审批 |
| usage input/output/total/cache/reasoning | native_wire_observation.responses | 合成 fixture 来源；缺可选明细为 null，非法总量或范围停止 |
| FCC 输入 | scores.*.input | 复用声明字段投影，不改变回答、分母或标准 |
| FCC 输出 | scores.*.raw_report / error | 原样保留；ContractError 不回退 stub |

固定路径模型请求为0、模型 token 来源 not_applicable；实际 API tokens/cost 均未观察。Agent 的 usage、缓存和预算金额全部是合成测试值，响应时延是本地工程计时。字节计数不是已核验的 DeepSeek token 上界。人为审阅时间为 null，未用模拟时间替代。

16条计划始终保留；批次停止后的条目 not_executed，缺文本为 null。未解析比例在 text_parse_observation 中显示，任务 unknown 在 FCC counts 中保留。固定模板具备解析格式优势；自由表达的解析 unknown 不能直接当作能力劣势。自由表达故障样例独立展示，不改写原句以降低 unknown。

## 范围与限制

本轮调用真实 SDK 与 FCC，未使用共享生产 Provider Adapter。共享 DeepSeek 允许列表仍仅列旧模型ID；生产 telemetry 仍要求合法 campaign_runtime 权限和来源绑定。本目录没有仿造这些对象，offline 输出目录也不是生产 claim。reasoning=none、store=false、并行工具关闭、最大输出1000、无重试、无重定向均是离线候选设置，尚非线上授权。

本目录为发布派生副本，旧原型和全部历史记录保留在原工作树。本机只验证携入源码，历史原件检查明确为未执行。统一联合验证、文件清单及哈希见 ../controlled_publication_v1/README.md；后续范围见 AUTHORIZATION_NEXT.md。
