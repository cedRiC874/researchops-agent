# 离线控制面演示证据

2026-10-09采集；**离线、脚本驱动、未调用模型**。这是Phase4真实控制面与CLI执行记录的分幕回放，不是实时终端录像、真人审批见证或真实Agent自主规划。没有剪接Main51或其他线上运行。

## 来源与结果

- 生产实现基线：`a877a5a0c6ae1dedc5faec61726d86279aafe749`。本批只改文档、容器配置和测试，未修改采集所用的生产源码。
- [白名单投影](summary.json)保留6个阶段、4个CLI进程的实际退出码`0 / 0 / 0 / 4`、6份源码绑定及两条报告claim；原始审计数据库、绝对路径、调用身份及完整参数不公开。
- 主流程依次为只读调用、发布挂起、CLI批准、显式执行。挂起和批准时handler尝试数均为0，目录不存在；显式执行后才有新发布产物。
- 改参数与过期使用各自独立调用，不把同一个已拒绝调用恢复为成功。参数不匹配返回`tool_approval_mismatch`；过期返回`tool_approval_expired`，CLI实际exit4是预期拒绝。二者handler尝试数均为0，没有发布产物。
- 过期测试使用1秒TTL，真实等待1.078秒；没有改时钟。参数案例的批准使用CLI，修改后执行使用原受控执行器。
- 三个运行的审计链回核有效，模型调用数均为0；本次采集外层实际exit0。原源码、phase3证据及此前发布产物未变，仅新建本次隔离演示产物。

## GIF与公开材料边界

[GIF](../../media/offline-control-demo.gif)由同一次实际结果生成8张静态浏览器画面后编码，1280×860，每页5秒、共40秒、302834字节；SHA-256为`35ef336e99d598d5f5f4440f0dcaaa9d4618d1cf2744c3df44bd726ab7c03a2d`。每幕停留时间是讲解节奏，不是执行耗时。调用别名只为展示，不是可复制执行命令。

此次仅将首页说明中的“Main51”改为“16 题对照”并缩短停留时间；其余7页的原始PNG及GIF解码像素一致，未重跑采集或改写结果。[108秒初版](https://github.com/cedRiC874/researchops-agent/blob/9481e60dc9a57c99b766bfad1a36654385348935/docs/media/offline-control-demo.gif)保留在旧提交中，原302611字节、SHA-256 `c03fca921136115c37013a1cd665119e7bd6bc3aaa3bce619168b3ededa8bced`；下述采集摘要与原始证据不变。

公开投影来自本地采集JSON，其SHA-256为`64afa6bbe1b0953ae2d391e5de983db24ed59f532fb3a18f45c6a14f584de5cb`。采集辅助脚本SHA-256为`8c668929a2bee5c38199334730e3f0efc467a668abc2953ead49f0f24f619e38`。原件本地保留；摘要绑定不等于公开了完整私有审计，也不构成第三方独立见证。

## 报告追证的实际范围

| 展示值 | evidence ID | 原始指标路径 |
| --- | --- | --- |
| −5.61 mmHg | E-7C87BB6C88EB | `estimates.contrast.adjusted_mean_difference` |
| −6.79 mmHg | E-B93CD9DC7751 | `estimates.contrast.mean_difference` |

来源是[phase3合成数据证据包](../../../artifacts/phase3/analysis_bundle.json)，原始未舍入值见投影。报告由现有[reporting.py](../../../src/researchops/reporting.py)生成；这次只展示并回核两条效应claim，**不声称报告每个数字都有字段级claim**，不将历史统计产物当作新模型成绩。

**当前真实 Agent 在审批暂停后不能恢复。** 此处批准／恢复只证明脚本驱动的控制面路径。它不证明模型安全、外部用户验收、生产SLA或新线上权限，不改变20/60及原STATUS/T7。
