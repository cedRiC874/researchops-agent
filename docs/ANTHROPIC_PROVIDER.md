# Anthropic Provider — offline adapter contract

## Status

Anthropic is implemented as an **offline-contract-only** third Provider adapter. It is available to
the Phase 6 and self-pilot CLIs, but it is not yet registered in the Eval v2 campaign and is not
enabled in the self-pilot web model-catalog preflight or the paid public-regression runner.

No Anthropic API Key, authenticated model-catalog check, model request, result, pass rate or cost
evidence exists. Existing DeepSeek public/pilot results are not inherited.

Machine-readable boundary:
[`evals/v2/anthropic_provider_contract.json`](../evals/v2/anthropic_provider_contract.json).

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
  tests.test_phase6_providers -v
```

Online commands remain gated by an explicit `--confirm-online` and `ANTHROPIC_API_KEY`. Do not run
them until a separate budget, retention policy and one-time evaluation authorization are approved.

## Gates before campaign registration

1. Authenticate the exact model through an Anthropic-specific Models API preflight without a model
   token call.
2. Run a small, budgeted tool/usage/error-semantics pilot that is not used to tune the frozen public
   tasks.
3. Add provider-specific price/coverage handling to the public runner or keep it fail-closed.
4. Freeze the exact Provider/model/transport/config hash in a successor campaign.
5. Only then change the campaign slot from `planned` to `registered`.
