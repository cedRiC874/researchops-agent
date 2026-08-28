# Eval v2 external review pre-results package v2

> 当前状态：`preparation_only / not_evidence / not_invited / not_run`。本包不包含真人专家、
> 联系方式、利益冲突原件、签名、R/SAS 实现或结果，也不授权 Provider、private access、
> non-synthetic release 或模型质量声明。

## 目的

本包为两类必须由外部人员完成的工作提供冻结、无选择偏差的交付边界：

1. 两位相互独立的领域专家，各自审阅全部 120 道 public task/golden 以及三个数据集边界；
2. 一位与 Python 实现者及两位领域专家身份分离的统计复核人，使用 R 或 SAS 完成独立实现。

内容审阅 scope 与模型 Candidate 解耦。它绑定 public corpus、dataset manifest、campaign、
task schema 和 internal-review bytes，但不绑定或披露 Candidate、Provider、模型输出、成绩、
tokens、cost 或 latency。未来 full-campaign successor 只能引用本包的内容审阅 commitment；
不得把本包反向解释为 Candidate 或 Provider 已通过。

## 使用前门禁

本包只有在以下步骤全部完成后才允许发给 reviewer：

1. 将本包以独立 PR 合并，取得公开 Git commit/tree 和 GitHub 时间锚；
2. 在仓库外冻结 reviewer roster、邀请 ledger、补偿规则、替换规则和截止时间；
3. 由 custodian 使用稳定、domain-separated keyed HMAC 生成去标识 identity commitments；
4. 明确两位领域专家、统计复核人、Python 实现者之间的 cross-role identity separation；
5. 冻结所有受邀者结果的 commitment 规则，包括 declined、withdrawn、rejected 和 excluded，
   不能只发布两份 approved receipt；
6. 冻结严格时间顺序，所有比较必须使用 `<`，不接受时间相等。

公开 Git 历史只能提供包发布时点。真实身份、资格、利益冲突和邀请结果仍需仓库外治理材料及
独立核验，不能由签名字段或模型自行证明。

## T1：领域专家审阅

- Reviewer A 建议覆盖 biostatistics/epidemiology、重复测量和临床分类；
- Reviewer B 建议覆盖 ecology/observational methods 和 applied statistics；
- 两人必须相互独立、彼此盲审，各自审阅全部 120 题，而不是从中选择 50 题；
- 两人收到同一 package commitment；
- 不提供任何 Candidate/Provider 输出、成绩或既有失败诊断；
- 每题记录 dataset/scenario/outcome、tool sequence/arguments、numeric/evidence direction、
  missing/repeated-measure/observational boundary、安全/审批边界和结构化 decision；
- 原始自由文本、姓名、邮箱、CV、资格材料和 conflict declaration 留在 custodian；
- 仓库只允许脱敏 commitments、受邀/完成/拒绝/退出/排除计数、结构化 reason-code 聚合、
  公钥和签名进入后续 versioned receipt。

邀请函见
[`domain_expert_invitation.template.md`](../evals/v2/external_review_pre_results_v2/domain_expert_invitation.template.md)，
逐题工作表见
[`domain_review_worksheet.template.md`](../evals/v2/external_review_pre_results_v2/domain_review_worksheet.template.md)，
外部 roster/邀请 ledger 要求见
[`external_roster_and_invitation_ledger.template.md`](../evals/v2/external_review_pre_results_v2/external_roster_and_invitation_ledger.template.md)。

## T2：独立 R/SAS cross-check

统计复核人只接收 detached allowlist：当前 LF synthetic CSV、design、中立 anchor spec、
tolerance policy、严格空结果 schema、detached manifest、runtime/COI attestation 与公开
package commitments。不得主动附送 README、artifacts、tests、Eval goldens、Python 输出或
`src/researchops`。

CSV、design 和历史参考结果已经公开，因此本包不能证明 input/result blindness。T2 的准确名称是
`non-blinded independent reproducibility cross-check`：独立性来自不同实现者、不同语言、独立
源码/runtime lock 和比较前 output lock，而不是假设公开信息不可访问。Reviewer 必须如实披露
此前是否看过排除材料；不得声称 blinded cross-check。

严格顺序为：

```text
package_anchor
  < reviewer_roster_anchor
  < external_implementation_lock
  < external_execution
  < external_output_lock
  < comparison
  < evidence_binding
  < any_future_model_results
```

R/SAS 脚本与 output 在比较前先由外部时间锚锁定。公开 reference 可能已被 reviewer 看到，
因此不作 blindness 声明；但 output lock 后不得修改脚本、expected、方法或 tolerance。任何差异
先分类并保留原 commitments。修订必须创建新版本，不能覆盖失败版本。

该 cross-check 即使成功，也只能称为 `synthetic_trial statistical-anchor cross-check`；它不会
自动把完整 Eval v2 的 statistical cross-check、full campaign、Provider 2/2 或 private 50
改为完成。

## 当前不可声称

当前仍是：

```text
external expert receipts = 0/2
R/SAS cross-check = not run
full campaign = design_only
registered Providers = 1/2
private holdout = 0/50
non-synthetic release = not authorized
```

不得用内部 review、这套模板、synthetic tests 或未来单次签名替代真人身份/资格/冲突治理；
不得使用已运行公开题、repo-local holdout 或 private 内容调整 prompt、scorer、tool schema、
candidate 或 task selection。
