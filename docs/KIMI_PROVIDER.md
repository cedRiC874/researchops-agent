# Kimi API Provider — design-only China transport contract

## Decision

Kimi API is a proposed independent ResearchOps Provider. It is not an OpenAI Provider merely because
its HTTP request and response shapes are OpenAI-compatible. The frozen design identity is:

```text
provider_id=moonshot_kimi
operator=Moonshot AI / Kimi API Open Platform
region_surface=China platform (platform.kimi.com)
api_origin=https://api.moonshot.cn
api_base=https://api.moonshot.cn/v1
credential_env=MOONSHOT_API_KEY
transport_id=moonshot_openai_compatible_chat_completions
chat_completions_path=/v1/chat/completions
default_model_id=kimi-k3
```

Candidate v5 freezes the pre-call status `preflight_implemented_offline_tested_not_run`. The dedicated
Models preflight CLI and MockTransport fixtures are implemented. After that lock, one separately
authorized metadata request verified China-platform authentication and exact model visibility; the
receipt is not inherited by candidate v5. There is no Chat Completions adapter, runtime registration,
Chat request, tool call, usage/cost-semantics evidence, pilot result or model-quality evidence. The
machine-readable pre-call boundary is
[`evals/v2/kimi_provider_contract.json`](../evals/v2/kimi_provider_contract.json).

The design and candidate lock reviewed public first-party documentation without reading a Key or
sending an authenticated request. The later post-lock request is a separate observation. Existing
DeepSeek, Anthropic, candidate, pilot and private results are not inherited.

## Fixed China-platform identity

The China API documentation specifies `https://api.moonshot.cn/v1`, Bearer authentication with
`MOONSHOT_API_KEY`, a Models endpoint and a Chat Completions endpoint. Keys issued by
`platform.kimi.com` and `platform.kimi.ai` are independent and must not be interchanged.

Any future ResearchOps implementation must therefore:

- fix the exact HTTPS origin and explicit paths; arbitrary base URLs are forbidden;
- use only `MOONSHOT_API_KEY`, held in memory and excluded from logs, errors and artifacts;
- identify receipts and results as `moonshot_kimi`, not `openai`;
- initially allow only the exact model ID `kimi-k3`; aliases and arbitrary caller-supplied model IDs
  remain denied;
- disable environment proxies, redirects, client retries, fallbacks, tracing and raw HTTP-body logs;
- read the Key only after all applicable local authorization, exact-model and configuration gates
  pass; request/token/cost budget gates apply to a future Chat Completions pilot, not the metadata GET.

Official protocol references:

- [API overview](https://platform.kimi.com/docs/api/overview)
- [List Models](https://platform.kimi.com/docs/api/list-models)
- [Chat Completions](https://platform.kimi.com/docs/api/chat)
- [Error reference](https://platform.kimi.com/docs/api/errors)
- [Kimi K3](https://platform.kimi.com/docs/guide/kimi-k3-quickstart)

OpenAI protocol compatibility does not establish identical tool, usage, streaming, retry, error or
billing semantics. Each must be verified by provider-specific fixtures and, later, a separately
authorized synthetic pilot.

## Public privacy and retention review

Review boundary: 2026-08-26 UTC, using public first-party pages without authentication.

The public [Kimi Open Platform Privacy Policy](https://platform.kimi.com/docs/agreement/userprivacy)
states that dialogue information, including user inputs and generated content, can help optimize the
model. The public [Kimi Open Platform Service Agreement](https://platform.kimi.com/docs/agreement/modeluse)
also grants a free right to use input, output and feedback for model-service optimization. The same
agreement describes customer-data processing as instruction-bound and disallows unauthorized use,
but the public optimization license prevents ResearchOps from claiming no-training or zero-data-
retention treatment.

The policy says personal information is stored in the People's Republic of China and retained only
for the period necessary to provide the service. It does not publish fixed retention periods for API
prompts, outputs, tool payloads, request metadata, abuse/security logs or backups. A complete API
subprocessor list, self-service training opt-out, public DPA, deletion SLA and public SOC 2 or ISO
assurance package were not found in the reviewed first-party pages. Unknown values remain `null`; they
must not be rewritten as zero, false, immediate deletion or a security guarantee.

Consequences for ResearchOps:

- only fresh synthetic, non-sensitive cases may be proposed for a future controlled pilot;
- prompts must contain no personal information, user email, business secret, real research record,
  private holdout item or custodian data;
- file upload, official web search, memory and other hosted tools remain out of scope;
- all non-synthetic and private evaluation is denied;
- a future successful synthetic pilot would not relax this data boundary.

Non-synthetic use requires separate written enterprise evidence covering no-training/no-model-
improvement, purpose limitation, exact retention and backup deletion periods, complete subprocessors,
data locations and transfer mechanisms, incident notice, deletion evidence, security assurance and an
applicable DPA. It also remains subject to the existing external-domain-expert, independent R/SAS and
private-50 gates.

## Model, price and rate-limit time boundary

The public-source values below are observations, not permanent contractual guarantees. Model
visibility is refreshed by the account-specific metadata preflight; legal, price and limit snapshots
must be rechecked before any Chat Completions request or pilot.

| Surface | Observed on 2026-08-26 | ResearchOps boundary |
| --- | --- | --- |
| Model | `kimi-k3`, 1M-token context advertised | Exact model visibility is unverified until a separately authorized metadata preflight |
| Price | Cache hit ¥2.00/MTok, input ¥20.00/MTok, output ¥100.00/MTok | Not a spend authorization or local hard cap |
| Account limits | Current values are account-specific and the public limit rules are under an announced update | Provider limits remain `unknown` and do not replace one-request or local budget gates |
| Pricing/limits | Provider may change prices and temporarily adjust limits | Freeze a new dated price/rate snapshot before Chat Completions or a pilot |

References: [Open Platform model and price surface](https://platform.kimi.com/),
[model pricing logic](https://platform.kimi.com/docs/pricing/chat), and
[recharge and rate limits](https://platform.kimi.com/docs/pricing/limits).

The public pages reviewed on 2026-08-26 already displayed agreements updated on 2026-08-24 with a
future effective date of 2026-08-31. Because that date had not arrived, this design does not treat the
future-dated text as an already-effective promise. Legal, privacy, pricing and rate-limit pages must be
reviewed again on or after 2026-08-31 and before any Chat Completions request or pilot. A Models-list
metadata preflight sends no prompt and generates no model tokens, but remains separately authorized and
non-authorizing.

The platform supports project daily/monthly spend limits and TPM limits, but documents an enforcement
delay of about ten minutes. Those controls are defense in depth only. A future harness must still
enforce local request-count, maximum-input-token, maximum-output-token and monetary ceilings. See
[organization, IP allowlist and project limits](https://platform.kimi.com/docs/guide/org-best-practice).

## Metadata preflight is non-authorizing

The provider documents `GET /v1/models`. ResearchOps now implements one dedicated fixed-origin Models
request with zero model-generation calls, zero client retries and no response-body persistence. The
pre-call candidate snapshot is covered only by offline fixtures and records
`not_run / network_calls=0`.

Offline-safe status command:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m researchops.cli kimi-models-preflight --model kimi-k3
```

Without `--confirm-online`, the command exits with the expected non-success receipt code, does not read
`MOONSHOT_API_KEY` and reports `http_attempts=0 / network_calls=0 / model_token_calls=0`.
The implementation enforces at most one GET per explicit invocation; it does not implement a global
authorization nonce or consumption ledger. One-time authorization remains an external operator gate.

### Post-lock observation

One separately authorized request completed after candidate v5 was locked:

- checked at `2026-08-26T09:41:49.967Z`;
- `GET https://api.moonshot.cn/v1/models`, `verified / HTTP 200`;
- HTTP attempts / network calls `1 / 1`;
- requested and returned model `kimi-k3`; authentication and exact visibility true;
- model token calls `0`; token usage and cost `null`.

The one-time authorization is consumed. The request must not be retried; any new metadata request needs
new explicit authorization. The redacted observation is recorded in the
[PR #21 comment](https://github.com/cedRiC874/researchops-agent/pull/21#issuecomment-5423475486).
No raw request ID or request-ID hash is reproduced here.

The verified receipt establishes only China-platform authentication and exact `kimi-k3` visibility at
that point in time. It does not verify or authorize Chat Completions, tool calling,
streaming, usage accounting, cost, availability, error semantics, model quality, a pilot, campaign
registration or private evaluation. A pilot would require a separate Key, budget, request/token caps
and one-time authorization.

## Frozen campaign and future candidate boundary

Candidate v4 remains unchanged. The successor does not alter `evals/v2/campaign.json`, prompt, scorer,
tool schema or historical evidence.

The Models preflight implementation is bound to successor candidate v5, with predecessor exactly
candidate v4, new contract hashes and `prior_results_inherited=false`. Candidate v4 remains unchanged.
Candidate v5 commitment is
`105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc`; supervised successor v6
continues to execute only DeepSeek and has not run online.
The post-lock Models receipt does not modify this commitment, inherit into v5/v6 or create a model-
quality result.
Any future Chat Completions adapter or other Kimi runtime change under `src/researchops/` requires a
new successor candidate v6 or later and cannot inherit v5 or any historical result.
Public execution remains DeepSeek, Eval v2 remains `design_only`, registered Providers remain 1/2 and
private remains 0/50. A Key, successful preflight or successful synthetic pilot cannot automatically
register Kimi or enable non-synthetic private evaluation.

## Gates

1. Candidate v5 and its successor pilot pack passed PR #21 implementation-head clean checks; no Chat
   Completions adapter is enabled. Verify PR disposition and current-head checks on GitHub rather than
   treating this pre-merge snapshot as main evidence.
2. The separately authorized zero-generation-token metadata request completed successfully after the
   lock. Its authorization is consumed and it remains non-authorizing. Do not retry it; a new request
   requires new explicit authorization.
3. Re-review the future-dated legal/privacy terms after they take effect and freeze a dated price and
   rate-limit snapshot before any Chat Completions request.
4. Only after a successful preflight and terms re-review, separately preregister and authorize a small synthetic-only
   tool/usage/error-semantics pilot with hard local request, token and cost ceilings. Its cases and
   results cannot be used to tune the frozen prompt or candidate.
5. Keep private and all non-synthetic release denied until written enterprise data protections,
   external expert review, independent R/SAS cross-check and actual external private-50 evaluation
   are complete.
6. Registration, if later eligible for consideration, requires a distinct campaign change and
   explicit approval; it is never automatic.
