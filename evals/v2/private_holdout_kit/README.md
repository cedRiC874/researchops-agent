# Eval v2 private-holdout custodian kit v1.1

该目录只包含公开协议和严格 schema，不包含也不得接收 private 题面、golden、真实 task ID、
case order、seed、storage locator、路径、逐题结果或 Provider body。

当前实现是 **synthetic conformance kit**，不是已激活的真实 private runner：

- campaign 仍为 `design_only / not_authorized`；
- private `0/50`、Provider `1/2`；
- private corpus commitment、授权、外部 golden review 与 R/SAS cross-check 均不存在；
- `verify-release` 对 `synthetic=false` 固定 fail-closed；
- 所有 private/performance/model-quality claim flags 固定为 `false`。

## Two-stage signed flow

```text
out-of-band trust + freeze + candidate + ledger anchors
        ↓
freeze_request (freeze-authority signature; private access=false)
        ↓
commitment_statement (custodian signature; salted opaque commitment)
        ↓
authorization_grant (two distinct Ed25519 keys; single_use=true)
        ↓
access_reserved (custodian-signed; atomically created before access)
        ↓
[optional dual-signed resume_authorized events]
        ↓
aggregate_results → terminal ledger event → signed ledger
        ↓
consumption_receipt → release_manifest
        ↓
offline synthetic verifier with all external anchors supplied explicitly
```

Schemas are in [`schemas/`](schemas/). Documents use strict JSON Schema 2020-12,
`additionalProperties=false`, duplicate-key/NaN rejection, canonical JSON, domain-separated SHA-256
and Ed25519 signatures. Authority/custodian key IDs, decoded public keys and anchored organization
commitments must all differ. Key validity windows are checked at every signature timestamp.

Core formulas (`||` means byte concatenation):

```text
document_sha256 = SHA256(
  UTF8("researchops-private-holdout-document-v1") || 0x00 ||
  ASCII(document_type) || 0x00 ||
  canonical_json(document without document_sha256 and signatures)
)

signature_message =
  UTF8("researchops-private-holdout-signature-v1") || 0x00 ||
  ASCII(document_type) || 0x00 || raw_32_bytes(document_sha256)

ledger_entry_sha256 = SHA256(
  UTF8("researchops-private-holdout-ledger-entry-v1") || 0x00 ||
  ASCII(ledger_id) || 0x00 ||
  canonical_json(entry without entry_sha256 and signatures)
)
```

The trust manifest is not self-trusted. The verifier also requires independently supplied values
for the freeze-request hash, candidate commitment, ledger base sequence/head, access-reservation
entry hash, and final ledger sequence/head. Passing values to the CLI proves only that the release
matches those values; it cannot prove the channel was independent or that no hidden ledger fork
exists.

Production private keys must remain outside the repository, command line, fixtures and logs. Tests
generate ephemeral Ed25519 private keys in memory and discard them.

## Offline commands

```powershell
# Protocol/schema pin, strict repository contracts, and current fail-closed state.
.\.venv\Scripts\python.exe scripts\eval_v2_private_custodian.py verify-kit `
  --project-root .

.\.venv\Scripts\python.exe scripts\eval_v2_private_custodian.py status `
  --project-root .

# Advisory point-in-time metadata check only. It does not hold a directory handle,
# authorize access, or protect a later reader from path replacement.
.\.venv\Scripts\python.exe scripts\eval_v2_private_custodian.py check-private-root `
  --project-root . `
  --private-root D:\EXTERNAL-CUSTODIAN-ROOT
```

`reserve-access` is a **synthetic-only** reference primitive. It writes a deterministic
create-if-absent freeze marker and advances an external ledger head under a fail-closed lock. A
normal process/head-update failure after marker creation leaves the tested marker consumed. It does
not prove power-loss or network-filesystem durability and never returns permission to read private
content (`private_access_may_proceed=false`). The command requires explicit
`--confirm-synthetic-consumption`; use only a disposable external registry during conformance tests.

`verify-release` requires all external anchors:

```powershell
.\.venv\Scripts\python.exe scripts\eval_v2_private_custodian.py verify-release `
  --project-root . `
  --release-dir D:\SYNTHETIC-RELEASE `
  --expected-trust-manifest-sha256 <sha256> `
  --expected-freeze-request-sha256 <sha256> `
  --expected-candidate-commitment-sha256 <sha256> `
  --expected-ledger-base-sequence <integer> `
  --expected-ledger-base-head-sha256 <sha256> `
  --expected-access-reservation-entry-sha256 <sha256> `
  --expected-ledger-final-sequence <integer> `
  --expected-ledger-final-head-sha256 <sha256>
```

The verifier makes zero network/Provider calls. It checks the eight-file allowlist before and after
bounded reads, compares directory/file identity metadata, and applies link/reparse checks, signatures,
component/candidate hashes, two-stage ledger bindings, expiry, replay within the supplied anchored
ledger, two Providers × three repetitions, precommitted metric denominators, usage/cost caps, latency
ordering, cell coverage, Wilson intervals and small-cell suppression. This is a bounded verification
snapshot, not an immutable-directory guarantee against an attacker who can keep mutating the folder.
If suppressed cells prevent exact numerator reconciliation, the result reports `bounded` rather than
claiming exact aggregate arithmetic.

Release scanning rejects schema-forbidden fields and configured sensitive patterns, including
several recursive URL/Base64/Base64URL/Base32/hex encodings. It is not a universal PII,
steganography, or hidden-ledger detector.

## Path and one-time boundaries

`check-private-root` rejects roots inside the repository/system temp, reparse ancestry and multiply
linked files at the instant of inspection. It is advisory because it does not remain attached to a
future private reader. A future non-synthetic executor must perform access in the same controlled
process through held no-follow handles and revalidate file identities. If an inspected root already
contains private files, metadata traversal itself is custodian access and must occur only inside an
authorized custody boundary.

The supplied ledger proves single use only within the supplied externally anchored ledger scope.
Global uniqueness still depends on an independent append-only/WORM witness. A local lock or signed
file alone cannot prove that the custodian did not create another registry or hide a fork.

## Data and claim boundary

Protocol 1.1 forbids pilot-participant-derived data and direct/quasi identifiers. It therefore does
not implement the pilot database's 90-day/seven-day retention route for private holdout material.
Supporting such material requires a future versioned retention/revocation contract and independent
Provider/proxy/backup deletion review; changing the corpus must invalidate its freeze.

Passing the current gate supports only this statement:

> The v1.1 schemas, Ed25519 role separation, supplied-anchor verifier, two-stage synthetic ledger,
> aggregate arithmetic and fail-closed claim boundary passed offline synthetic tests.

It does not establish a private corpus, real authorization/run, second Provider, expert review,
statistical cross-check, model quality, unknown-production generalization or SLA. See the
[custodian guide](../../../docs/PRIVATE_HOLDOUT_CUSTODIAN_KIT.md).
