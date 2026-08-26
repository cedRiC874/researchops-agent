# Anthropic Models API zero-generation-token preflight contract

> Status: `implemented_offline_tested_not_run`
> Version: `anthropic-models-preflight/1.0`
> Reviewed against Anthropic official API documentation: 2026-08-25
> Online requests performed: `0`

## Purpose

在任何 Anthropic Messages/tool pilot 之前，用一次独立、只读的 Models API metadata 请求
回答两个窄问题：

1. 当前 Key 能否通过 Anthropic direct API 的认证；
2. 受控 allowlist 中的 exact model ID 是否对该 Key 可见。

该预检不调用 Messages、Completions、Token Counting 或 LiteLLM，不产生模型输出 token，
也不证明 Messages API、tool calling、usage/error semantics、余额、成本、延迟或模型质量。
它仍是一次联网认证请求，不能称为 offline 或免费。

本合同现已由 `src/researchops/anthropic_preflight.py`、严格机器快照
`evals/v2/anthropic_models_preflight_contract.json` 与无网络 tests 实现。successor candidate
v4 绑定新源码且不修改、不继承 v3；Anthropic Web/public runner/controlled pilot 仍未启用，
本次实现没有读取 Key 或执行真实请求。

## Official API basis

Anthropic 官方文档定义：

- `GET /v1/models` 列出当前 API 可用模型；
- `GET /v1/models/{model_id}` 返回单个 model metadata，也可解析 alias；
- direct API 使用 `x-api-key` 与 `anthropic-version` headers；
- API errors 具有稳定 HTTP 类别，所有响应带 `request-id` header；官方 Python SDK 默认对
  connection errors、408、409、429 与 5xx 等 transient failures 重试 2 次，因此本设计
  不采用默认重试行为。

参考：

- [List Models](https://platform.claude.com/docs/en/api/models/list)
- [Get a Model](https://platform.claude.com/docs/en/api/models/retrieve)
- [Claude API errors](https://platform.claude.com/docs/en/api/errors)
- [Python SDK retries](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)

## Fixed request contract

实现必须生成恰好一个请求：

```text
method=GET
origin=https://api.anthropic.com
path=/v1/models/{percent-encoded exact model_id}
body=absent
anthropic-version=2023-06-01
x-api-key=<memory-only secret>
accept=application/json
accept-encoding=identity
user-agent=researchops-agent/0.2.0 anthropic-models-preflight/1.0
content-type=absent
authorization=absent
anthropic-beta=absent
```

`content-type` 采用 Get a Model endpoint 的官方 bodyless GET cURL 示例而省略；若 Anthropic
未来把它改为该 endpoint 的强制 header，必须更新版本化合同与 fixtures，不能运行时猜测。

上面列出的是应用层必须显式控制的 required/forbidden headers；`Host`、连接管理等由
HTTP transport 正常生成的标准 headers 可以存在。测试不得错误地要求实际 header 集合只
等于上表，但必须逐项验证 required 值和 forbidden headers 不存在。

约束：

- `provider_id` 必须精确为 `anthropic`；
- `model_id` 必须先通过 `AnthropicProvider.validate_model()`，再要求原始输入与返回的
  normalized ID 完全相等；现有 validator 会 `strip()`，所以这一步额外拒绝前后空白。
  只接受当前 exact allowlist，不接受任意字符串、alias 或调用方提供的 URL；
- base URL、API version、path prefix 和 request method 不可由 CLI、Web、环境变量或
  candidate 覆盖；
- response `id` 必须与请求的 exact model ID 完全相等；即使 API 能解析 alias，也不能用
  alias resolution 放宽本地 identity；
- 不发送 `anthropic-beta`、prompt、tools、dataset、participant、candidate task 或其他
  application data；
- 不自动分页，因为 single-model retrieve 不需要 catalog scan；
- 不 follow redirect；任何 3xx 都 fail closed；
- TLS certificate verification 必须使用固定 `certifi==2026.7.22` CA bundle；直接构造
  `SSLContext(PROTOCOL_TLS_CLIENT)`，不得调用会读取 `SSLKEYLOGFILE` 的 convenience factory，
  且 `keylog_filename` 必须保持 null。不得允许任意 CA disable 开关或 TLS session-key
  持久化；
- 默认不继承 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`.netrc` 等环境网络配置
  (`trust_env=false`)；如未来确需企业代理，必须另建显式 allowlisted contract；
- client event hooks、第三方 tracing/callbacks 与 HTTP debug/header logging 必须为空或关闭；
  `httpx`/`httpcore` effective DEBUG 开启时在读取 Key 前 fail closed；
- 客户端由每次预检独占并在成功、失败或取消后关闭；
- `http_attempts=1`、`provider_managed_retries=0`、fallback/alias/routing 均为 0/false；
- response 必须 streamed/bounded read，硬上限 64 KiB；请求固定
  `Accept-Encoding: identity`，响应只接受 absent/`identity` Content-Encoding。任何压缩
  encoding 在读取 body 前拒绝，避免解压 bomb；identity bytes 逐 chunk 计数，在读到第
  65,537 byte 时立即 abort/close，且不能只信任 `Content-Length`；超限、非 JSON 或 schema
  异常均 fail closed；
- connect/read/write/pool timeouts 分别为 5/10/5/5 秒，request-total deadline 为 15 秒；
  client/transport cleanup 另有 5 秒 close deadline。后续版本可进一步收紧，但不得放宽到
  无界等待。

直接使用仓库已锁定的 `httpx==0.28.1`、`httpcore==1.0.9`、`h11==0.16.0` 与固定
`certifi==2026.7.22`，不经过 LiteLLM。这样可以避免为一次 metadata
请求加载 LiteLLM process-global state，也避免引入 Anthropic SDK 的默认自动重试。未来若
改用官方 SDK，必须显式 `max_retries=0` 并重新验证等价的 request/retention 合同。

## Invocation and Key handling

已实现 CLI：

```text
python -m researchops.cli anthropic-models-preflight \
  --model claude-sonnet-5 \
  --confirm-online
```

省略 `--confirm-online` 时只返回安全 `not_run` receipt，不读取 Key 或联网。

调用顺序必须为：

1. 验证 `--confirm-online`；未确认时 `network_calls=0`，且不读取 Key；
2. 本地验证 provider、exact model、timeout 与 dependency pin；
3. 才从 `ANTHROPIC_API_KEY` 读取值，并在 client 前要求 1–512 个 visible ASCII characters、
   无空白/control/CRLF，避免 header exception/log 反射；
4. 发出一次固定 Models API 请求；
5. 在 `finally` 中关闭 client，并 best-effort 丢弃本地 Key 引用。

“清除引用”只缩短 secret 生命周期；Python 进程不能证明底层缓冲区已安全归零，因此本设计
不声称内存取证级 zeroization。实现应避免复制 Key，并让持有范围尽可能短；若 Key 来自
`os.environ`，环境中的原值仍可能存在，preflight 不负责删除调用方环境。

不得：

- 接受命令行 Key、URL、header 或 request body；
- 打印、持久化、做 unkeyed/stable digest、截断展示或放入 exception/repr 的 Key；
- 把 Key、Authorization、原始 response/error body、raw request ID 写入 artifacts、审计、
  tracing 或第三方 callbacks；
- 仅凭历史 receipt 跳过新 pilot 前的同进程预检。

上述 standalone CLI 只产生诊断 receipt，进程退出后绝不能解锁 pilot。未来受控 pilot
必须使用另一个专用 orchestration command，在同一进程内先完成 preflight，再以同一个
single-use、带 expiry、不可序列化的 credential capsule 打开 pilot transport。需要绑定两个
不同但固定的 tuple：

```text
preflight=(anthropic, anthropic_models_retrieve, api.anthropic.com,
           anthropic-version=2023-06-01, exact_model_id)
pilot=(anthropic, litellm_anthropic_chat_completions, api.anthropic.com,
       exact_model_id, openai-agents version, litellm version)
```

可以使用 process-random keyed HMAC 在内存中核对 capsule equality/expiry；不得持久化该
digest，也不得用 standalone receipt 替代 capsule。持久化 receipt 只供审计，始终
`authorizes_model_run=false`。

## Success contract

只有以下条件全部满足才返回 `verified`：

- HTTP status 为 200；
- response 是 bounded JSON object；
- `type == "model"`；
- `id == requested_model_id`，区分大小写且不接受 alias；
- `created_at` 是可解析的 RFC 3339 datetime string，`display_name` 是非空 string；
- `capabilities` 是 object 或 null，`max_input_tokens` / `max_tokens` 是非负 integer 或 null；
- 官方未来新增的未知顶层字段可忽略但不得持久化；上述当前 documented fields 缺失或类型
  错误仍 fail closed；
- client 已关闭；
- 没有第二次 request、redirect、retry、fallback 或 model endpoint call。

成功只允许推出：

```text
models_api_authenticated=true
exact_model_visible=true
network_calls=1
http_attempts=1
model_token_calls=0
messages_api_verified=false
tool_calling_verified=false
usage_semantics_verified=false
model_quality_claim_allowed=false
authorizes_model_run=false
```

`token_usage` 与 `cost` 必须为 `null / unavailable`，不能写成零。`model_token_calls=0`
描述的是没有调用推理 endpoint，不是账单或账户侧总活动声明。

## Stable result schema

实现 MUST 只输出以下 allowlisted receipt fields；不得增加 raw headers/body、exception、
Key、组织/workspace identity 或自由文本：

```json
{
  "schema_version": "anthropic-models-preflight/1.0",
  "status": "verified",
  "provider_id": "anthropic",
  "requested_model_id": "claude-sonnet-5",
  "returned_model_id": "claude-sonnet-5",
  "verification_method": "anthropic_models_retrieve",
  "api_origin": "https://api.anthropic.com",
  "anthropic_version": "2023-06-01",
  "checked_at_utc": "RFC3339 UTC",
  "latency_ms": 123,
  "http_status": 200,
  "http_attempts": 1,
  "network_calls": 1,
  "model_token_calls": 0,
  "token_usage": null,
  "cost": null,
  "models_api_authenticated": true,
  "exact_model_visible": true,
  "messages_api_verified": false,
  "tool_calling_verified": false,
  "usage_semantics_verified": false,
  "model_quality_claim_allowed": false,
  "authorizes_model_run": false,
  "request_id_sha256": null,
  "error_code": null
}
```

字段类型与 nullability：

- literals：`schema_version`、`provider_id`、`verification_method`、`api_origin`、
  `anthropic_version`；
- `status` 只能是 `not_run | verified | failed`；
- `requested_model_id` 是已严格验证的 allowlisted string 或 null；只有 raw input 在本地
  validation 前即非法时才为 null，避免把任意输入反射进 receipt。`returned_model_id` 是
  string 或 null；
- `checked_at_utc` 是 RFC 3339 UTC string；`latency_ms` 是非负 integer 或 null；
- `http_status` 是 integer 或 null；`http_attempts` 与 `network_calls` 各只能是 0 或 1；
- `model_token_calls` 固定为 0，`token_usage` 与 `cost` 固定为 null；
- `models_api_authenticated` 与 `exact_model_visible` 是 boolean 或 null；其余 capability/claim
  flags 固定为 false；
- `request_id_sha256` 是 64 位小写 hex string 或 null；
- `error_code` 在 `verified` 时为 null，在 `not_run/failed` 时必须是下面 taxonomy 的稳定
  string。

`latency_ms` 是一次 metadata request 的本地 wall-clock observation，不是 Provider SLA。
若保留 `request_id_sha256`，它只用于本地关联/去重；Anthropic support 需要 raw
`request-id`，hash 无法供其检索。默认设计主动放弃事后 support lookup：raw ID 不进入
receipt/log，session 结束后不可恢复。未来若确需把 raw ID 交给 support，必须另建显式授权、
不落盘的一次性人工通道。不要持久化 display name、capability tree、组织/workspace header
或未知 response fields。

Result fields 使用三态而不是把未知伪装成 false：

- `status=not_run`：本地门禁在发请求前停止；attempts/network 为 0，HTTP/latency/returned
  model/request hash 为 null，auth/visibility 为 null，error code 非空；非法 raw model 输入
  时 `requested_model_id` 也为 null，其他本地门禁可保留已验证的 allowlisted ID；
- `status=verified`：仅限完整 200 success contract；attempts/network 为 1、HTTP 为 200、
  latency 非负、returned model exact、auth/visibility 为 true，error code 为 null；
- `status=failed`：已经尝试但未满足成功合同；attempts/network 为 1，HTTP 可以为 status 或
  null，latency 非负，error code 非空；returned model/auth/visibility 按已证明事实填值，
  其余保持 null；
- `models_api_authenticated=false` 仅用于明确的 401；除成功外的其他 HTTP/network 状态
  保持 `null`，不从 403/404/429/5xx 猜测 credential 状态；
- `exact_model_visible=false` 仅用于经过认证且明确表明该 exact model 不可见的结果；若无法
  区分不存在、权限或暂时故障则保持 `null`；
- `messages_api_verified`、`tool_calling_verified` 和 `usage_semantics_verified` 始终为
  false，因为本预检不测试这些能力。

## Failure taxonomy

所有失败都必须返回稳定、无 Key、无原始 Provider message 的本地 code。未知状态 fail closed。

| 条件 | 稳定 code | 语义 |
| --- | --- | --- |
| 未确认联网 | `anthropic_preflight_confirmation_required` | `not_run`，network 0 |
| Key 缺失 | `anthropic_preflight_key_missing` | `not_run`，client 未创建 |
| Key 不是安全 header value | `anthropic_preflight_key_invalid` | `not_run`，不回显 Key，client 未创建 |
| model 不在 allowlist | `anthropic_preflight_model_not_allowed` | `not_run`，network 0 |
| timeout/dependency drift | `anthropic_preflight_configuration_invalid` | `not_run`，network 0 |
| 3xx | `anthropic_preflight_redirect_denied` | 固定 origin 未得到直接响应 |
| 400 | `anthropic_preflight_invalid_request_or_spend_limit` | 请求/组织 spend limit；不推断模型质量 |
| 401 | `anthropic_preflight_auth_failed` | Key 无效、过期或撤销 |
| 402 | `anthropic_preflight_billing_blocked` | billing/payment 问题 |
| 403 | `anthropic_preflight_permission_denied` | Key 无资源权限 |
| 404 | `anthropic_preflight_model_unavailable` | exact model 不可见或不存在 |
| 408 | `anthropic_preflight_provider_timeout` | Provider request timeout；不自动重试 |
| 409 | `anthropic_preflight_conflict` | Provider resource state conflict |
| 413 | `anthropic_preflight_protocol_failed` | 无 body GET 不应超限；视为合同异常 |
| 429 | `anthropic_preflight_rate_or_spend_limited` | rate、tier cap 或 workspace spend limit |
| 504 | `anthropic_preflight_provider_timeout` | Provider 返回 timeout error；不自动重试 |
| 500/502/503/529/其他 5xx | `anthropic_preflight_provider_unavailable` | Provider 暂时不可用；不自动重试 |
| connect/read timeout | `anthropic_preflight_timeout` | 单次尝试超时 |
| DNS/TLS/network | `anthropic_preflight_network_failed` | 连接失败；不记录底层正文 |
| 200 但 id/type 不匹配 | `anthropic_preflight_identity_mismatch` | fail closed，不接受 alias/异常 schema |
| 非 JSON/超限/documented 顶层字段缺失或类型错误 | `anthropic_preflight_response_invalid` | fail closed；未知新增字段可忽略 |
| client close 失败 | `anthropic_preflight_client_close_failed` | 原本可成功也不得标 verified；若已有主失败则保留主 code |
| caller cancellation | `n/a (propagated)` | `finally` 清理后向上传播；不生成 receipt/error_code |
| 其他 | `anthropic_preflight_failed` | unknown 保持失败 |

虽然官方建议对部分 transient errors 重试，本预检刻意保持一次尝试，避免隐藏请求数和在
未授权状态下扩大网络动作。需要重试时必须由用户重新发起一次新的显式 preflight；不能在
一次命令内部自动重放。

## Integration boundary

- `phase6-status --provider anthropic` 继续是纯离线状态查询，`network_calls=0`；不得静默
  触发本预检。
- 当前 generic `phase6-run-online` 与 `self-pilot-run` 只有 parser choice；runtime、direct
  Phase 6 Agent、Eval Provider executor 与 public `AnthropicProvider.open_model` 均在
  Key/client 前 fail closed。底层 adapter 行为只能由 module-private single-use offline-test
  capability 覆盖，生产 capability factory 不存在。它们不是受控 Anthropic pilot，也不得
  接受 standalone receipt 解锁。
- module-private Python test seams 不是安全隔离边界；能任意导入/调用 underscored internals
  的受信任代码持有者在本威胁模型之外。对外支持的 public method 默认拒绝，CLI/service
  路径不能获得 test capability。
- generic self-pilot 还会使用 repo public tasks，不满足“非已运行公开题/不可调参”的 task
  boundary。未来实现必须另建 dedicated controlled-pilot orchestration + disjoint synthetic
  pack，或在这些 generic entrypoints 对 Anthropic 继续 fail closed，然后才能提供 Key。
- self-pilot Web catalog 继续不展示 Anthropic。preflight 实现、离线测试或一次获授权 live
  metadata check 都不是启用 Web 的充分条件；任何启用都需要独立 reviewed code change 与
  明确授权，成功 preflight 绝不自动改变 catalog。
- Eval v2 public runner 继续拒绝 Anthropic；成功 preflight 不改变 campaign slot，仍为
  `planned`。
- historical candidate v3 commitment 保持不变；successor candidate v4 绑定实现后的 source
  bundle，`prior_results_inherited=false`，不能继承 v1/v2/v3 成绩。
- pilot staging 继续使用 DeepSeek；supervised v4 pack 未运行，不能改写为 Anthropic pack。
- private custodian 继续 synthetic-only、private 0/50、Provider 1/2、not authorized；
  preflight 与 private release 无关。
- Phase 6 仍只支持首次审批暂停，不因 preflight 支持批准后在线恢复。

## Required offline verification

实现 PR 至少覆盖以下无网络测试，并使用内部 injected `httpx.MockTransport`。该
transport factory 只作为 private test seam，不得暴露为 CLI/API 的 URL、header、proxy 或
transport override：

1. 未确认、缺/非法 Key、非法 model、dependency drift 与 HTTP debug logging 均在 client
   创建前停止；
2. exact method/origin/path/body、required/forbidden application headers；允许标准 transport
   headers，不出现 Messages、prompt、Authorization、Content-Type 或 beta header；
3. 200 + exact id/type + 当前 documented top-level ModelInfo 类型是唯一成功路径；
   alias/different id、必需字段缺失/类型错误、malformed/oversized JSON 全部拒绝，未知新增
   字段可忽略但不持久化；
4. 3xx、400、401、402、403、404、408、409、413、429、500、504、529 与未知 status 映射稳定；
5. timeout、DNS、TLS、取消和 client-close failure 路径；identity body 64 KiB 可成功，读取
   第 65,537 byte 时立即 abort/close；non-identity encoding 在 body读取前拒绝；
6. 每次最多一次 request，retry/fallback/redirect 均为 0；
7. `trust_env=false`，环境 proxy、`.netrc`、`SSL_CERT_FILE`/`SSL_CERT_DIR` 与任意 base URL
   不能改变目标；固定 certifi roots，`SSLKEYLOGFILE` 不能创建文件或启用 TLS key logging；
8. Key 和 raw request/error body 不出现在 repr、exception、stdout/stderr、receipt 或 test
   snapshots；
9. raw request ID 不持久化；可选 hash 符合预期；
10. success/failure receipt 只有 allowlisted fields，token usage/cost 保持 null；
11. preflight success 不修改 campaign、candidate、Web catalog、pilot pack 或 private flags；

CI 只能运行这些 fixtures，不配置 `ANTHROPIC_API_KEY`，也不访问 Anthropic。

## Live-use gate

一次真实 Models API preflight 仍需用户显式提供 Key 与联网授权；成功结果只是 pilot 的必要
条件，不是充分条件。后续小规模 tool/usage/error-semantics pilot 还必须另有：

- 用户明确批准的封顶预算与一次性授权；
- 预承诺的 synthetic、非当前公开已运行题、非 repo-local holdout task set；
- 固定 Provider/model/transport/dependency/config hash；
- 禁止用结果调整 prompt、scorer、tool schema 或 candidate；
- 独立报告 tool、usage、error 与 completion semantics；
- future same-process pilot capability 必须 single-use/有 expiry，并拒绝不同 model、transport
  或 credential binding；该 capability 当前未实现；
- 无论结果如何，都不自动注册第二 Provider。

只有外部领域专家复核、R/SAS 独立 cross-check，以及 external custodian 的 private 50 题
评测与合规 aggregate completion 实际完成后，才考虑（不是自动）正式注册第二 Provider或
启用 non-synthetic private evaluation。在此之前所有相关 claim/authorization flags 保持
false；当前 kit 仍不支持 non-synthetic release，因此这项完成条件目前尚不可达。

## Implementation acceptance criteria

本实现完成的判据是：请求身份、联网确认、Key 生命周期、成功语义、失败分类、脱敏 receipt、
集成边界、无网络测试矩阵和后续人工门禁均由代码/合同覆盖且互不矛盾。它不以 live API
成功、campaign 注册或模型成绩作为完成条件。
