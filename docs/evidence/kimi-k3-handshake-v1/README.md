# Kimi K3 non-streaming synthetic handshake — tool-protocol failure

## Outcome

One separately authorized, single-use synthetic handshake failed closed on the first model response
during strict local tool-protocol validation.

```text
status                         failed
error_code                     kimi_chat_tool_protocol_invalid
scenarios completed            0 / 1
network attempts / calls       1 / 1
model requests                 1 / 2
tool executions                0 / 1
invalid-request probe          not reached
input / output tokens          283 / 116
usage complete                 true
local observed cost            CNY 0.017260
actual Provider bill           unknown / null
outcome unknown                false
retry / resume authorized      false / false
```

The authorization was consumed. No second model request, local tool execution, fixed HTTP 400 probe,
retry, resume or fallback occurred. Kimi remains `planned_not_registered`.

## Interpretation boundary

`kimi_chat_tool_protocol_invalid` is a stable local taxonomy. It means no Provider tool call passed
the frozen parser and became trusted. Raw Provider headers and bodies were deliberately not retained,
so the exact response shape and causal root cause remain unknown. This evidence does not attribute a
malformed response or fault to Kimi.

The first response carried a validated usage projection: 283 input and 116 output tokens. That is
enough to compute the local CNY 0.017260 estimate under the frozen K3 CNY 20/M input and CNY 100/M
output prices. It does not verify end-to-end usage semantics, and it is not a Provider invoice.

## Publication boundary

The public outcome is [public_receipt_projection.json](public_receipt_projection.json). Original
consume and terminal receipts are not copied because they contain authorization-derived linkage and
exact operational fields. [artifact_commitments.json](artifact_commitments.json) publishes only
their byte sizes and SHA-256 values.

The publication excludes the authorization identifier/hash, authorization binding, Key and request
headers, request ID, raw prompt, response, reasoning, tool arguments/results, local paths and all
private or non-synthetic data.

## Claim boundary

This evidence proves only that one K3 synthetic attempt reached a known local failed terminal after
one network call and that usage/cost counters were captured. It does not establish Chat, tool, usage
or error-semantics compatibility, model quality, Provider registration, private/non-synthetic
support, billing accuracy or production reliability.

The consumed authorization must not be retried. The current parser, prompt, tool schema, scenario and
candidate may not be adjusted from this observation and rerun under the same plan. Any future Kimi
work requires an independently justified successor, a new commitment and a fresh authorization.
