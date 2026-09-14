# 本地验证与交接记录

日期：2026-09-14。独立分支：`codex/run-detail-ui-v1`。
独立 worktree：`researchops-agent-run-detail-ui-v1`。
HEAD 仍为 `551b9e252670be871ff75b0638b033b07d3c6f08`；
tree 为 `fa18ea712732425a0ed035439a97b93ee08e78ce`。
没有 commit、暂存、push、PR、merge 或发布。

## 文件清单

全部新增于 `services/run_viewer_v1/`，没有修改既有受跟踪文件：

| 文件 | 用途 |
| --- | --- |
| `index.html` | 页面结构、只读交互入口 |
| `style.css` | 原生响应式布局，沿用 Pilot 米白/深绿视觉约定 |
| `app.mjs` | DOM 安全渲染、筛选、分页、证据展开、标识符复制 |
| `view-model.mjs` | 缺失语义、分页及安全 ID 格式 |
| `server.mjs` | Node 标准库 loopback 只读服务、固定路由、Host/Origin 与 CSP 边界 |
| `catalog.mjs` | 历史白名单投影、来源校验、合成失败/部分完成 fixture |
| `data/depth60.public.json` | 已提交公开脱敏摘要的原字节副本 |
| `tests/viewer.test.mjs` | 8 项数据与 HTTP 边界测试 |
| `tests/browser-check.mjs` | 原 7 项浏览器子测试及 1 个父测试；P2 修复另加 7 项，见本批记录 |
| `README.md` | 启动、局部测试、接口及主线程集成说明 |
| `FIELD_MAPPING.md` | 字段来源、类型、缺失语义和脱敏边界 |
| `VERIFICATION.md` | 本记录 |
| `P2_REPAIR_VERIFICATION.json` | 原P2修复的23项测试、注入诊断及9文件前后哈希；保留不覆盖 |
| `package.json` | 仅用于测试的固定Node/Playwright版本与完整联合套件命令 |
| `package-lock.json` | 测试依赖的npm锁文件，不改变根依赖 |

本批另新增 `.github/workflows/run-viewer-v1.yml`；总发布清单为本目录15文件加1个workflow，共16文件。

## 原 v1 的 16 项通过记录（保留，不代表本批覆盖）

以下原记录未覆盖主线程后来发现的两项 P2：首次 `/api/runs` 目录加载期间的筛选变化，
以及未完成复制 Promise 的过期成功/失败回调。旧的“来源快速切换”测试延迟的是详情请求，
旧复制测试只检查已完成的复制，不能替代本批新增回归。

执行 README 中联合测试命令，Node v24.19.0，既有 Playwright + 本机现有 Edge，无新增依赖。

| 指标 | 结果 |
| --- | --- |
| tests | 16（8 个数据/HTTP 测试 + 7 个浏览器子测试 + 1 个父测试） |
| pass | 16 |
| failures | 0 |
| errors | 0 个未捕获执行错误；浏览器 pageerror 数组为 0 |
| skips / cancelled / todo | 0 / 0 / 0 |
| 实际进程退出码 | 0 |
| 用时 | 6347.2848 ms |

Node test runner 没有独立的 `errors` 汇总字段；这里的 errors 说明来自正常退出和浏览器错误捕获，
不伪称为 unittest 输出。没有运行仓库根目录全量回归、冻结锚点检查、Provider 或真实 store。
联合测试后只清理 CSS 文件末尾多余空行、补充本交接文档；未再改行为。

覆盖：历史 60/60 与 20/60 的独立语义、未知账单、合成失败/部分完成、missing/null/unknown/
not_applicable/明确零、原始公开 blob 一致性与错误 hash 拒绝、非法资源/路径/方法/Host/Origin、
10,001 条记录分页、浏览器 101 条事件分页、来源快速切换的旧响应竞争、证据展开、真实剪贴板复制、
390px 手机布局、恶意 HTML/Markdown 文本（测试中移除 CSP 后仍不执行）、零业务写请求及零跨源页面请求。

开发期观察也保留：首次浏览器启动缺少 Playwright 自带浏览器而退出 1；改用既有 Edge。
随后一项状态文本测试因段落空白行断言过紧而失败，改为读取准确状态节点；没有降低语义要求。
审阅另修复切换记录后的旧复制提示、来源快速切换竞态以及缺失状态的原型属性误识别。
最终结果不能替代主线程全量回归、生产鉴权审查或在线/外部验收。

## 原 v1 完整 diff 审阅（历史记录）

对新增文件逐个按 `/dev/null → 文件` 的完整新增 diff 审阅，而非仅查看普通 `git diff`
（未跟踪文件不会出现在普通 diff 中）。本目录所有实现、样例、测试、说明均在审阅范围内。

- 没有共享 API、Provider Adapter、审计 schema、运行入口、冻结标准、锁文件、CI、Docker 或数据库修改。
- 服务只读取固定公开副本和自身页面资产，浏览器不接触任意文件读取入口。
- 历史数据与模拟数据标签、执行与检查分母、时间与费用口径清晰；无真实事件补造。
- 动态内容只经 DOM/textContent；无 eval、innerHTML、Markdown 脚本执行或动态资源加载。
- 事件/问题分页、错误处理、复制标识符及异步选择边界已经过相应验证。
- 既有文件 diff 和暂存 diff 为空。局部 whitespace 检查覆盖全部新增文件；
  `git diff --no-index` 的退出 1 表示存在新增 diff，不作为检查失败；检查诊断及异常退出另行识别。

当时审阅结论：未发现剩余阻断项；主线程后续审阅发现的两项 P2 由下述有界修复处理，
原审阅结论不能证明这两项竞态不存在。
尚未接入真实运行投影或生产鉴权；不声称本版具备该能力。

## 预览与截图

原 v1 专用预览：`http://127.0.0.1:18743/`，PID `22044`，启动于本机时间 2026-09-14 18:42:05。
此前本任务的 PID `45668` 在核对端口归属后已停止，以加载最终静态资产；没有停止其他进程。
原交付时保留预览。本批按授权保持该进程不变，不停止、重启或重复启动。
该服务在启动时缓存资产，因此此既有地址仍是修复前版本，不作为本批修复的验证入口。
本批浏览器测试使用独立临时端口服务，结束后关闭；未更新原截图来冒充新的交互验证。
PID 可能随时间复用；后续管理进程必须先重新核对归属，不能仅凭本记录直接停止。

截图和预览日志置于任务专用 visualization 目录，不属于仓库修改：
`history-desktop.png`、`partial-desktop.png`、`partial-mobile.png`。
已检查桌面及手机截图，内容可读，无明显溢出或遮挡。

## 主线程待办

1. 从这个独立 worktree 接收上述新目录，决定静态挂载和目标环境。
2. 如需真实记录，提供无业务写副作用的只读投影、原有鉴权衔接、字段公开白名单和显式证据关联。
3. 如需显示归档/外部验收通过，提供各自可追溯回执；本页不代为执行验收。
4. 当前 Internal `source_files()` 选择 `src/`、`evals/` 和四个固定根文件，
   不包含 `services/run_viewer_v1/`，所以本次不要求更新该源码承诺。
   选择器定位为 `src/researchops_internal_telemetry/source.py:21`；本轮只读核对代码，未执行承诺生成或校验。
   适用全量回归、CI 纳入和发布由主线程处理；若改变集成范围/选择器，由其按实际规则评估。

## 本批 P2 有界修复

本版是本地可信固定资产预览，不宣称提供不可信文件安全读取或生产鉴权。
保留全部原成果；本批修改 `app.mjs`、`tests/browser-check.mjs`、`README.md`、本记录，
并新增 `P2_REPAIR_VERIFICATION.json` 记录测试前后实现/测试文件身份。

- 目录竞态：初始化及筛选变化共用 `selectVisibleRun()`，目录返回后读取实际筛选条件，
  不再无条件选择 `catalog[0]`。新增测试延迟真实 `/api/runs`，先切到合成，释放目录后核对
  筛选值、列表、唯一选中项、详情及无历史详情请求。
- 复制竞态：点击捕获记录选择代次与复制操作代次；选择开始立即使旧复制失效。
  Promise 成功和失败都先验证两个代次才更新提示/计时器，计时器回调也有同样守卫。
  这不取消系统剪贴板写入，只隔离过期回调的页面副作用。
- 新增六种复制回归：延迟成功/失败分别覆盖切换至另一记录、A→B→A、同页新复制。
  注入替换的是浏览器 `navigator.clipboard.writeText`；断言实际调用值、调用次数和 Promise
  结算索引，监测真实 3500ms 提示计时器的创建/清除次数。旧回调必须不改变任何这些观测。
- 两组回归都检查服务返回的 `/app.mjs` 与磁盘文件原字节相同，不用替代页面或重写 app 源码。

最终联合套件结果、测试前后哈希及稳定性见 `P2_REPAIR_VERIFICATION.json`。
哈希范围仅包括六个实现文件、公开 JSON 副本、两个测试文件；不包含本记录、结果 JSON 或其他文档，
不构造自包含哈希，也不作为项目冻结源码承诺。本批行为源码稳定后仅执行一次联合相关套件。

本批实际运行时间：2026-09-14 11:04:32.051Z 至 11:04:41.490Z。
联合命令为 `node --test --test-reporter=tap services/run_viewer_v1/tests/viewer.test.mjs services/run_viewer_v1/tests/browser-check.mjs`。

| 本批指标 | 实际结果 |
| --- | --- |
| tests / pass / fail / skips | 23 / 23 / 0 / 0 |
| 构成 | 8 个数据/HTTP 测试 + 14 个浏览器子测试 + 1 个父测试 |
| cancelled / todo | 0 / 0 |
| 实际测试进程退出码 | 0 |
| TAP duration_ms | 9353.5194 |
| 实现和测试前后身份 | 9/9 文件 bytes 与 SHA-256 完全相同 |
| 注入命中证据 | 1 条目录延迟 + 6 条复制延迟诊断；全部来自通过的浏览器断言 |

目录注入实际命中 1 次，只有合成失败详情请求；复制注入分别记录 1 或 2 次真实 app 调用，
旧 Promise 的结算索引为 0，新复制存在时按 1→0 结算；过期回调的提示/计时器变化均为 0。
原始 TAP 和 stderr 保存在本任务 visualization 目录的 `p2-repair/`；结果 JSON 保留其 SHA-256，
该 JSON 不包含自身哈希。测试结束后仅补充本段文档与修复 diff，未修改实现或测试。

本批完整修复 diff 已逐文件审阅，包括四个修改文件和新增结果 JSON；
修复前的四文件快照保存在任务 visualization 目录，原成果可恢复。
审阅未发现本批范围内剩余 blocker；这一结论不代表全量通过或主线程集成批准。
既有 PID 22044 预览保留，不展示本次缓存之外的新资产。CI 纳入和进一步集成仍由主线程决定。

20/60、原 STATUS/T7、历史事故记录和主线程监督任务保持不变。

## 主线程专用 CI 批次（原记录保留）

原UI任务已确认没有在写文件或运行测试，本批由主线程统一修改；不管理既有预览。
新增workflow、测试专用package.json及lock；业务实现和已有测试断言不变。
固定Node 24.19.0、Playwright 1.62.1；CI为Ubuntu 24.04与锁定版本对应Chromium。
本地最终验证使用Windows、同版本Playwright和配套Chromium，不使用旧Edge预览。
安装缓存、浏览器、完整TAP和新结果记录全部位于忽略目录 `output/run-viewer-ci-final-20260914-01/`，
不新增第17个发布文件，不覆盖旧16项或P2的23项记录。

配置审阅边界：paths包含查看器目录、workflow自身和固定公开摘要路径；checkout深度0满足固定历史blob；
仅contents:read，persist-credentials:false，无pull_request_target、secrets或写权限；npm ci按锁文件且禁用安装脚本。
浏览器准备使用本地已安装的锁定Playwright CLI，不让npx临时选择其他版本；完整联合套件不筛选、不重试、不skip。
当前业务实现不直接依赖根Python锁、其他服务或真实数据源；没有这些路径的强制Node重测条件。

最终联合套件在配置审阅完毕后仅执行一次，并记录13项实现/数据/测试/配置输入的前后哈希
（原9项、package.json、package-lock.json、workflow及FIELD_MAPPING.md）。最终结果如下；
README/本记录在结果追加时会变化，不冒充受测行为字节，也不把结果JSON纳入自引用哈希。
未commit/push/创建PR；GitHub托管CI仍为待核验，本地结果不能代替。

### 本批最终局部验证（Windows + 配套 Chromium）

开始 UTC `2026-09-14T11:22:47.421Z`，结束 `2026-09-14T11:22:58.749Z`。
通过新package脚本 `npm run test:ci --prefix services/run_viewer_v1` 执行完整联合套件，未筛选、重试或跳过。

| 指标 | 结果 |
| --- | --- |
| tests / pass / fail / skips | **23 / 23 / 0 / 0** |
| cancelled / todo / spawn error | 0 / 0 / null |
| 实际 npm/测试子进程退出码 | **0** |
| 记录器实际退出码 | **0**，主线程另从原工具进程回收确认 |
| TAP duration_ms | 10741.6368 |
| Node / npm / Playwright | 24.19.0 / 11.17.0 / 1.62.1 |
| 本地浏览器 | Windows配套chromium-headless-shell，版本151.0.7922.34，revision1234；由锁定包registry和已安装文件确认，无Edge覆盖 |
| CI浏览器目标 | Ubuntu24.04配套Chromium/headless-shell，同一Playwright版本；尚未实际托管执行 |
| 注入命中诊断 | 7条：目录1条，复制成功/失败×三种切换6条 |
| 前后身份 | 13/13受测输入bytes及SHA-256一致 |
| stderr | 空；SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| stdout SHA-256 | `c8ca9b26baadde4df70226e733c7939c24cf153805e68c38c22d551d09cd0515` |

Node runner没有独立的unittest式errors字段；23项均通过并且浏览器pageerror断言通过，不伪造该字段。
新机器记录及原TAP分别在 `output/run-viewer-ci-final-20260914-01/validation-final.json` 和 `test.stdout.tap`，
均排他创建、不在发布清单中。原P2机器记录SHA仍为
`6137b395d88183946d5e8884fcf9072ac00de8e0d2da67742539fdebff6a2425`；旧16项/23项历史结果不变。
新机器记录为6770 B，SHA-256 `705df1159151b51988e95883a2a7effa68b225fa35077d3e1458b26f5f3d90eb`。

关键当前配置哈希：

| 文件 | SHA-256 |
| --- | --- |
| `.github/workflows/run-viewer-v1.yml` | `1f0e4c0022d06da44bc381c368b2b59c9261e9d460f58f3d4bbcd650b3baa0ee` |
| `package.json` | `dd032514b2536061f72354ab35125c7262841330637c7f890277a48f931bbc09` |
| `package-lock.json` | `07e84e0480b40f51c60803e4322c29b0cfc6aff4f1ba31f09a61958acd0e03f2` |
| `app.mjs` | `e1f3ca1439030dc3d129760e2db84b8b4cf1f31b3e2d343633013238b6838f41` |
| `tests/browser-check.mjs` | `7cfe00e0e5d5dd9e0bc0f74b16cdce9ace43822b3f823da232903fe8778a02dc` |

### 最终完整diff审阅与剩余门禁

复用原13文件中未变化实现/数据/测试的已完成审阅。本批实际只增加workflow、测试package/lock，
修改README与本记录；P2业务修复和所有测试文件原字节未改。包括新增文件在内的最终清单为16项。
已解析YAML并核对paths、contents:read、非pull_request_target、无secrets表达式、历史checkout深度、
精确版本断言、锁文件安装、配套浏览器安装和完整测试命令；没有生产/根依赖/其他服务修改。
无本批未解决的实现blocker；未发现需要额外测试发现逻辑或断言调整，因此没有作这类修改。

安装到ignored output的依赖/浏览器/缓存及机器记录不提交；没有改.gitignore或共享虚拟环境。
既有预览由原任务保留（该任务交接时报告PID34260），本批没有使用、停止或重启它。
未运行本地根全量、更新successor、修改20/60、原STATUS/T7或第5项，也未影响PR44监督。

待用户另行授权后，才能暂存明确16文件、commit/push/创建窄PR。新固定head须完成完整diff复核、
专用Node/browser job及其他实际必需checks；获合并授权后regular merge并核验实际main。
没有发布前，托管CI结果始终是待核验；本地Chromium通过不是Linux托管CI通过。
