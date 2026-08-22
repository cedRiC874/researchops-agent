# ResearchOps Agent Supervised Pilot 单场记录模板

> 模板版本：`supervised_pretest_session_record_v1`
> 本模板只记录去标识的操作与技术事实。
> **禁止填写：姓名、邮箱、电话、机构、IP、API Key、Authorization、绝对路径、Provider 原始错误、Agent 答案正文、逐字引语、患者/subject 信息或参与者自己的研究数据。**

## A. 场次标识与证据边界

| 字段 | 填写值 |
| --- | --- |
| `campaign_id` | `EXT-PILOT-________________` |
| `session_instance_id` | `PILOT-RUN-________________________________` |
| `participant_id` | `PX-____________` |
| `moderator_role_id` | `MOD-__`（仅角色代码，不填姓名） |
| `session_date_utc` | `YYYY-MM-DD`（不记录精确开始时间） |
| `protocol_version` | `external-researcher-pilot-protocol-v1` |
| `moderator_guide_version` | `supervised-pilot-moderator-v1` |
| `recruitment_checklist_version` | `supervised-pilot-recruitment-v1` |
| `candidate_commitment_sha256` | `7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11` |
| `deployment_git_sha` | `________________________________________` |
| `task_pack_sha256` | `________________________________________________________________` |
| `consent_document_sha256` | `________________________________________________________________` |
| `provider_id` | `________________` |
| `model_id` | `________________` |

固定声明：

```text
pilot_phase=supervised_pretest
qualifying_external_pilot=false
external_validation_claim_allowed=false
professional_correctness_assessed=false
recording_enabled=false
```

## B. 场次前确认

- [ ] 招募资格已由单独检查表核验。
- [ ] 联系方式、排期和补偿信息未进入本记录或 pilot 数据。
- [ ] 参与者已在首次任务前主动同意。
- [ ] 参与者知道可以跳过或撤回。
- [ ] 参与者知道禁止输入自己的数据、身份信息和 API Key。
- [ ] 未启用录音、录像、录屏或自动会议转写。
- [ ] Provider Key 位于服务端，参与者未看到或输入 Key。
- [ ] 固定 6 题及顺序已由 task pack 在场次前冻结。
- [ ] 主持人已承诺不提示答案、不展示 machine score、不临场换题或重跑。
- [ ] IRB/伦理边界已按协议核验；未自行宣称豁免。

若任一项未满足，记录 `session_outcome=terminated_before_tasks`，不要开始任务。

## C. 任务操作记录

只记录操作状态，不复制 prompt、授权 context 或 Agent 回答。

| 序号 | `task_id` | `dataset_id` | `scenario` | 回答是否显示 | 反馈是否提交 | 状态 | 技术事件 ID |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 |  |  |  | Yes / No | Yes / No | completed / skipped / technical_failure / withdrawn |  |
| 2 |  |  |  | Yes / No | Yes / No | completed / skipped / technical_failure / withdrawn |  |
| 3 |  |  |  | Yes / No | Yes / No | completed / skipped / technical_failure / withdrawn |  |
| 4 |  |  |  | Yes / No | Yes / No | completed / skipped / technical_failure / withdrawn |  |
| 5 |  |  |  | Yes / No | Yes / No | completed / skipped / technical_failure / withdrawn |  |
| 6 |  |  |  | Yes / No | Yes / No | completed / skipped / technical_failure / withdrawn |  |

任何任务均固定：

```text
qualifying_run=false
exclusion_reason=supervised_pretest
run_attempt_count<=1
```

不得为了改善结果重跑；技术失败只记录一次并进入技术事件。

## D. 技术事件

没有技术事件时写 `none`。事件说明只能使用去敏、稳定代码和可见影响，不保存 traceback、路径、Provider error body、请求/响应正文或凭据。

| 事件 ID | 阶段 | 稳定事件代码 | 可见影响 | 严重度 | 是否暂停场次 | 处理状态 |
| --- | --- | --- | --- | --- | --- | --- |
|  | consent / task_load / provider / answer_display / feedback / withdrawal |  |  | low / medium / high | Yes / No | open / resolved / escalated |

疑似凭据/身份信息泄露、越权、审批绕过、错误数据绑定或意外外部写入时，立即停止场次。这里只写独立安全事件 ID，不复制敏感细节：

```text
security_incident_reference=________________ / none
```

## E. 去标识口头观察摘要

只写主持人概括后的操作观察，每条不超过两句话；不写逐字引语、不写答案内容、不写专业正确性判断。

1. `________________________________________________________________________`
2. `________________________________________________________________________`
3. `________________________________________________________________________`

允许记录的主题：按钮或字段是否容易找到、说明是否易懂、在哪一步停顿、是否理解跳过/撤回、是否知道低信心与专家复核的区别。

禁止记录的主题：姓名/单位/研究项目、完整口述、Agent 答案、参与者自己的数据、主持人认为答案是否正确。

## F. 主持偏差与协议偏差

| 字段 | 填写值 |
| --- | --- |
| 主持人是否解释任务或答案 | Yes / No |
| 是否提示反馈选项 | Yes / No |
| 是否展示 machine score/golden | Yes / No |
| 是否临场换题或重跑 | Yes / No |
| 是否修改 prompt/model/schema/scorer | Yes / No |
| 是否发生其他协议偏差 | Yes / No |
| 去标识偏差摘要 | `____________________________________________________________` |

任一项为 Yes 时，本场仍是非达标预试，且必须在后续材料修改前完成偏差复核。不得删除负面或失败记录。

## G. 场次结局与撤回

| 字段 | 填写值 |
| --- | --- |
| `session_outcome` | completed / participant_skipped_tasks / withdrawn / technical_failure / terminated_for_safety / terminated_before_tasks |
| 已完成任务数 | `0 / 1 / 2 / 3 / 4 / 5 / 6` |
| 已提交反馈数 | `0 / 1 / 2 / 3 / 4 / 5 / 6` |
| 总场次分钟数 | `____` |
| 参与者是否撤回 | Yes / No |
| session 是否已撤销 | Yes / No / N/A |
| 新任务是否已阻止 | Yes / No / N/A |
| 删除流程事件 ID | `________________ / N/A` |
| 固定补偿是否按预先规则处理 | Yes / No / N/A（不填支付信息） |

撤回时不记录理由。联系、支付或删除请求的直接身份映射继续保存在独立系统，本模板只引用不含身份内容的事件 ID。

## H. 预试问题分类

- [ ] 招募或资格说明问题
- [ ] 同意或撤回流程问题
- [ ] 页面导航/按钮问题
- [ ] 反馈字段理解问题
- [ ] 计时或状态问题
- [ ] Provider/基础设施问题
- [ ] 安全或隐私问题
- [ ] 主持材料问题
- [ ] 观察到模型行为问题（只登记，不调优锁定 prompt）

建议的 pilot 专用材料改进，不包含 prompt/scorer/tool schema 修改：

```text
______________________________________________________________________________
```

若问题可能要求修改锁定 prompt、scorer、工具 schema 或 `src/researchops`，只登记 issue ID：

```text
locked_candidate_issue_reference=________________ / none
```

当前 supervised cohort 内不得实施该修改。未来如确需修改，必须创建新 candidate commitment 和新 campaign，不能继续调优或覆盖当前锁定证据。

## I. 主持人最终核验

- [ ] 本记录不含姓名、邮箱、机构、IP、API Key、答案正文或逐字引语。
- [ ] 联系与补偿数据仍与 pilot 数据分离。
- [ ] 没有把本场计入正式外部 pilot 的人数、运行数、覆盖或成功率。
- [ ] 没有把参与者可用性意见称为专业正确性。
- [ ] 没有修改或调优锁定 prompt、scorer、工具 schema。
- [ ] 技术、安全和撤回事件均使用去标识事件 ID 关联。
- [ ] 删除/保留期限已按协议安排。

主持角色确认代码：`MOD-__`
记录完成日期（UTC）：`YYYY-MM-DD`
