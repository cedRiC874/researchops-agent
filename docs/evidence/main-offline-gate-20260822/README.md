# Main Offline Gate Audit — 2026-08-22

本目录记录 `main@badb7169fff4d6f1a3a5952ac84032d75c059b01` 的
[`offline-quality-gate` run 32568017243](https://github.com/cedRiC874/researchops-agent/actions/runs/32568017243)
复核结果。

## 结论

GitHub workflow 的技术结论是 `success`，144 项已提交根测试为 `OK`，artifact
完整性、哈希、审计链和敏感内容 verifier 也通过。但该 run 新重建的 Phase 5
质量结果不是历史基线的 50/50，而是：

- 任务成功：44/50（88.0%）；
- evidence citations：10/21（47.62%）；
- 非预期工具错误：0%；
- 安全违规：0%；
- model calls：0；
- harness errors：0。

失败任务为 `AE-001`、`AE-012`、`RE-001`、`RE-002`、`RE-003`、`RE-004`；
共同问题包含 required evidence 未匹配，部分任务还出现 dataset/chart exact 字段漂移。
机器生成摘要的文本内容快照见 [`eval_summary.md`](eval_summary.md)；仓库副本只把
Windows CRLF 换行规范化为 LF，不改变指标文本。

## 已定位根因：CSV 换行与 golden provenance 不一致

`.gitattributes` 将 `data/synthetic_trial.csv` 声明为 `eol=lf`。Git blob 和 clean
GitHub checkout 的文件为 15,247 bytes、241 个 LF、0 个 CRLF，SHA-256：

```text
7ae3c201ccb543b5c647c8c50b2a754294d1d62aaaa458d0f2fb4b0af990ca00
```

当前 Windows 工作树里长期保留的同一 CSV 仍为 15,488 bytes、241 个 CRLF，
SHA-256：

```text
db7ce30ae0fdc9d455edfd6f107f974215aa7fc91209f73a7d1afdf208b9062c
```

Phase 5 corpus 的 golden exact/evidence 绑定了后一个 CRLF hash。相同 corpus
SHA-256 在本地 CRLF 工作树重跑为 50/50、21/21，而 `main` clean LF checkout
重跑为 44/50、10/21；失败的 dataset SHA、evidence ID 与 chart ID 都沿该 provenance
链发生漂移。因此本地 50/50 也是环境绑定结果，不能替代 clean-checkout 质量证明。

## P1：绿色 workflow 不等于质量阈值通过

当前 workflow 会重建评测并运行 artifact verifier，但 verifier 没有对
`success_rate == 1.0`、`evidence_citation_accuracy == 1.0` 等发布阈值 fail-closed。
因此本次 GitHub check 能显示绿色，同时报告内部仍有 6 道失败。这是一个 CI 门禁
缺口，不能把 run 32568017243 表述为“Phase 5 质量通过”或继续用它支撑当前源码
50/50。

## 修复状态

P1 已作为仅含 5 个文件的
[PR #3](https://github.com/cedRiC874/researchops-agent/pull/3) squash 合并至
`main@b3f515a300855caef89efbf1f48859e91b27d925`。上面的旧 run 继续保留为事故
证据；当前 main 状态已恢复：

- `evals/tasks.jsonl` 已把 38 个 Phase 5 provenance 标量更新为 LF lineage：
  dataset `7ae3…`、ANCOVA `E-8EDFAE7ED8F0`、Welch `E-E5D03B8E6EB8`、
  chart `CH-11F349FABC44`；
- 新 corpus 为 30,490 bytes、50 LF、0 CRLF，SHA-256
  `dd591862542be96d1da095d7569d31716ad66025f66e21e45b81e30dce8f8e67`；
- verifier 新增版本化 `phase5-ci-v1`，精确要求 50/50、failed 0、success rate 1、
  evidence 21/21 与 citation accuracy 1；不传 profile 时仍只做历史完整性验证；
- workflow 分别保存 `eval-run` 与 verifier 的 native exit code，任一非零都明确
  `exit 1`，不再让后续命令覆盖失败；
- 正向：当前 LF 工作树与 `origin/main` 独立 LF 快照均重建 50/50、21/21，
  `eval-run=0`、profile verifier=0；
- 负向：修复前 44/50 产物被 profile 稳定拒绝，错误码
  `phase5_quality_threshold_mismatch`，verifier exit 1；
- GitHub branch push run
  [32570797245](https://github.com/cedRiC874/researchops-agent/actions/runs/32570797245)
  在 clean Windows runner 上通过 152/152、50/50、21/21，日志中的
  `phase5-ci-v1.status=valid`，50 条 audit chain 全部有效；
- GitHub PR run
  [32570848599](https://github.com/cedRiC874/researchops-agent/actions/runs/32570848599)
  独立重复得到 50/50、21/21 与 audit chain 全部有效；
- 合并后的 main push run
  [32571384757](https://github.com/cedRiC874/researchops-agent/actions/runs/32571384757)
  通过 152/152、50/50、21/21、`phase5-ci-v1.status=valid`、50 条 audit chain
  全部有效；artifact digest 为
  `sha256:d6cb19a4c1069aa7309f3afc0690024089ee06ef1e9afaaa4931de735d292aa5`；
- 全过程 0 次模型调用、0 次网络调用，未修改 Eval v2 prompt/scorer/tool schema。

机器可读的完整修复验证摘要（含本地、PR 与 main）：
[`p1_local_fix_verification.json`](p1_local_fix_verification.json)。

main 脱敏 summary 的 LF 规范化长期副本：
[`main_fixed_eval_summary.md`](main_fixed_eval_summary.md)。
其 SHA-256 为
`a7546f9a5b60b19143eb3be068beeed276c493cbcd628713307a2311cfb57fa3`。

P1 现已由 clean `main` run 关闭。Phase 5 始终是
`offline_deterministic / components_and_control_plane`，不是 LLM 规划准确率。

## Provenance

- evaluation mode：`offline_deterministic`；
- subject：`components_and_control_plane`；
- source tree SHA-256：
  `ab3d46ee6a316b6da300eb28701e5206ea689d59de643226570ffde09de8ced2`；
- corpus SHA-256：
  `aef47d5136ae1aef090ccf94770f48386691634c028fce7e6251835b94a2eb42`；
- clean checkout dataset SHA-256：
  `7ae3c201ccb543b5c647c8c50b2a754294d1d62aaaa458d0f2fb4b0af990ca00`；
- local CRLF dataset SHA-256：
  `db7ce30ae0fdc9d455edfd6f107f974215aa7fc91209f73a7d1afdf208b9062c`；
- network calls：0；
- LLM planner evaluated：false。
