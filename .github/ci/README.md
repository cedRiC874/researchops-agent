# CI 文档分流规则

这是一层离线CI选择与结果核对，不是模型运行授权，也不改变历史成绩或源码承诺。

## 什么时候运行

`offline-quality-gate`和`devcontainer-offline-demo`仅保留PR、main分支push、手动触发。功能分支push不再重复启动这两套工作流；PR和main均按完整改动集合选择路线。其他已有路径限定工作流保持原样。

- PR：核对实际检出为事件中的head SHA，再比较唯一merge-base与该head之间的全部变化；不把最后一个提交或合成merge SHA当作PR全量diff。
- main push：比较事件中的before与after，实际检出必须与after一致。
- 手动触发：总是完整验证。
- 无法取得可靠比较集合（空/截断diff、缺对象、多个merge-base、新分支等）：保守选择完整验证。
- 检出身份不匹配、记录写入失败或分流自检失败：工作流失败，不能宣称轻量通过。

Git比较使用NUL分隔的完整raw diff，不用可能截断的API文件列表，不启用外部diff或textconv。

## 轻量白名单

只有以下既有文件的普通文本修改可走轻量路线；路径精确匹配、区分大小写，两侧都必须是100644普通文件：

- `README.md`、`README.en.md`、`STATUS.md`
- `docs/PORTFOLIO.md`、`docs/README.md`
- `docs/CODESPACES.md`、`docs/DEMO.md`、`docs/RESEARCHOPS_INTERVIEW_GUIDE.md`

新增、删除、重命名、类型/执行位变化、符号链接、混合改动或任何白名单以外路径均完整验证。**不是忽略所有Markdown或整个docs目录。** 冻结CONTRACT/REVISION、证据、脚本、测试、依赖、工作流及本目录本身都不在白名单。本次分流实现的PR也必须完整验证。

扩充白名单前必须核对该文件是否影响运行、冻结合同或承诺选择集，不能仅凭扩展名判断。

## 轻量检查与最终门禁

分流自检只使用Python标准库和临时Git仓库。轻量文档检查直接读取固定HEAD的Git blobs，检查UTF-8、大小、普通文件类型、本地Markdown链接、README平台约束及STATUS保留标记；不安装项目依赖、不访问外部链接、不运行演示、模型或业务测试。它不证明文案中的历史成绩正确，内容仍需审阅。

完整路线仍执行所有原重任务，原步骤、时限、退出判定和验收阈值不变。文档检查与重任务独立运行；文档失败不会被当作重任务已通过。

每个工作流都有始终执行的汇总job：`offline-validation-result`、`devcontainer-validation-result`。它要求分流与文档检查真实成功；完整路线的每个重任务必须success；轻量路线的重任务必须明确skipped。失败、取消、缺结果、未知输出或不符合所选路线均使汇总失败。轻量成功不能写成根全量通过。

工作流不使用顶层路径过滤来让整个检查消失。[GitHub的必需检查说明](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)指出，路径过滤可能让检查停留在Pending，而条件跳过的job又可能被当作成功。汇总门禁用于区分这些情况。

本补丁不修改仓库规则集或分支保护。若后续配置必需检查，应包含上述汇总job，不能只依赖可条件跳过的旧重任务。此配置变化需要另行确认。

## 本地相关验证

```sh
python -m unittest discover -s tests -p test_ci_document_routing.py -t . -v
python .github/ci/document_scope.py check-docs
```

第二条校验的是已提交HEAD，不把未提交的工作区文档冒充固定版本。测试不会读取真实Key/store或调用Provider。

新规则只对使用这些工作流字节的新运行生效；不会取消、重启或重跑旧CI。已有PR要采用新规则，需经过正常版本接续，不能拿新规则追认旧head已经通过。
