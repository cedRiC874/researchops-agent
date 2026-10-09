# 文档导航

ResearchOps 同时保留固定工作流与 Agent 编排，用任务观察检验何时需要模型。本组已知任务的对照首先支持固定工作流；入口与证据分列如下。

| 想了解什么 | 从这里开始 |
| --- | --- |
| 项目定位与快速开始 | [项目首页](../README.md) |
| 为什么这组任务不优先使用 Agent | [一页项目说明](PORTFOLIO.md) |
| 模拟数据、聚合结果与审批流程 | [离线演示](DEMO.md) |
| 固定工作流与 Agent 的 16 题结果、发布与验证限制 | [Main51 受控观察证据](evidence/main51-controlled-observation-v1/README.md) |
| 工具、权限、统计计算与证据的分工 | [架构说明](ARCHITECTURE.md) |
| 攻击面、现有控制与未覆盖项 | [安全边界与 OWASP 对照](SECURITY_OWASP.md) |
| 历史评测及各自声明边界 | [证据索引](EVIDENCE.md) |
| 当前状态与尚未关闭的问题 | [项目状态](../STATUS.md) |

## 阅读证据时

根目录的[`probe_out_v3.json`](../probe_out_v3.json)是冻结输入，由[历史surface mapping](../src/researchops_completion_telemetry/surface_mapping.py)、[Depth-60 source-bundle](../src/researchops/phase6_source_bundle.py)与[Internal v3承诺](../evals/provider_completion_internal_source_v3/source_manifest_v3.json)引用；不移动、不重写历史哈希。

每份结果都应与自己的任务集、源码版本和评分口径一起阅读。离线回归通过不等于在线模型表现；内部已知任务的观察不等于未知任务泛化；失败、未知与跳过各自保留，不换算成通过。

旧运行记录与冻结材料用于追溯历史，不自动成为当前运行指令或新调用授权。Provider 的历史验证范围可从 [DeepSeek](DEEPSEEK_COMPLETION_FIRST_LIVE_VALIDATION_V1.md)、[Anthropic](ANTHROPIC_PROVIDER.md) 和 [Kimi](KIMI_PROVIDER.md) 文档继续查阅。

[返回项目首页](../README.md)
