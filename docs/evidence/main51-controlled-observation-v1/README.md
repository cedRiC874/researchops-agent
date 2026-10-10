# Main51受控观察

观察日期：2026-10-04。16道开发方已知合成题、两条路径，共32条业务观察；不是外部未见集或独立验收。此页是公开候选的白名单汇总，原完整归档本机保留，不含Key、授权包、真实store、本机路径或原始HTTP body。

## 结果

| 路径 | 观察 | pass | fail | unknown | Provider请求 | 工具执行 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 固定流程 | 16 | 16 | 0 | 0 | 0 | 14 |
| Agent | 16 | 13 | 1 | 2 | 30 | 14 |

32条完整收集，未执行0、隔离拒绝0；`all_business_passed=false`。固定流程具有规则、换算和模板优势，顺序与题目类别相关；这是具体实现级观察，不是模型能力排名。

## 失败与无法判定

- IC-04：工具证据0.00625 g，正确为6.25 mg；Agent输出0.00625 mg。facts与evidence失败，属于实质换算错误。
- IC-06：输出“West-East的差值（difference）为+6.4 mg [E1]。”。可见数值与证据一致，但括注超出有限解析；正号本来受支持，正式unknown不改判。
- IC-11：设计缺失，Agent只说“design_requests的数量不是1，本题未取得结果。”。双方零工具，但Agent未建立明确澄清动作，正式unknown保持。

其余13题双方通过。三题执行与响应记录完整，不能归因于截断或未执行。本轮expression全部不适用，不称五维全面通过。

## 遥测与费用

30请求usage完整：输入25624、输出839、总26463 tokens；原生和归一化状态均completed，来源均native_status。212个审计事件链有效，独立原样归档回读通过，502个绑定输入稳定。Python、源码launcher与外层实际exit0。

冻结输入2／输出8元每百万token下保守估算0.057960 CNY，实际账单null；5元是本次独立观测停止线，不是账单硬cap。旧调用不产生新权限。

## 固定身份

- 执行commit：`a877a5a0c6ae1dedc5faec61726d86279aafe749`；tree：`6fa2ec7ddbc2db274c19783e394a8b6b5e41e6db`。
- 固定评分器：`fcc2026c60943de6016495ad244291689a9d491d`，结构1.0／测量1.1。
- source-only v3：`3d9c83b06b7f1ab0fbed6ac722ca1c45faaa66cb08e2561868e9fb37aae65592`；manifest文件摘要：`15b303b6aaf97938bf2b6775fd55c2217edfca8ef03e59208ebfd02bfa9c8637`。
- 业务原归档JSON摘要：`20f4ebc75ff211c00c2d45863f27642516fed42160a7ecca9ad24bce1b2b48a4`。
- 独立回读记录摘要：`709aa01dbce80d64f249a1d4948d65f24561a9266ce6cce98b55106bc832043f`。

本页不是原归档替代品，仅凭公开摘要不能独立重做完整归档验证，不构成外部见证。本次文档整理没有重新评分或调用模型。

## 工程验收

[PR #51](https://github.com/cedRiC874/researchops-agent/pull/51)的固定main保存记录：6个workflow／8个job成功。root执行2535项，2524 passed、0 failures、0 errors、11 skips；S10专项11项和bridge授权重跑455项实际exit0。root原生数值exit未观察／null，UNKNOWN STEP归属限制保留；不是一次零跳过全量通过。

- [根CI](https://github.com/cedRiC874/researchops-agent/actions/runs/37139724528)
- [bridge CI](https://github.com/cedRiC874/researchops-agent/actions/runs/37139724524)：保留原超时与一次授权重跑。
- 工程交付记录摘要：`d4cb267ea333ea1a1307f94b340bb1b6e8aaacdde8c3e08125e77cb385f2bed8`。

这些绿色属于上述固定版本，不是新文档分支已通过CI。Depth-60保持20/60，旧归因、事故及原STATUS/T7不变。
