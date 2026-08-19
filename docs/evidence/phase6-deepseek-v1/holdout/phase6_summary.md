# Phase 6 在线 Agent 评测

> 范围：OpenAI Agents SDK 驱动的 provider 模型规划、工具轨迹、最终证据回答与审批中断。

- Provider：`deepseek`
- Transport：`openai_compatible_responses`
- 模型：`deepseek-v4-flash`
- 单次响应输出上限：2000 tokens
- Split：`holdout`
- 成功率：100.0%
- 通过：4/4
- Agent 执行失败：0；Harness 错误：0
- 逻辑工具错误率：0.0
- 本地工具 attempt 错误率：0.0
- 回答完整性准确率/覆盖率：1.0/1.0；失败数：0
- Evidence 标签完整性准确率：1.0
- Numeric CLAIM 任务准确率：1.0
- Evidence precision：1.0
- 延迟 P50/P95（ms）：5046.617100015283/14585.47470002668
- 成本状态：`unavailable`；总成本：None
