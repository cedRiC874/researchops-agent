# 第6项独立CI与最终候选范围

本文件描述主线程已审阅43文件基础上的新增CI层。原README/CI_PLAN中的“尚未包含workflow”是上一批43文件的历史范围；
本批新增本说明及.github/workflows/item6-offline-preintegration.yml，最终候选共45文件。原43文件字节不变。

## 两份清单各自的范围

- publication-files.json仍是已审阅运行白名单：42项摘要加清单自身，共43文件；SHA-256固定为f4f859e3cd264e7144ada8ffad7404fdfcbaa1ef72e7a52e0000f26045bb299e。
- 最终发布白名单在本批交付的final-release-files.json中，共45文件：上述43文件加workflow及本说明。
- workflow和本说明不伪装成评分器源文件，不塞入原运行白名单，也不声称受原源码承诺认证。
- 所有本地证据、原机材料、私有diff、ZIP、交接文件和独立审阅摘要不在这45文件的候选暂存范围内。

## CI行为

新增独立workflow用于pull_request、main上相关路径push及workflow_dispatch；只读contents权限，无Provider或Key/store授权。
显式checkout PR head或事件SHA，不使用浮动分支替代评分器；准备器从该源码head带入运行白名单，目标HEAD始终fcc2026c。
固定对象缺失时准备阶段只尝试取得该完整SHA，失败即停止，不回退main。运行白名单摘要不匹配即停止。
源码checkout中workflow文件的SHA及源码commit另写ci-binding.json，和preparation的43文件摘要共同绑定；
另外记录GitHub实际workflow_definition_commit/ref，不把PR head的文件摘要冒称为GitHub合并上下文中的执行定义。
本地演练没有GitHub执行定义身份，该两字段保持null。

runner为windows-2022，Python设置沿用仓库已有Windows CI版本3.12.10；创建该job专用新venv，
安装固定评分器工作树requirements.lock并pip check，不修改开发者共享环境。
30分钟是含依赖准备的初始外层上限，不是已测托管时延；后续由真实托管记录决定是否调整。

运行59项相关联合测试及一次实际CLI，保留各自实际进程退出码。摘要检查两条路径完整N=8、24故障中的20报告/4拒绝，
版本身份及测试输入前后稳定性；原始unknown、损坏源fail、证据与回答不改写。
失败会传播到job；没有continue-on-error或删除失败场景。已创建证据的阶段失败后仍保留本次尝试指定的7个artifact文件。
准备器返回reused时workflow停止，不能盲目启动第二次运行。

checkout显式关闭persist-credentials、set-safe-directory和clean，不修改全局Git信任或执行清理。
artifact上传限定明确文件名，准备阶段尚未产生输出目录时不会展开为根目录通配符。
上述输入名依据[checkout官方定义](https://raw.githubusercontent.com/actions/checkout/v4/action.yml)和
[upload-artifact官方定义](https://raw.githubusercontent.com/actions/upload-artifact/v4/action.yml)核对。

## 验证与正式状态边界

本地可验证YAML结构、PowerShell语法及执行prepare/tests/cli/summarize中的同一脚本文本；
本地演练使用已安装依赖，记录execution_context=local_shell_rehearsal，不假称执行了GitHub checkout、setup-python、pip安装或artifact上传。
真实托管CI须在明确Git发布授权后触发；CI代码建成不等于托管成功、合并后main通过或正式集成验收。
本批未创建commit、push、PR或merge，未改既有根CI、评分器、标准、锁文件、successor、20/60或T7。
