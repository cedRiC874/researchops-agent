# ResearchOps Agent

**中文** · [English](README.en.md) · [文档入口](docs/README.md) · [项目状态](STATUS.md)

科研数据分析场景的受控Agent原型：模型编排工具，代码约束权限，结果关联证据。项目首先回答的是：**什么时候不该用Agent？**

![离线、脚本驱动、未调用模型：只读调用、发布挂起、CLI批准、执行及两种拒绝](docs/media/offline-control-demo.gif)

**离线、脚本驱动、未调用模型** · 108秒实际离线记录回放：只读调用 → 发布挂起 → CLI批准 → 执行 → 改参数被拒 → 过期被拒。两种拒绝是独立案例；CLI批准由脚本发起，不冒充真人审批录像。未剪接真实模型运行。[分镜、证据与局限](docs/DEMO.md)

## 任务定义清楚时，先用固定流程

2026-10-04，同一组16道开发方已知合成任务、同一冻结工具证据下：

| 路径 | 通过 | 失败 | 无法判定 | 模型请求 |
| --- | ---: | ---: | ---: | ---: |
| 固定流程 | 16 | 0 | 0 | 0 |
| Agent | 13 | 1 | 2 | 30 |

32条业务观察完整收集，但不是业务全部通过。固定流程直接实现规则、单位换算和模板；Agent还需要生成正确参数与文字。这个结果支持本组明确任务优先采用固定流程，不足以排名通用能力。

Agent的新增价值需要在开放式输入、变化的目标和动态工具选择中另行证明，并抵偿调用成本、延迟与风险。本项目尚未证明这类优势，不能从13/16推导未知任务上的收益。

[结果、失败案例与固定版本](docs/evidence/main51-controlled-observation-v1/README.md)

## 四个工程重点

- **工具编排**：仅逻辑资源ID和白名单工具，不直接运行任意Python、SQL或shell。
- **权限边界**：受控写入停在待审批状态，批准绑定具体范围，恢复时重新核对；拒绝不是等待审批。
- **响应遥测**：原生／归一化状态分栏，usage和计时关联同一事件；缺失保持未知。
- **证据核验**：报告统计值关联evidence ID、指标路径及显示值；审计、评分和归档保留失败。

Main51对照只有两个只读工具；通用科研分析、离线审批演示和服务切片是不同路径，不拼成一套已生产验收的系统。

## 改变设计判断的教训

1. **能力与格式遵循分开测。** 早期对照中，额外查询的严格序列要求与有限文本解析影响了很多分数；也有真实不合格工具请求，不能把所有失败归咎格式。unknown不等于错误或通过。
2. **检查结果与必要不变量，而非唯一轨迹。** 一次额外只读查询可能合理；权限、证据来源、预算和副作用约束则必须持续成立。这是后继设计原则，不回改旧协议成绩。
3. **算术交给确定性实现。** IC-04取得0.00625 g，却输出0.00625 mg，正确值应为6.25 mg。有证据和引用ID不代表最终文字正确。
4. **CI绿色不等于质量达标。** 历史workflow成功但质量结果仅44/50、证据引用10/21；后续才接入质量阈值和真实退出传播。[事故记录](docs/evidence/main-offline-gate-20260822/README.md)

## 安全覆盖与缺口

对照[OWASP LLM Top 10 2025](https://genai.owasp.org/llm-top-10/)，不是安全认证或完备防御声明。

| 风险 | 覆盖状态与对应证据 | 仍需注意 |
| --- | --- | --- |
| 提示注入 | **部分覆盖**：[公开场景](evals/v2/public_tasks.jsonl)及[4项离线描述投毒测试](tests/test_phase6_tool_description_poisoning.py) | 不证明真实模型抵抗投毒 |
| 敏感信息泄露 | **已测试**：[写前扫描／持久化拒绝](tests/test_completion_telemetry_ledger.py) | 未覆盖完备DLP，不存完整body |
| 过度代理 | **已测试**：[范围绑定审批与执行](tests/test_tool_runtime.py) | 未覆盖生产多租户授权 |
| 错误信息 | **已测试**：[报告追证](tests/test_reporting.py)及[实际失败](docs/evidence/main51-controlled-observation-v1/README.md) | IC-04仍有错误，引用ID不是保证 |
| 无限制消耗 | **已测试**：[预算／未知usage](tests/test_item6_experiment_budget.py) | 未覆盖账单硬封顶，存在观测停止线 |

[风险映射、已有注入题与待补测试](docs/SECURITY_OWASP.md)

## 已知限制

**当前真实 Agent 在审批暂停后不能恢复。** 本页离线演示使用脚本驱动的Phase4控制面，不把它与Main51真实线上结果剪成同一次执行。安全表“已测试”只指所链接的限定场景，不是完备安全声明。

## 继续阅读

离线复核使用Python 3.12及锁定依赖：Windows x86-64入口为`scripts\portfolio_demo.ps1`；Linux x86-64入口为`scripts/portfolio_demo.sh`，使用`requirements.linux.lock`。严格数值复现尚不支持原生macOS 与 ARM；Codespaces路线仍待单独构建验证，不假称已一键可用。

- [一页项目说明](docs/PORTFOLIO.md) · [文档导航](docs/README.md) · [当前状态](STATUS.md)
- [聚合结果图](artifacts/phase3/effect_estimates.png)：历史模拟分析，212个available cases，不是完整ITT。
- [文章中文版](https://zhuanlan.zhihu.com/p/2078131794466099751) · [Hacker News讨论](https://news.ycombinator.com/item?id=49518667)

这是研究原型／作品集，不是临床决策工具或生产SLA证明。Depth-60仍为20/60，旧归因与原STATUS/T7不被新结果覆盖。个人贡献和AI辅助分工待本人确认，不填写未经核实的工时。

License: [MIT](LICENSE)
