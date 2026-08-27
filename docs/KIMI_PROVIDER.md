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

## Current offline diagnostic successor — candidate v8

Candidate v8 freezes Chat parser v3 and controlled Pilot v3 at commitment
`b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c`, with Candidate v7
commitment `2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5` as its immutable
predecessor. It is `diagnostic_snapshot_only=true`; public-regression and controlled synthetic Pilot
online authorization are fixed false and cannot be unlocked by authorization. Public/Pilot execution configuration
remains Candidate v5 / Pack6 and DeepSeek. Candidate v8 is not selected by or wired into Pilot
Staging.

Chat v3 preserves v2 request bytes, response acceptance predicates and validation precedence. Its
only protocol delta is a 39-value fixed local diagnostic enum for the broad
`kimi_chat_response_invalid` class. The diagnostic stores only
`kimi-response-validation-diagnostic/1.0` plus an allowlisted branch code. It cannot contain raw
headers/body, Provider request/completion/model IDs or their hashes, actual field names or values,
JSON pointers, offsets, sizes, prompt/reasoning/tool payloads, authorization bindings or free-text
exceptions and cannot support causal Provider-fault attribution.

Pilot v3 uses independent receipt/event/checkpoint 3.0 schemas and the
`artifacts/kimi_controlled_pilot_v3` namespace. When the terminal error remains response-invalid, the
diagnostic must propagate identically from `request_failed` to `run_terminal`, checkpoint and receipt.
If a terminal authorization/terms/pricing guard supersedes it, the request-failure diagnostic remains
hash-bound while later projections are null. The verifier rejects missing, unknown, non-generic or
conditionally drifting values. It checks v1/v2/v3 authorization namespaces and creates the v3
cross-version tombstone before any Key lookup.

The local unkeyed hash chain checks structure and projection consistency only. A fully consistent
replacement with another valid enum can be re-hashed by an attacker controlling all local artifacts;
authenticated tamper evidence requires a repo-external signature/HMAC or external chain-head anchor
and is not claimed. Private, gitignored events/checkpoints/receipts retain authorization hashes and
bindings, exact operation times, tombstone and event-chain integrity values. They must never be
published verbatim; any future public evidence requires a separate minimal projection and publication
gate.

Candidate v8 has performed zero online calls and inherits neither the v7 authorization nor its
post-lock failure. The v6 and v7 online commands remain permanently disabled. Candidate v8 is also
fixed `online_not_authorized`; both unconfirmed and confirmed CLI paths return zero-call,
zero-Key-load non-authorizing receipts. See the
[Candidate v8 / Pilot v3 runbook](KIMI_CONTROLLED_PILOT_V3_RUNBOOK.md),
[Chat v3 contract](../evals/v2/kimi_chat_completions_contract_v3.json),
[Pilot v3 contract](../evals/v2/kimi_controlled_pilot_contract_v3.json) and
[runtime v8 contract](../evals/v2/kimi_runtime_candidate_v8_contract.json).

## Frozen historical snapshot — candidate v7

Candidate v7 freezes the official-documentation-driven Chat parser v2 and isolated controlled Pilot
v2 at commitment
`2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5`, with Candidate v6
commitment `57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641` as its immutable
predecessor. Public-regression and supervised-pilot execution remain DeepSeek; Kimi remains an
independent, unregistered synthetic compatibility configuration.

Parser v2 requires a non-empty terminal choice with `finish_reason` and accepts documented usage at
the top level, choice level, or both when reconciled. Core prompt/completion/total tokens are required;
optional cache reporting cannot reduce the all-uncached conservative budget unless it is explicitly
present. Empty-choices usage trailers, conflicting projections, unknown usage fields, duplicate
terminal/DONE and post-terminal data remain fail-closed. The protocol binds the first-party Chat,
migration and streaming documentation captures.

Pilot v2 has an independent capability, receipt/event/checkpoint 2.0 schemas, verifier and
`artifacts/kimi_controlled_pilot_v2` namespace. Candidate v7, Chat v2 and Pilot v2 commitments are
bound at runtime before the Key loader can run; authorization IDs are checked against both v1 and v2
local artifact namespaces. After the v7 lock, one separately authorized attempt failed closed during
local response validation on the first required-tool request: one request/call, zero completed
scenarios, zero trusted Provider tool calls, zero tool executions and zero usage observations. Actual
tokens, bill and Provider latency remain unknown. No v6 result was inherited, and this observation is
not inherited into Candidate v7 or Pack v8. See the
[sanitized v7 failure evidence](evidence/kimi-controlled-pilot-v2-response-failure-v1/README.md).

Candidate v7's one-time authorization is consumed; no further v7 online execution is authorized, and
no online command is published. Both the v6 and v7 online commands are permanently tombstoned. The
retained implementations are auditable snapshots, not permission to call them. See the
[Candidate v7 / Pilot v2 runbook](KIMI_CONTROLLED_PILOT_V2_RUNBOOK.md),
[Chat v2 contract](../evals/v2/kimi_chat_completions_contract_v2.json),
[Pilot v2 contract](../evals/v2/kimi_controlled_pilot_contract_v2.json) and
[runtime v7 contract](../evals/v2/kimi_runtime_candidate_v7_contract.json).

`pilot_pack.supervised_v7.json` and `pilot_pack.supervised_v8.json` are immutable historical artifacts;
neither is selected by the active Pilot Staging composition or invite path. Candidate v5 / Pack v6
remain the configured active baseline, while current-source execution stays fail-closed until a
separately locked current successor exists. The versioned
[post-lock status overlay](evidence/kimi-historical-status-overlays-v1/README.md) binds both Pack/review
hashes and records that the separate one-call observations occurred without being inherited into a
Candidate, Pack, compatibility result or quality claim.

## Historical candidate v6 and consumed post-lock attempt

Candidate v6 freezes an independent, unregistered Kimi compatibility configuration at commitment
`57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641`. It keeps the Eval v2
public-regression and supervised-pilot execution Provider as DeepSeek while binding the exact
`moonshot_kimi / kimi-k3 / https://api.moonshot.cn / chat_completions / reasoning_effort=low`
synthetic-only configuration and its resource limits.

The fixed SSE Chat/tools adapter and three-scenario controlled pilot are implemented and covered by
MockTransport. After Candidate v6 was locked, one separately authorized post-lock attempt stopped at
the first response's usage-validation stage: one request/call, zero completed scenarios, zero trusted
Provider tool calls and zero tool executions. Usage, actual token consumption, bill and Provider
latency remain unknown; G4 is `planned_not_registered` and the consumed authorization cannot be
retried. This observation is not inherited into Candidate v6 or Pilot Pack v7. See the
[sanitized failure evidence](evidence/kimi-controlled-pilot-usage-failure-v1/README.md).

An offline postmortem confirmed that the frozen parser's usage-only terminal-chunk assumption differs
from the official documented same-chunk finish/usage example. Because no raw Provider body was saved,
that independent contract mismatch is not a unique causal explanation for the observed
`kimi_chat_usage_invalid`; the live root cause remains undetermined. Any future attempt requires
Candidate v8 or a later versioned successor, a fresh legal and Kimi K3 pricing review, a complete authorization
window and entirely new one-time authority. The existing local caps remain 8 model requests, 40,000
input tokens, 10,000 output tokens, 6 tool executions, 10 minutes and CNY 5; they do not guarantee the
Provider bill.

The historical runtime contract is
[`evals/v2/kimi_runtime_candidate_v6_contract.json`](../evals/v2/kimi_runtime_candidate_v6_contract.json).
It records `offline_ready_not_run`, zero online/model-token calls in the locked v6 snapshot, no campaign
or registry registration, no private/non-synthetic support and no model-quality or compatibility claim.
The consumed-run procedure is retained in the
[controlled synthetic pilot runbook](KIMI_CONTROLLED_PILOT_RUNBOOK.md).

## Historical candidate v5 metadata baseline

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
future-dated text as an already-effective promise. Before `2026-08-30T16:00:00Z`, G2a allowed only a
separately authorized synthetic compatibility pilot whose fresh current-effective legal capture,
preview delta review, Kimi K3 pricing capture and complete 600-second window all passed the locked
local gates. The consumed v6/v7 attempts used that pre-effective branch. At or after the cutoff, G2a
is invalid and the actually effective legal/privacy text must be captured and reviewed before any
later Chat Completions request or pilot. Neither branch permits private or non-synthetic data. A
Models-list metadata preflight sends no prompt and generates no model tokens, but remains separately
authorized and non-authorizing.

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
new explicit authorization. The redacted observation, merge provenance and clean-main boundaries are
recorded in the
[Kimi Models preflight main CI snapshot](evidence/kimi-models-preflight-main-ci-v1/README.md) and the
[PR #21 comment](https://github.com/cedRiC874/researchops-agent/pull/21#issuecomment-5423475486).
No raw request ID or request-ID hash is reproduced here.

The verified receipt establishes only China-platform authentication and exact `kimi-k3` visibility at
that point in time. It does not verify or authorize Chat Completions, tool calling,
streaming, usage accounting, cost, availability, error semantics, model quality, a pilot, campaign
registration or private evaluation. A pilot would require a separate Key, budget, request/token caps
and one-time authorization.

## Frozen campaign and candidate-lineage boundary

Candidate v4 and v5 remain unchanged. Candidates v6, v7 and v8 do not alter `evals/v2/campaign.json`, the
public prompt, scorer, tool schema, scenario selection or historical evidence.

Candidate v5 entered `main@c65ff65c` through regular-merged PR #21 at commitment
`105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc`. Candidate v6 succeeds v5;
Candidate v7 succeeds v6 and is retained as a historical snapshot; Candidate v8 succeeds v7 only as
a source-bound diagnostic snapshot. Every successor fixes
`prior_results_inherited=false`; neither post-lock failure nor either consumed authorization is a
candidate result. Any later Kimi runtime change requires a newly versioned successor later than v8.
Public execution remains DeepSeek, Eval v2 remains `design_only`, registered Providers remain 1/2 and
private remains 0/50. A Key, successful preflight or successful synthetic pilot cannot automatically
register Kimi or enable non-synthetic private evaluation.

## Gates

1. Candidate v5 and its successor pilot pack passed PR #21 and `main@c65ff65c` clean checks. The exact
   merge and three final main runs are frozen in the
   [main CI snapshot](evidence/kimi-models-preflight-main-ci-v1/README.md).
2. The separately authorized zero-generation-token metadata request completed successfully after the
   lock. Its authorization is consumed and it remains non-authorizing. Do not retry it; a new request
   requires new explicit authorization.
3. Candidate v6 and v7 each consumed one separate post-lock authorization and failed closed on the
   first request. Neither may be retried, and both online commands are permanently disabled.
4. Candidate v8 cannot be unlocked by authorization. Any future attempt requires a successor later
   than v8, fresh legal/pricing review and a new explicit one-time authorization. The v6/v7 observations cannot tune the prompt, scorer, tool schema,
   scenarios, candidate or task selection.
5. Keep private and all non-synthetic release denied until written enterprise data protections,
   external expert review, independent R/SAS cross-check and actual external private-50 evaluation
   are complete.
6. Registration, if later eligible for consideration, requires a distinct campaign change and
   explicit approval; it is never automatic.
