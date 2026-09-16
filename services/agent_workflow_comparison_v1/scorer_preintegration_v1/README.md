# 第6项：固定候选离线预集成（公开候选）

实际评分器对SDK＋FakeModel等合成开发观察的离线预集成验证。
本结果不自动证明上游CI、正式集成或发布验收完成。

评分器固定commit：fcc2026c60943de6016495ad244291689a9d491d；tree：cb4fd9421d6f25da121aefa51bb30244900f0a40。
结构internal-behavior-eval-v1.0，测量修订internal-behavior-eval-v1.1。
评分器核心、契约、锁文件和manifest均从固定Git对象核验，不以PR新head代替评分器版本。

## 在任意待审源码checkout准备固定执行树

需要Python 3.12及Git，且本地仓库已经包含固定评分器commit对象。准备器只使用本地Git，不fetch或发布。
参数中的目标目录必须在源checkout之外、尚不存在；存在时仅允许精确匹配的已准备目录复用。

```sh
python services/agent_workflow_comparison_v1/scorer_preintegration_v1/prepare_ci.py --source-root . --destination ../item6-fixed-runtime
```

程序按publication-files.json白名单核对并带入文件。目标HEAD仍严格等于固定评分器commit，
源checkout可以是另一个PR提交。preparation.json分别记录scorer_commit、tested_source_commit、
源文件哈希以及是否包含未提交变化。未提交变化由字节摘要绑定，不能假称它们已包含在源commit中。
准备器不启动测试；若返回reused，应先查看已有结果和对应进程，不重复启动。

进入目标目录后，使用符合固定requirements.lock（或requirements.linux.lock）的环境运行：

```sh
python -B -X utf8 services/agent_workflow_comparison_v1/scorer_preintegration_v1/verify.py --scope joint --report services/agent_workflow_comparison_v1/scorer_preintegration_v1/outputs/validation-new.json
python -B -X utf8 services/agent_workflow_comparison_v1/scorer_preintegration_v1/run.py --output services/agent_workflow_comparison_v1/scorer_preintegration_v1/outputs/result-new.json
```

输出必须新建。new验证为15项原接入测试＋11项发布边界测试；joint另含33项聚合原型测试，共59项。
评分器及其测试未修改；不调用Provider，不使用真实Key或store。实际运行过程启用原有进程禁网约束。
verify检查当前受测文件稳定性与19个带入文件，不读取另一台机器的工作树；
historical_origin_check明确为not_performed_in_this_context，绝不将未执行的本地历史核验报告为通过。
原57文件保留核验来自已有本地记录，公开carryover仅保留其摘要和来源标识。

## 公开材料与结果边界

publication-files.json列出完整候选白名单，排除自身递归哈希；public-provenance.json记录原件/派生件摘要及转换类别。
原始两份评分计划、两份真实评分报告和故障报告保持字节不变。
validation-history.public.json是修复前48项验证的公开摘要，不证明当前发布修复已通过；新测试报告由上述命令另行生成。
上游及带入元数据移除了个人绝对路径和会话附件引用，保留评分器身份、文件哈希、父子关系及有日期的CI快照。
不会用派生件冒充原始完整验证记录；原件及失败/历史材料继续保留在本地。

两路径各8题；损坏源fail不直接归因为模型能力。固定模板有格式优势，Agent自由表达unknown原文保留。
具体映射和结论边界见ADAPTER.md，拟议CI接入步骤见CI_PLAN.md。
本候选不包含GitHub workflow改动；CI方案仍待主线程审阅，不改变既有根全量门禁。
