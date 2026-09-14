# ResearchOps 只读运行详情页 v1

独立、本地、固定快照的运行查看器。没有实时执行、Provider 调用、生产登录系统或业务写接口。
不导入 ResearchOps 运行器、Provider Adapter、审计数据库或 claim store。
本版信任本地代码与固定资产，适用于本机可信目录预览。路由白名单不等于不可信文件安全读取：
本版没有针对文件替换、符号链接或并发篡改实现安全文件读取器，也不提供生产鉴权。

## 启动

在本功能所在仓库根目录，使用已安装的 Node.js（本次验证 v24.19.0）：

```powershell
node services/run_viewer_v1/server.mjs --port 18743
```

打开 <http://127.0.0.1:18743/>。终端打印本进程 PID；使用 Ctrl+C 停止这个进程。
端口被占用时进程报错退出，不会终止其他服务；可指定另一个空闲的 1024–65535 端口。
固定绑定 `127.0.0.1`，不提供 host 参数。不需要 npm install、共享虚拟环境或数据库。
这是 HTTP 页面，不能用双击 HTML 的 file:// 模式代替启动命令。

## 数据与公开边界

- `depth60-public`：真实公开历史在线批次，固定合成任务，执行 60/60、逐题检查通过 20/60。
- `DEMO-FAILURE-001`：人工合成的失败展示，不是历史事故或在线记录。
- `DEMO-PARTIAL-001`：人工合成的部分完成与暂停展示；聚合 evidence 和工具关联全部是模拟。

历史来源：基线 `551b9e252670be871ff75b0638b033b07d3c6f08` 中的
`docs/evidence/phase6-deepseek-depth60-v1/public_summary.json`。
其 tree 为 `fa18ea712732425a0ed035439a97b93ee08e78ce`。
副本 `data/depth60.public.json` 保留 Git blob 原字节，SHA-256：
`b0a549aac1f42b7a389fcffca91172299897979ed8d2b677b55d941c80a4bb9e`。
启动时只读该固定副本并核对哈希，不一致则拒绝启动，没有替代源。
测试还将副本与固定基线 Git blob 比较；不会跟随 main 或读取未知未提交版本。

来源 commit 是读取基线，摘要内 `repository_anchor` 才是历史执行源码锚点。
公开材料 ID 不等于单次 run ID 或工具 evidence ID。历史摘要没有逐步事件和工具结果关联，
因此页面明确显示未提供，不用架构、请求数或链检查摘要推造历史过程。
公开 hash 验证只验证副本身份，不代表本页回验了原始审计链或取得外部验收。
本目录不改变 20/60、历史事故、根 README/STATUS/T7、冻结标准或源码承诺。

## 只读接口

| GET/HEAD 路由 | 返回 |
| --- | --- |
| `/`、`/app.mjs`、`/view-model.mjs`、`/style.css` | 固定页面资产 |
| `/api/runs` | 三个固定逻辑 ID、标题、数据来源标签 |
| `/api/runs/depth60-public` | 历史批次的白名单展示投影 |
| `/api/runs/DEMO-FAILURE-001` | 合成失败展示投影 |
| `/api/runs/DEMO-PARTIAL-001` | 合成部分完成展示投影 |

不存在通用静态目录、文件路径参数、任意 URL 加载、原始 JSON 下载或动态执行入口。
按 raw URL 精确匹配；未知 ID、查询参数、编码路径、路径遍历均返回 404。
POST/PUT/PATCH/DELETE/OPTIONS 返回 405；不匹配当前 loopback 地址的 Host/Origin 返回 403。
这些限制用于本地预览边界，不构成生产认证。没有 CORS 放行；内容使用同源 CSP、no-store、nosniff。
页面通过 DOM/textContent 渲染文本，不解析来自记录的 HTML、Markdown、图片或链接。
复制操作只允许固定投影中符合格式的安全标识符；不会复制整段模型内容。

字段模型与来源表见 [FIELD_MAPPING.md](FIELD_MAPPING.md)。时间线每页 8 条，问题每页 4 条；
筛选后分页重置，显示匹配数与公开记录总数。服务固定返回三个小快照，前端最多渲染当前页；
未来大量真实记录应由主线程增加稳定快照与服务端分页，不能把本版视为生产规模支持。

## 局部验证与专用 CI

数据语义、历史原字节和 HTTP 边界测试只需 Node 与 Git：

```powershell
node --test services/run_viewer_v1/tests/viewer.test.mjs
```

专用 CI 固定 Node **24.19.0**、Playwright **1.62.1**，用本目录的测试专用
`package.json`/`package-lock.json` 执行 `npm ci --ignore-scripts --no-audit --no-fund`。
它在 Ubuntu 24.04 runner 上安装该锁定 Playwright 对应的 Chromium，再执行完整联合套件。
没有浏览器或缺少历史 Git 对象会失败，不 skip；不提供仓库 secrets 或 Provider 配置。
应用本身仍只用 Node 标准库，新增包仅供测试。

本地可将锁文件安装到已忽略的 `output/` 新目录，避免 node_modules 混入发布清单。
以下命令在仓库根目录执行；选择尚不存在的测试目录，不覆盖旧验证材料：

```powershell
$deps = Join-Path (Get-Location) 'output/run-viewer-local-test-deps'
New-Item -ItemType Directory -Path $deps -ErrorAction Stop
Copy-Item services/run_viewer_v1/package.json, services/run_viewer_v1/package-lock.json $deps
npm ci --prefix $deps --ignore-scripts --no-audit --no-fund
$env:PLAYWRIGHT_MODULE = Join-Path $deps 'node_modules/playwright'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $deps 'browsers'
Remove-Item Env:RUN_VIEWER_BROWSER -ErrorAction SilentlyContinue
node (Join-Path $env:PLAYWRIGHT_MODULE 'cli.js') install chromium
```

安装完成后，完整联合命令不筛选用例，包含原8项数据/HTTP检查与14项浏览器子测试：

```powershell
node --test services/run_viewer_v1/tests/viewer.test.mjs services/run_viewer_v1/tests/browser-check.mjs
```

默认解析已安装的 `playwright` 包及其配套浏览器。`PLAYWRIGHT_MODULE` 仅用于本地隔离依赖位置；
专用 CI 清空该覆盖值和 `RUN_VIEWER_BROWSER`，只能使用锁文件安装的包与匹配 Chromium。
以前的 P2 验证使用既有 Edge，其23项记录保留，不冒充此 Chromium/CI 配置的结果。
不应为了启动只读应用安装浏览器；上述安装只属于显式授权的测试准备。
可选设置 `RUN_VIEWER_SCREENSHOT_DIR` 到本任务获准的截图目录。
测试仅启动自己的 loopback 临时端口服务、独立无头浏览器，完成后关闭；不调用 Provider。

`.github/workflows/run-viewer-v1.yml` 的路径过滤覆盖本目录、workflow自身及固定公开摘要的仓库路径。
测试不依赖 Python运行器、根requirements或其他服务代码。历史副本比较通过固定commit读取Git blob，
因此该workflow使用 `fetch-depth: 0`，不是只checkout当前树。现有通用CI绿色不能代替本专用Node/browser job。
本地验证不代表已执行GitHub托管CI；未发布时托管结果仍为待核验。

## 主线程集成事项

本版完整实现本地公开样例查看，不声称接入实时运行。
生产挂载、鉴权、run 可见性、无业务写副作用的数据投影、事件公开白名单、显式
claim/evidence/tool 关联、归档与外部验收回执、新鲜度和服务端分页均由主线程决定。
现有 Pilot GET 状态会调用可能创建 attempt 的 `current_attempt()`，故本版不复用该接口。
Production Slice GET 保留原鉴权及数据检查 profile 用途，本版没有绕过或修改它。

固定基线的 `src/researchops_internal_telemetry/source.py::source_files()` 只枚举
`src/`、`evals/` 下指定扩展名，以及 `pyproject.toml`、两个 requirements 锁文件、
`probe_out_v3.json`；本功能的 `services/run_viewer_v1/` 不在该选择范围内。
因此本目录新增或本次修复不要求更新当前 Internal 源码承诺。Git tree 身份与该选择器的
源码承诺是不同概念；若以后改变集成范围或选择器，应由主线程按实际选择规则另行评估。
本次仅阅读选择器代码，未执行或修改源码承诺、历史哈希、successor 或冻结标准。
本批仅新增专用Node CI与测试依赖锁；不运行本地根目录全量、不改变源承诺。
发布仍需主线程另行授权，托管CI尚待新固定提交实际触发后核验。
