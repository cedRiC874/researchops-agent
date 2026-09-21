# 第6项离线发布接续 v1

本目录是第一批源码接续与公开派生材料准备；无共享权限/遥测实现，无Git发布授权。原工作树及82个候选/历史文件保留。实际评分器固定FCC，结构v1.0、测量v1.1；本批不扩展评分标准。

使用Python 3.12及现有requirements.lock依赖，在仓库根目录执行一次：

```powershell
python -B -X utf8 services/agent_workflow_comparison_v1/controlled_publication_v1/joint.py --run-id LOCAL-NEW
```

已提交源码由CI使用 `--require-committed`：核验实际HEAD为历史基线dfb568d的后继、全部闭包与manifest均匹配该commit，逐一核验本树评分器与FCC blob相同。未提交的本地开发副本仍允许离线测试，但execution_commit=null且不能作为正式固定版本。没有凭环境变量跳过身份验证、源码漂移或网络禁止的入口。

runtime-manifest.json是事先冻结的源码、测试、数据、协议与新CI闭包；不能在验证过程中自动刷新。评分合同单独进入事后评分，路径运行只读取其摘要。manifest自身由运行快照和提交绑定，不自我哈希。

联合套件为26项模型无关、51项原生Responses和12项发布边界检查，共89项；其中6项为本轮审阅修复新增。保留全部业务断言，新增无历史输出的干净文件物化、变更/缺失/额外源码拒绝、路径穿越、未提交/无关提交拒绝和脱敏映射测试；另覆盖开始前拒绝、CI全输入路径覆盖及无末尾换行的diff边界。模拟Git身份测试只证明校验分支，不冒充真实新commit；真正干净提交检出的验证由未来CI完成。

输出在本目录outputs/<新run-id>/。原始validation/observations/fault-validation及封存记录留在本地；public为显式派生文件，映射记录原始/派生SHA-256。路径和测试隐私哨兵被脱敏；若脱敏会改写final_output则拒绝派生。null、空串、空白、未知、未执行、失败与固定分母不改变。CLI退出0只说明工程套件通过，不是正式Agent成绩。

两条路径各16条内部已知任务，共32条观察；模板的解析优势及自由表达unknown单独披露。工具故障不归因为模型能力。使用真实SDK与真实FCC，但模型响应、usage/cache和价格是合成fixture，实际Provider调用为0，真实API费用及真实人工业务复核时间不可用。

新增 .github/workflows/item6-controlled-comparison.yml，仅使用现有锁文件安装到隔离CI环境，不改锁、共享虚拟环境、旧59项工作流或根CI。依赖下载发生在离线测试进程启动前；测试进程禁止socket/DNS/子进程。未来push/PR还会自动触发仓库既有根CI，本轮未触发，发布授权需明确涵盖。

权限/遥测接桥只见 BRIDGE_CONTRACT_DRAFT.md；source_manifest不授予线上权限。文件清单与来源派生表在public中；本地完整diff及历史保全记录在outputs中，不直接全量发布。
