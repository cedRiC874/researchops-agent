# 离线控制面演示

**离线、脚本驱动、未调用模型**。这段108秒GIF来自2026-10-09新采集的真实离线控制面记录，用分幕回放讲解；不是实时终端录像，不包含真人审批见证，也没有剪接Main51真实线上运行。

![离线、脚本驱动、未调用模型](media/offline-control-demo.gif)

## 六步与讲解节奏

| 时间 | 画面 | 实际证据 |
| --- | --- | --- |
| 0—7秒 | 来源、合成数据与离线标签 | 脚本驱动；模型调用0 |
| 7—20秒 | 只读调用 | 主案例A：读取成功，无发布目录 |
| 20—33秒 | 发布挂起 | awaiting_approval，handler尝试0，无发布目录 |
| 33—46秒 | CLI批准 | approved，handler仍未执行；批准由脚本调用CLI |
| 46—63秒 | 显式执行 | 同一案例A执行一次，生成新发布产物 |
| 63—79秒 | 改参数被拒 | 独立案例B：tool_approval_mismatch，handler尝试0 |
| 79—95秒 | 过期被拒 | 独立案例C：真实1秒TTL到期，CLI exit4，handler尝试0 |
| 95—108秒 | 两条效应值追证与局限 | −5.61／−6.79 mmHg → evidence ID → 原始指标路径 |

停留时间仅是讲解节奏。拒绝是终态，不把同一个已拒绝调用恢复成成功。CLI命令中的调用别名用于展示，不能复制执行；采集使用新隔离目录，不改已有产物。

## 能证明什么、不能证明什么

- 原[执行器](../src/researchops/tool_runtime.py)负责权限和审批；原[CLI](../src/researchops/cli.py)负责批准／显式恢复，没有改生产源码来制作演示。
- 四个真实CLI进程的实际退出码是0、0、0、4；最后一个是预期过期拒绝，不是隐藏失败。原始记录本地保留，公开[白名单证据](evidence/offline-control-demo-v1/README.md)及[摘要](evidence/offline-control-demo-v1/summary.json)。
- [reporting.py](../src/researchops/reporting.py)提供两条效应claim，本次展示并回核这两条；不夸大为报告所有数字都已有字段级追证。
- 数据来自历史phase3合成分析，不是临床客户数据、新模型成绩或Main51业务结果。
- **当前真实 Agent 在审批暂停后不能恢复。** 本演示只展示脚本驱动的控制面；不证明模型能规划、抵抗提示注入或在批准后自动继续。

## 离线复核与容器

现有[Windows入口](../scripts/portfolio_demo.ps1)与[Linux入口](../scripts/portfolio_demo.sh)运行确定性分析、报告和离线核验，与本GIF的画面采集不是同一个入口。制作GIF没有重跑历史模型评测。

严格数值复现目前支持Windows／Linux x86-64及各自锁定依赖，不把云端Linux当作原生macOS／ARM验证。[官方Python 3.12 devcontainer候选](CODESPACES.md)由独立CI构建并执行现有离线demo；托管CI成功前不提供Codespaces按钮。本批不启动云实例，也不承诺云服务免费。
