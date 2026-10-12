# 现场提问（专家试用 v1）

专家在已经准备好的公开数据集上提交一个问题，复用 Eval v2 的工具、规则和输出约束，展示英文和中文回答。**无自动评分**；不读取 golden、不生成通过率，不提供重跑或覆盖选项。

## 当前能否在线运行

本服务直接使用当前仓库的 `EvalV2ProviderExecutor`。当前 main 的构造器拒绝所有未获离线测试授权的调用，错误码为 `eval_v2_completion_telemetry_session_required`；DeepSeek adapter 也要求经过验证的 completion telemetry session。因此，即使提供 `--confirm-online` 和进程环境密钥，当前版本仍会显示“出错”，不会建立真实 Provider 连接。

本服务没有绕过、修改或替换这些限制。它交付可离线验证的现场提问入口；要开展真正的在线专家试用，仍需在独立变更中解决上游执行器的运行准入。本 PR 不授权该上游变更。`--confirm-online` 是用户的数据发送确认，不是上游运行准入凭证。

## 运行环境

不增加第三方依赖，使用主仓库已有 `.venv`。从仓库根目录运行：

```powershell
# 在主仓库时
& .\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "src;services/expert_review_v1/src"
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m researchops_expert_review --help
```

如果在独立 worktree，先切换到 worktree 根目录，再激活主仓库已有环境，例如相邻目录中的：

```powershell
& ..\researchops-agent\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "src;services/expert_review_v1/src"
$env:PYTHONDONTWRITEBYTECODE = "1"
```

下文假设登记表已存在于 `artifacts/prepared/logical_dataset_registry.json`。也可以指向主仓库已有的准备产物登记表；本服务不下载或重新准备数据。示例命令中请替换为实际登记表位置。密钥只在确认、授权检查、格式检查和去重之后，从 `provider.api_key_env`（DeepSeek 为 `DEEPSEEK_API_KEY`）读取到进程内。无密钥参数、输入框或密钥文件加载功能。请由你自己的运行环境事先提供该变量，不要把密钥写入问题文件。

## 创建会话

```powershell
python -m researchops_expert_review session-create --output-dir artifacts/expert-review/session-01
```

目录必须尚不存在，且经过路径解析后位于本仓库 `artifacts/` 的独立子目录。直接复用 self-pilot 会话目录校验。

## 本机预览

```powershell
python -m researchops_expert_review preview --registry artifacts/prepared/logical_dataset_registry.json --dataset-id palmer_penguins_v0_1_0
python -m researchops_expert_review preview --registry artifacts/prepared/logical_dataset_registry.json --dataset-id palmer_penguins_v0_1_0 --local-head 5
```

第一段完整输出 `EvalV2InspectDatasetBackend.inspect_dataset` 返回的 JSON，不重新投影或改变统计信息。`--local-head` 默认 0，最大 5；启用后另起“本机文件前 N 行（只在本机显示，不会发送）”段落，按物理文本行读取，包含 CSV 表头。不输出文件路径；不会调用模型、联网或写入会话记录。前几行可能含公开数据中的受试者代号，仅用于本机检查。

## 提问

将问题以 UTF-8 保存到本机文本文件。建议使用英文问题，以保持现有预分类规则的效果。

```powershell
python -m researchops_expert_review ask --session-dir artifacts/expert-review/session-01 --registry artifacts/prepared/logical_dataset_registry.json --dataset-id palmer_penguins_v0_1_0 --question-file question.txt --provider deepseek --model deepseek-v4-flash --confirm-online

python -m researchops_expert_review ask --session-dir artifacts/expert-review/session-01 --registry artifacts/prepared/logical_dataset_registry.json --from-task V2-DEV-049 --provider deepseek --model deepseek-v4-flash --confirm-online
```

两种问题来源互斥。示例题从 `evals/v2/public_tasks.jsonl` 只读取得英文原文和完整 context，数据集由题目决定，包括 049 的 `bundle_id` 与 `release_name`。每次执行使用随机 `ADHOC-xxxxxxxx` 标识和 `EvalV2ToolGateway`，只授权当前数据集；发布工具仍只能产生待审批提案，不会实际发布。

执行器使用 `bilingual_output=True`、self-pilot Web 的 10000 token 配置，以及 self-pilot 默认的 8 turns 和 120 秒超时。现有规则仍可以收紧具体请求的输出预算或不给模型任何工具；本服务不重写预分类、提示规则、工具选择或输出约束。

本机输入检查限制为 2000 个字符，拦截身份证号、手机号、邮箱、文件路径、`sk-` 密钥和 `SUBJ-` 受试者编号的常见格式。**这只是格式匹配，不是完备检测**：可能误报，也可能漏报，请自行确认问题没有个人信息或敏感内容。命中时提示改写，只保存命中类型、规范化问题的 SHA-256 和运行元数据；不保存问题原文，也不读取密钥或发送请求。

同一会话以问题的 `" ".join(question.split())` 的 UTF-8 SHA-256 去重，跨数据集、来源和进程生效；原始问题仅对通过格式检查的提交保存。确认、数据集验证和格式检查后，先持久化题目占位再读取密钥。失败、拒绝、缺少密钥和中断都不提供重跑功能；并发提交由会话文件锁拒绝。进程被强制终止可能留下锁，会话会保持拒绝运行，以避免误重跑。

## 网页

```powershell
python -m researchops_expert_review web --session-dir artifacts/expert-review/session-01 --registry artifacts/prepared/logical_dataset_registry.json --provider deepseek --model deepseek-v4-flash --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。只监听 `127.0.0.1`；选择登记表数据集，然后选择该数据集的示例题或现场输入问题。必须勾选“我确认会把问题文本和汇总统计发送给 DeepSeek”；后端也独立检查确认值。运行时禁用按钮，下方展示本会话历史，不提供重跑按钮。网页不接收或展示密钥及本机路径。

## 展示和记录

状态为“完成 / 先提问澄清 / 拒绝 / 出错”，使用 self-pilot 原安全过滤器，并在展示边界复用本服务的路径、密钥和受试者编号格式匹配，再由 `split_bilingual_output` 拆分。这补充拦截上游过滤器未覆盖的相对路径、其他目录和非十六进制受试者代号，不改变执行器规则。过滤命中整份回答屏蔽；进程内密钥若意外出现在回答中也整份屏蔽。格式不完整时保留原拆分器的提示，不自行补写中文。

发布示例的执行可以结束，但原始结局保留 `waiting_approval`，展示明确注明“发布仍待审批，未实际发布”。“完成”仅指本次执行结束。

每题一个 `EXPERT-*.json`，另有 `index.json`。记录含 run_id、UTC 时间、数据集、问题及规范化哈希、来源、provider/model、结局、屏蔽状态、实际展示回答、工具调用次数、耗时及 token 用量。用量未知为 `null`，不补零。当前执行器不返回预分类字段，因此该字段为 `null`，不在服务内重新分类。工具调用次数指网关已记录的调用数（包括被拒绝调用）；不保存工具调用正文、模型请求/响应封装、原始输出副本、文件路径或密钥。会话包含允许保存的问题与回答，请按本机试用记录管理。

## 离线验证

测试只用 mock / 离线 runner；测试专用的授权 patch 仅存在于测试代码。运行器在测试进程中拒绝外部 socket 连接和 DNS，仅允许本机 HTTP 测试；不删除、过滤或跳过已有测试。

```powershell
$env:PYTHONPATH = "src;services/expert_review_v1/src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
$env:OPENBLAS_CORETYPE = "NEHALEM"
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"
python services/expert_review_v1/scripts/run_offline_tests.py service --report services/expert_review_v1/evidence/service-tests.json
python services/expert_review_v1/scripts/run_offline_tests.py root --report services/expert_review_v1/evidence/root-tests.json
python services/expert_review_v1/scripts/verify_protected.py --report services/expert_review_v1/evidence/protected-verification.json
```

保护核对使用开发前实际字节的逐文件 SHA-256 基线，覆盖冻结清单列出的每个文件、清单本身和 `evals/v2/` 全部文件；不是假定历史清单中的哈希等同于当前 main。核对脚本及报告位于本服务目录中，且 Git 变更范围应仅包含 `services/expert_review_v1/`。

已知局限：现有预分类规则只认英文措辞；个人信息检查只是格式匹配；无自动评分；当前上游在线准入关闭；强制中断后的会话不能继续；模型未按双语格式回答时不自动重试。
