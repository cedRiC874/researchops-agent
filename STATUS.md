# 项目进展

更新：2026-10-10。最近一次模型任务观察发生在2026-10-04；此后的文档和演示整理没有产生新成绩。

**这是可以查看和运行离线演示的研究原型，还不是已经证明适合真实业务的产品。**

## 现在可以看什么、运行什么

- [中英文首页与40秒演示](README.md)已随[PR #52](https://github.com/cedRiC874/researchops-agent/pull/52)合并。演示展示读取、等待审批、批准后执行，以及参数变化和过期时的拒绝；它由脚本驱动，不调用模型。
- [快速开始](README.md)可以在本机运行50题离线演示，无需模型Key。浏览器里的开发容器也已通过[实际构建与离线演示检查](https://github.com/cedRiC874/researchops-agent/actions/runs/37977328797)。创建Codespaces仍由使用者确认，用量和费用以GitHub账户提示为准。
- 作品集改版在合并前通过了全部9项自动检查。**合并后的完整检查尚未全部结束**：当前5项中3项通过，完整测试和流程集成两项仍在运行。[完整测试进度](https://github.com/cedRiC874/researchops-agent/actions/runs/37977328849) · [流程集成进度](https://github.com/cedRiC874/researchops-agent/actions/runs/37977328928)。这些状态对应[本次合并版本](https://github.com/cedRiC874/researchops-agent/commit/d11641e52590d4bc55df4896ad27d4b81cc732cf)，不是对未来版本的保证。

## 目前观察到了什么

在16道开发方事先知道的合成任务上，两种实现各运行了一次：

| 实现方式 | 通过 | 失败 | 无法判定 | 模型请求 |
| --- | ---: | ---: | ---: | ---: |
| 固定流程 | 16 | 0 | 0 | 0 |
| Agent | 13 | 1 | 2 | 30 |

一次失败是单位换算错误；另外两题分别涉及输出格式和澄清行为，仍保留“无法判定”。本次30次模型响应的用量、结束状态和耗时都有记录，归档能够回读，但记录完整不代表答案全对。[逐题问题与证据](docs/evidence/main51-controlled-observation-v1/README.md)

这组任务定义清楚，固定流程更适合作为默认方案。结果不证明Agent在陌生任务上的能力，也不是模型排名。

## 还有什么没有完成

- **真实Agent在等待审批后还不能恢复执行。** 视频里的批准后继续属于独立的离线控制面，不把两条路径混成一个已完成的功能。
- 尚未完成外部独立验收，也没有生产可靠性或临床使用的证据。内部题目通过和自动测试绿色都不能替代这些验证。
- 部分历史运行的记录不足，失败原因仍无法确认；新记录不能补回旧证据。早期60题严格评测仍是20/60，不重新解释成更高成绩。
- 旧在线授权不能重复使用。任何新的模型调用都需要单独明确范围、预算和授权；本次文档修改不授权重跑。

历史费用、不同版本的测试数量、跳过项和失败记录保留在[结果证据](docs/evidence/main51-controlled-observation-v1/README.md)与[原完整账本](docs/archive/STATUS-before-portfolio-cleanup-20261009.md)中，不合并计算成新版本通过。

<details>
<summary>保留的历史问题追踪标记</summary>

旧的外部验证与关闭门槛（T6-B／T7）仍未完成。为保持既有检查兼容，保留原状态文字，不改变其含义。

状态：`open / cross-cutting / causal attribution incomplete`

</details>

[项目首页](README.md) · [文档入口](docs/README.md)
