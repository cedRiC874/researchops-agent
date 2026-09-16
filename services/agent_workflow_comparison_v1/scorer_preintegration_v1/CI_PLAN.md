# 拟议的第6项独立离线CI（待主线程审阅）

本文件是CI需求，不是已启用workflow或CI成功记录。本次不修改.github/workflows、根全量、源码承诺或successor。

建议一个独立Python 3.12离线job，沿用项目固定平台锁文件。依赖安装/checkout属于准备阶段，
评分及验证子进程继续执行现有禁网/禁Provider约束，无Key/store注入。

1. 取待审PR的精确源码head；本地同时具备固定fcc2026c对象，保留完整commit，不使用最新main替代。
2. 从待审源码运行prepare_ci.py，输出到该job独占的固定评分器worktree，核对白名单并记录两个不同版本身份。
3. 在固定执行树安装其锁定依赖；运行verify.py --scope joint，再执行一次run.py CLI；输出使用新文件名。
4. 检查测试实际退出码、完整分母、24个故障预期、身份清单缺项拒绝、公共材料扫描与输入前后哈希。
5. 上传本次job输出作为CI artifact，声明formal_integration_accepted=false；不把修复前48项摘要改称本次测试。

当前联合套件59项（原48项＋11项发布边界测试）；不靠现有根目录unittest discover隐式发现services测试。
新增边界断言逐一删除9个身份必选项，并覆盖空清单、额外项、坏哈希、非固定执行树、非法白名单路径、
重复JSON键、公开材料路径泄露和历史核验的not_performed语义。原评分维度/原因/8题分母断言保留。

本地额外集成验证使用真实的不同Git源head→固定评分器head，记录源代码未提交字节绑定，
不创建假PR或新commit。该记录不能代替实际托管CI。实际job上限应依据托管运行数据设置，
不直接沿用第5项根全量的360分钟预算，也不由本方案授权其它运行。

原工作站保留检查与可移植验证是不同事实：CI不读取原工作站，不生成original_files_unchanged=true；
它只报告本次带入文件与受测输入是否稳定，历史原件保留状态引用既有记录摘要。
Git归属问题不通过全局safe.directory、ACL或信任配置修改解决；使用任务/runner自有工作树。

若主线程调整评分器锚点，先审阅差异并显式更新批准的锚点及绑定；不要在CI中自动跟随分支。
