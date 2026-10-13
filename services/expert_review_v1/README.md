# 现场提问（专家试用 v1）

专家在已经准备好的公开数据集上提交一个问题，复用 Eval v2 的工具、规则和输出约束，展示英文和中文回答。**无自动评分**；不读取 golden、不生成通过率，不提供重跑或覆盖选项。

## 当前能否在线运行

本服务为每题建立经过验证的完成遥测会话，然后交给 `EvalV2ProviderExecutor` 和 DeepSeek adapter。仅支持 `deepseek-v4-flash`、`deepseek-v4-pro` 的 `responses / openai_compatible_responses` 路径。在线运行要求代码已提交，`src`、`evals`、`services/expert_review_v1/src` 没有未提交改动或未跟踪文件；三个运行包必须从当前仓库的源码目录加载，旧安装副本会被拒绝。来源和 Git 检查没有跳过选项。

`--confirm-online` 仍只是用户的数据发送确认。通过原有授权、格式检查和去重后，先执行只读环境预检，再占用问题。预检失败不写记录、不创建会话锁、不读取 Key，修正环境后仍可提交同一问题。占位后的准备流程会重复相同检查，完成专用 `eval_v2_interactive_v1` 绑定、单题计划承诺和审计账本写入后才读取进程密钥。只有遥测闭合、执行器报告 `complete` 且 case 未失败时才展示正常回答；截断、取消、请求失败和账本写入失败均为受控失败，不重试。

这些是开发者本机交互试用记录，**不是评测证据**，不属于 `campaign_runtime`，不关闭 T6-B/T7，也不影响 Depth-60。此变更仅经过离线验证，尚未声称真实在线验证成功。

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

下文假设登记表已存在于 `artifacts/prepared/logical_dataset_registry.json`。也可以指向主仓库已有的准备产物登记表；本服务不下载或重新准备数据。示例命令中请替换为实际登记表位置。密钥仅在确认、授权检查、格式检查、去重、源码与干净提交检查、计划登记和账本创建成功之后，从进程环境的 `DEEPSEEK_API_KEY` 读取。无密钥参数、输入框或密钥文件加载功能。请由你自己的运行环境事先提供该变量，不要把密钥写入问题文件。先提交所需源码改动，或将不需要的改动恢复；不要通过忽略检查处理未提交文件。

## 创建会话

先从仓库根目录进行只读预检；`--expert-review` 会同时检查现场提问包的来源：

```powershell
python -B -m researchops.eval_v2_interactive_telemetry preflight --expert-review
```

预检输出当前提交号，不联网、不读取 Key、不创建文件或账本。它不代替实际运行前的重复检查，也不签发执行授权。

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

服务的路径规则用于识别以下明确路径形态：

- Windows 盘符路径（`C:\`、`C:/`）、UNC 路径（`\\server\share\`）和 `file://`。
- 以 `/` 开头、至少包含两段 ASCII 路径名的绝对路径，如 `/home/example`、`/opt/private`、`/opt/data.csv`。开头斜杠前紧邻英文字母或数字时不按此规则匹配；前面是中文时仍可识别，例如“文件在/opt/data.csv”。
- `~/` 或 `~\` 开头的家目录路径，以及 `./`、`../` 开头的相对路径。
- 小写的 `artifacts`、`data`、`evals`、`src`、`services`、`output`、`docs`、`tests`、`scripts` 后接 `/` 或 `\`，并且还包含一个后续路径分隔符，例如 `data/private/notes`。只有一个分隔符时需符合下述文件扩展名规则，例如 `data/file.csv`；`data/design`、`Data/Methods` 这类普通文字不按目录前缀拦截。
- 小写的 `.venv`、`.git` 后接 `/` 或 `\`，仍按单段路径识别，例如 `.git/config`。
- 包含路径分隔符、并以受控文件扩展名结尾的相对路径；扩展名限 `csv`、`tsv`、`json`、`jsonl`、`parquet`、`sqlite`、`sqlite3`、`db`、`xlsx`、`xls`、`txt`、`md`、`py`、`ps1`、`yml`、`yaml`、`toml`、`log`、`zip`、`data`。

普通单位或组别中的斜杠不再仅因其形态被服务的路径规则判为路径，例如 `ml/kg/min` 和“治疗组/对照组/安慰剂组”。这一调整不代表内容已通过完整隐私审查，其余格式检查仍按原规则执行。

同一会话以问题的 `" ".join(question.split())` 的 UTF-8 SHA-256 去重，跨数据集、来源和进程生效；问题原文不写入会话 JSON。确认、数据集验证、格式与去重检查、只读环境预检之后，才持久化题目占位；锁内再次去重，准备阶段再次核对环境，再登记计划并读取密钥。占位前的环境拒绝不消耗题目；占位后的失败、拒绝、缺少密钥和中断不提供重跑功能。每题 case ID 和账本 run_id 均不复用。并发提交由会话文件锁拒绝。进程被强制终止可能留下锁，会话会保持拒绝运行；未终结的调用可由只读核对命令发现。

## 网页

```powershell
python -m researchops_expert_review web --session-dir artifacts/expert-review/session-01 --registry artifacts/prepared/logical_dataset_registry.json --provider deepseek --model deepseek-v4-flash --port 8765
```

启动时会先执行相同只读环境预检；不通过时输出中文原因并非零退出，不构造或启动服务器。通过后浏览器打开 `http://127.0.0.1:8765`。只监听 `127.0.0.1`；选择登记表数据集，然后选择该数据集的示例题或现场输入问题。必须勾选“我确认会把问题文本和汇总统计发送给 DeepSeek”；后端也独立检查确认值。运行时禁用按钮，下方展示本会话历史，不提供重跑按钮。网页不接收或展示密钥及本机路径。

## 展示和记录

状态为“完成 / 先提问澄清 / 拒绝 / 出错”，使用 self-pilot 原安全过滤器，并在展示边界复用本服务的路径、密钥和受试者编号格式匹配，再由 `split_bilingual_output` 拆分。这补充拦截上游过滤器未覆盖的相对路径、其他目录和非十六进制受试者代号，不改变执行器规则。过滤命中整份回答屏蔽；进程内密钥若意外出现在回答中也整份屏蔽。格式不完整时保留原拆分器的提示，不自行补写中文。

新记录通过 `output_redaction_types` 保存空列表或命中类别，类别只允许 `file_path`、`api_key`、`subject_id`、`self_pilot_filter`；不保存命中的原始片段。CLI 和网页在回答被屏蔽时显示固定中文原因说明，不回显路径、密钥、受试者编号或未知类别值。旧记录没有类别字段时沿用通用屏蔽说明，未屏蔽记录的网页字段保持兼容。

已知边界：`https://` 中的 `s:/` 仍会命中上游旧 self-pilot 盘符模式，导致整份回答被屏蔽。本次不修改 `src/` 中的旧过滤器，也不将 URL 作为绕过过滤的例外。屏蔽说明用于解释本机展示行为，交互记录仍不是评测证据。

发布示例的执行可以结束，但原始结局保留 `waiting_approval`，展示明确注明“发布仍待审批，未实际发布”。“完成”仅指本次执行结束。

每题一个 `EXPERT-*.json`，另有 `index.json`；新索引版本为 `expert-review/1.1`，仍可读取 `1.0`。记录含题目标识、UTC 时间、数据集、规范化问题哈希、来源、provider/model、结局、屏蔽状态、实际展示回答、工具调用次数和耗时。新增完成遥测摘要包括模型尝试次数、各次终态、token 用量合计、是否全部终结、是否写入失败、计划承诺哈希、Git 提交号和遥测模块版本。用量未知为 `null`，不补零。CLI 和网页在回答旁展示模型调用次数、是否完整及 token 数。

审计账本为每题独立运行登记计划承诺、哈希、必要元数据及调用开始/终态事件，不记录问题原文、本机路径、密钥、模型请求/响应正文或工具调用正文。会话 JSON 同样不保存问题原文和上述敏感信息；实际展示回答仍按原有安全过滤规则处理。当前执行器不返回预分类字段，因此该字段为 `null`，不在服务内重新分类。工具调用次数指网关已记录的调用数（包括被拒绝调用）。

## 只读核对账本

```powershell
python -m researchops.eval_v2_interactive_telemetry verify --session-dir artifacts/expert-review/session-01
```

此命令不联网、不读取密钥，self-pilot 会话目录也可使用。它重放账本闭合检查，逐题输出尝试次数、终态、用量、是否全部终结、承诺哈希和提交号，不输出回答正文、问题原文或路径。`ledger_intact` 表示链、收据、文件哈希、封存事件、身份和持久化证据全部校验通过；`case_closed` 表示遥测闭合且全部终结，和题目是否成功分别报告。完整记录的截断响应仍是题目失败。

退出码 `0` 表示全部账本完整、闭合且题目成功；`1` 表示账本都完整，但有题未闭合或失败；`2` 表示至少一个账本无法读取或完整性校验不通过（空目录也不能证明完整，返回 `2`）。进程中断而没有收据也返回 `2`：没有收据就无法证明账本没有被截断。

`integrity_reason` 只输出固定值：`ok`、`receipt_missing_unsealed`（无封存事件且缺收据）、`receipt_missing_sealed`（有封存事件但缺收据）、`chain_invalid`、`hash_mismatch`、`unreadable`、`other_integrity_failure`。不会回显损坏账本中的任意文本。模块版本 `1.1` 仍可只读核对 `1.0` 收据。

## 离线验证

测试只用 mock / 离线 runner；测试专用的授权 patch 仅存在于测试代码。运行器在测试进程中拒绝外部 socket 连接和 DNS，仅允许本机 HTTP 测试；不删除、过滤或跳过已有测试。

```powershell
$env:PYTHONPATH = "src;services/expert_review_v1/src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:OPENBLAS_CORETYPE = "NEHALEM"
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"
New-Item -ItemType Directory -Force artifacts/interactive-telemetry-validation | Out-Null
python services/expert_review_v1/scripts/run_offline_tests.py service --report artifacts/interactive-telemetry-validation/service-tests.json
python services/expert_review_v1/scripts/run_offline_tests.py root --report artifacts/interactive-telemetry-validation/root-tests.json
```

`evidence/` 中已有文件以及 `verify_protected.py` 使用的基线是 PR #59 的历史证据，仍绑定各自提交，没有被改写。本次遥测改动的验证结果另存新文件，不能用旧基线证明当前实现。

已知局限：现有预分类规则只认英文措辞；个人信息检查只是格式匹配；无自动评分；仅支持指定 DeepSeek 路径；强制中断后的会话不能继续；模型未按双语格式回答时不自动重试。

## PR #59 历史验证结果与文件清单

实现提交 `a45d1b6090f9d1d7979c206925a4bd3cafbb6881` 的新增服务测试在主仓库 `.venv` 中 **37/37 通过**。三个已有准备数据集的 preview 均与原后端返回值完全一致。冻结清单列出 499 个文件，连同清单本身和全部 `evals/v2` 文件去重后，**508/508** 开发前后原始字节 SHA-256 一致。

[GitHub Windows 全量测试](https://github.com/cedRiC874/researchops-agent/actions/runs/38244631776/job/114795467863)执行原命令 `python -m unittest discover -s tests -t . -v`：2682 项，2671 通过、11 跳过、0 失败、0 错误（6983.728 秒）。11 个跳过均来自仓库既有条件，未改测试或跳过规则；其中 10 项由既有的[标准版本重放任务](https://github.com/cedRiC874/researchops-agent/actions/runs/38244631776/job/114795468141)覆盖，该任务 11/11 通过，剩余 1 项因 CI 不提供对应原始产物而保持原跳过条件。准确数量、跳过原因和对应提交记录在 `evidence/ci-root-tests.json`。

本机全量不能宣称通过：首轮退出且没有最终汇总，保留为未完成；第二轮在发现测试时，导入 pandas 卡在 Windows WMI 系统信息查询。独立诊断复现相同调用栈，WMI 查询也超时，已停止本服务启动的阻塞进程，未修改或重启系统服务。后续恢复本机 WMI 后才能重新完成本机全量。一次原样单模块复现 `tests.test_campaign_prerequisites_v5_full` 通过，不替代首轮未完成的全量结果。此限制与 CI 成功分别记录，不混称为本机成功。

所有 18 个新增文件均位于 `services/expert_review_v1/`：

- 源码：`src/researchops_expert_review/__init__.py`、`__main__.py`、`core.py`、`web.py`。
- 测试：`tests/test_cli.py`、`test_core.py`、`test_web.py`。
- 验证脚本：`scripts/run_offline_tests.py`、`verify_protected.py`。
- 文档与本目录忽略规则：`README.md`、`.gitignore`。
- 证据：`evidence/service-tests.json`、`prepared-preview.json`、`protected_baseline.json`、`protected-verification.json`、`ci-root-tests.json`、`root-tests-first-attempt.json`、`root-tests-local.json`。
