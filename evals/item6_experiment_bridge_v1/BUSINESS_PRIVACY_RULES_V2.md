# item6-business-privacy-rules/2.0

Prospective, internally reviewed experiment revision. It grants no execution
authority. Historical diagnostic 1.0 and artifact/archive 1.4 retain their
original interpretation. Current diagnostic 1.1 and artifact/archive 1.5 bind
this revision through the frozen source commitment; freeze 1.2 still binds the
complete source manifest. Old artifacts are replayed using their original
version, never relabelled by this implementation.

## Classification and precedence

The unchanged shared public-artifact scan runs first, including canaries,
credential/header/private-key patterns, email, absolute paths and full stack
headers. Its exact rejection is labelled P00. Size limits remain unchanged.
The experiment's supplemental scanner then examines all canonical JSON bytes:

| Rule | Reject | Accept only in the absence of any other rejection |
| --- | --- | --- |
| P01 | email address | ordinary text |
| P02 | drive-letter absolute path with non-word boundary | the `s:/` inside HTTPS |
| P03 | absolute `/home/` or `/Users/` with path boundary | those segments inside an ordinary HTTPS URL |
| P04 | traceback header or `Traceback:` followed by content | a bare reference to the word Traceback |
| P05 | Authorization/proxy-Authorization followed by a field delimiter and value, including quoted or JSON-escaped field keys | a mention of the field name |
| P06 | reasoning_text followed by a field delimiter and value, including quoted or JSON-escaped keys | a mention of the field name |

An empty/null structured sensitive field is not an exemption. URLs are not
allowlisted containers: credentials, canaries, emails or absolute paths elsewhere
in the same text still reject. Native reasoning output items remain prohibited
by the unchanged response-shape gate. This is bounded detection, not proof that
arbitrary obfuscated data is non-sensitive. No rejected body, substring, position,
length or body hash is persisted. Labels remain producer-bound, not independent
proof of the discarded content.

## Frozen stop matrix

| Outcome | Continue? | Preconditions / attribution |
| --- | --- | --- |
| Legal completed answer, wrong fact/unit/direction, free expression | yes | FCC scoring unchanged; fail/unknown never promoted |
| Legal empty/whitespace output | yes | preserve exact text and FCC fail; missing message is not empty text |
| Ordinary read failure or requested facts missing | yes | case failure retained; all evidence and lifecycle gates must still hold |
| Original eligible missing/conflicting design or policy refusal | conditional | isolation 1.1, zero executed tools, bound exception, actual closure/reconciliation/seal; retain failed case |
| Invalid scope/tool/arguments or missing catalog prerequisite | no | no new isolation eligibility |
| SDK maximum turns, request/tool quota, cost/token/time limit | no | exact SDK MaxTurnsExceeded maps to item6_sdk_max_turns_exceeded; no retry or budget increase |
| HTTP failure, timeout, cancellation, outcome unknown | no | cleanup and faithful sealing only |
| Missing/unmapped/null native completion, invalid shape or usage | no | no fallback substituted for native attribution |
| Privacy, source/authority/claim/environment drift | no | no post-rejection send, tool or case |
| Audit, evidence IO, cleanup, reconciliation or sealing failure | no | incomplete evidence remains incomplete |

The target is the complete 16-case/32-observation denominator, not permission to
send all requests after a fatal error. Unexecuted suffix remains unexecuted.
Tasks, order, prompts, tool definitions, FCC, budgets and real store are unchanged.
Offline MockTransport runs are synthetic engineering evidence, not online results
or model-quality evidence. No change to 20/60 or STATUS/T7.
