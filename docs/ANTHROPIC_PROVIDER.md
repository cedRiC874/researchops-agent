# Anthropic Provider — offline adapter and Models preflight contract

## Status

Anthropic remains an **offline-contract-only** third Provider adapter. A fixed-origin Models API
metadata preflight is implemented and covered by injected `httpx.MockTransport` tests. After candidate
v4 was locked, one separately authorized request used a CCTK gateway token against the official origin
and returned HTTP 403 with one network call and zero model tokens; it did not verify an official
Anthropic credential or CCTK. Generic Phase 6/self-pilot Anthropic online entrypoints, direct Agent/
Provider-executor calls and public `AnthropicProvider.open_model` now fail closed before reading a
Key. Adapter internals remain testable only through a module-private, single-use offline-test
capability; no production authorization factory exists. Self-pilot Web, Eval v2 public runner and a
controlled pilot entrypoint remain disabled.

Underscored Python test seams are not claimed as a security boundary; arbitrary trusted code with
source-level access is outside this entrypoint threat model. Supported public methods deny by
default, and CLI/service paths cannot obtain the offline-test capability.

No official Anthropic API Key, authenticated model-catalog check, model request, result, pass rate or
cost evidence exists. The CCTK route has been abandoned, and existing DeepSeek public/pilot results are
not inherited.

PR #19 regular-merged this offline implementation to `main@77911226`; all three clean-main workflows
passed without a Provider Key or model call. The merge provenance, exact run links and non-authorizing
claim boundary are recorded in
[Anthropic Models preflight main CI v1](evidence/anthropic-models-preflight-main-ci-v1/README.md).

Machine-readable boundaries:

- predecessor adapter contract:
  [`evals/v2/anthropic_provider_contract.json`](../evals/v2/anthropic_provider_contract.json);
- current preflight/entrypoint contract:
  [`evals/v2/anthropic_models_preflight_contract.json`](../evals/v2/anthropic_models_preflight_contract.json).

## Frozen adapter identity

```text
provider_id=anthropic
default_model_id=claude-sonnet-5
transport_id=litellm_anthropic_chat_completions
api_base=https://api.anthropic.com
api_key_env=ANTHROPIC_API_KEY
openai-agents=0.21.0
litellm=1.83.0
```

The allowlist contains the exact Claude API IDs `claude-sonnet-5`, `claude-opus-4-8`, and
`claude-haiku-4-5-20251001`. Anthropic documents these as fixed model IDs rather than accepting an
arbitrary model string; see the official
[model ID and versioning reference](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions).

`litellm==1.83.0` is an exact, locally verified compatibility pin for the repository's locked
`openai-agents==0.21.0` and `openai==3.1.0`. The lock lists versions but not wheel/sdist hashes.

## Runtime controls

- missing Key, disallowed model, invalid timeout, missing dependency or version drift fails before a
  Provider request;
- every run owns an `AsyncHTTPHandler` with the explicit timeout and official API base;
- concurrent Anthropic use in one process fails closed so LiteLLM's process-global state cannot be
  shared across controlled runs;
- `num_retries=0`, `max_retries=0`, and empty fallbacks are injected after trace-safe settings
  serialization, so caller settings cannot enable hidden retries or routing;
- process-global LiteLLM retries, aliases, prompt rewrites, fallbacks, callbacks, cache and prior
  debug records must be empty;
- third-party OpenAI tracing remains disabled and sensitive trace data remains excluded;
- LiteLLM message logging is disabled during the controlled context and its module-global debug-log
  sink discards request/Key records before the original empty object is restored;
- the Key is excluded from repr/errors/artifacts, cleared from the model object at context exit, and
  the owned HTTP handler is closed on success or failure;
- usage is requested, but a positive request with all-zero token counts is recorded as unavailable,
  never as genuine zero usage or zero cost;
- an outer run timeout still bounds the complete Agent call.

LiteLLM remains a best-effort compatibility layer. Offline construction and injected-runner tests do
not establish tool-call compatibility, token accounting, latency, price, availability or model
quality on the real Anthropic API.

## Offline commands

These commands do not call Anthropic:

```powershell
.\.venv\Scripts\python.exe -m researchops.cli phase6-status `
  --provider anthropic

.\.venv\Scripts\python.exe -m unittest `
  tests.test_phase6_providers tests.test_anthropic_preflight -v

.\.venv\Scripts\python.exe -m researchops.cli anthropic-models-preflight `
  --model claude-sonnet-5
```

The unconfirmed preflight command returns `not_run` with `network_calls=0` and does not read a Key.
Only this dedicated command can make the metadata request, and only with `--confirm-online` plus
`ANTHROPIC_API_KEY`. Generic `phase6-run-online`, `self-pilot-run`, Web and public-runner paths reject
Anthropic and cannot consume a preflight receipt. A controlled Messages/tool pilot entrypoint is not
implemented.

## Models API preflight implementation

The Anthropic-specific availability contract is specified in
[Anthropic Models API zero-generation-token preflight design](ANTHROPIC_MODELS_API_PREFLIGHT.md).
It uses one fixed `GET /v1/models/{exact_model_id}` metadata request to verify Models API
authentication and exact allowlisted model visibility. It does not call Messages/Completions or
produce model output tokens, but it is still an online authenticated request and is not evidence of
tool compatibility, usage/error semantics, cost, availability SLA or model quality.

The implementation owns a direct `httpx==0.28.1` client with exact `httpcore==1.0.9` /
`h11==0.16.0` and fixed `certifi==2026.7.22` CA roots,
fixes origin/version/headers, performs at
most one GET, disables retries/fallbacks/redirects/environment proxies/TLS key logging, rejects
unsafe Key header values and HTTP debug logging before client creation, rejects compressed responses,
bounds identity bodies to 64 KiB and emits a strict non-authorizing receipt. Its only live observation
is the post-lock 403 credential-origin mismatch described above; no verified Models result,
Messages/tool request, token/cost observation or quality result exists.

Candidate v4 is integrated into main and binds the source and machine contract without modifying or
inheriting v3.
`phase6-status` remains offline, generic Anthropic runs remain disabled, and campaign registration
remains false.

## Gates before campaign registration

1. The Anthropic track is paused. Any future attempt requires a direct official Anthropic Key and a new
   one-time authorization; the prior CCTK token must never be reused. A successful receipt still would
   not authorize Messages/tools.
2. Run a small, budgeted tool/usage/error-semantics pilot that is not used to tune the frozen public
   tasks, only after the user supplies a Key, capped budget and one-time authorization.
3. Add provider-specific price/coverage handling to the public runner or keep it fail-closed.
4. Freeze the exact Provider/model/transport/config hash in a successor campaign.
5. Complete external domain-expert review, an independent R/SAS cross-check and the external
   custodian's actual private-50 evaluation plus compliant aggregate completion before
   considering—not automatically changing—the campaign slot from `planned` to `registered` or
   enabling non-synthetic private evaluation. The current synthetic-only kit cannot yet satisfy this
   condition.
