# Value-free business privacy diagnostics v1

This additive diagnostic contract is `item6-business-privacy-diagnostic/1.0`.
It does not change scan acceptance, refusal/isolation eligibility, timing,
budgets, scoring, ownership, or batch-stop behavior. Freeze 1.2 and case-isolation
1.1 remain unchanged and bind implementation through the exact source manifest.
New artifact/archive 1.4 requires nullable `privacy_diagnostic` on each case.
Original schemas and producer archives remain byte-preserved; no retrospective
label is added to the IC-14 failure or any old artifact.

## Fixed labels and no content retention

Only values returned by the real SDK at its model-output boundary are covered:
`sdk_text` (output text parts) and `sdk_tool_args` (decoded tool arguments).
Other existing callers of the business scanner retain their original behavior
without claiming diagnostic coverage. Null means **not recorded**, never proof
that the field was clean or that the Provider omitted anything. Older artifact
versions did not persist this diagnostic and use their original reader.

P00 means the original shared privacy/canary scanner rejected the value; it
does not distinguish a particular shared regex from a canary. The subsequent
unchanged business regex reports its first matching alternative: P01 email,
P02 drive-path prefix, P03 Users/home path, P04 traceback marker, P05 auth-header
marker, P06 reasoning-text marker. Rule order, expressions and case-insensitive
matching stay the same. Opaque codes avoid recursively triggering the scanner
on a diagnostic label itself.

Records contain only fixed schema/rule/location/error enums, case-local SDK
response index, global response and attempt indices, and hashes referencing
already-approved telemetry/audit metadata. They never contain the rejected
value, matched substring, offsets, lengths, a digest of the rejected value,
tool argument values, Key, exception text/args or traceback. Existing telemetry
hashes are references to sanitized records, not hashes of rejected content.

## Association and failure handling

The model wrapper calls the original scanner before storing text, tool plans,
call IDs or replay. On a real typed scanner failure, a bounded callback binds
the diagnostic to the active real Case/Owner and the last accepted response for
that case. It links the exact response event and completion-record digest and
persists `item6_business_privacy_rejected_v1` as a system event in the existing
append-only chain. The case field becomes `recorded` only after export confirms
the acknowledgement, payload and event order.

Before an append is attempted, the case field is `unacknowledged` with null event
hash. A before-write or committed-but-unacknowledged failure keeps that state;
failure before association may leave the field null. The original scanner
exception is always re-raised, including when diagnostic production throws.
No callback return, failure or serialized label grants permission to send,
execute a tool, retry, isolate the privacy failure, or start another case.
Evidence failure cannot turn rejection into success or replace the original
failure attribution. The normal runner stops the batch as before.

## Independent readback: scope and limitation

Readback validates exact fields/enums/types, mode/run/case/freeze/source binding,
the actual last accepted response, SDK/global indices, audit ordering and
record-to-event equality. It rejects orphan/cross-case records, extra fields,
invalid labels, altered response links, forged successful completion or work
after the privacy stop. Unacknowledged writes remain explicitly incomplete;
existing committed events are checked without fabricating a missing ack.

Because the rejected value is deliberately not retained, **readback cannot
independently rescan the text or prove that a valid rule/location label describes
the actual text**. A coherent producer rewrite of both labels and all envelopes
is not detectable from these artifacts alone. This is producer-bound diagnostic
evidence, not an external witness, raw-response capture, or proof of a leak or
false positive. Tests must not claim otherwise. Normal tamper/hash-chain and
cross-artifact/response-binding checks still apply.

All prior rejection, privacy, source/authorization, unknown-outcome, cleanup,
budget, expiry and I/O stop conditions remain. No raw body recovery, additional
online probe, real Key/store access, formal source-only continuation, CI change
or publication follows from this offline patch. Depth-60 20/60 and STATUS/T7
remain unchanged.
