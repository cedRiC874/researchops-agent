# External review signing instructions

> No private key, seed, keystore/HSM locator, direct identity, contact detail or completed conflict
> declaration may enter this repository. These instructions define interoperable document hashes
> and signatures; they do not establish real-world identity or qualification by themselves.

## 1. Trust chain and roles

The only valid order is:

```text
public Git package anchor
  < signed pre-invitation governance anchor
  < first invitation
  < signed domain records and signed statistical execution lock
  < signed statistical comparison receipt
  < signed post-review governance closeout
  < evidence binding
  < any future model campaign result
```

All comparisons use strict `<`; equal timestamps fail closed. The external custodian keeps direct
identity, qualification, conflict, invitation and timestamp evidence. Public documents contain only
stable HMAC identity commitments, role, public key, controlled counts and hashes.

## 2. Ed25519 keys

Generate each role key with an OS CSPRNG, HSM/YubiKey or audited Ed25519 implementation. Private
material stays in external encrypted storage and is never passed through chat, email, a CLI argument
or a repository path. Export only the raw 32-byte public key.

Encode:

```text
public_key_b64 = base64(raw_32_byte_public_key)
signature_b64  = base64(raw_64_byte_signature)
```

Derive the key ID from the raw public key:

```text
digest = SHA256(
  UTF8("researchops-reviewer-key-id-v1\0")
  || raw_32_byte_public_key
)
key_id = ROLE_PREFIX || UPPER_HEX(digest[0:8])
```

Role prefixes:

```text
ERD-  domain reviewer
ERS-  statistical reviewer
ERC-  comparison verifier
ERG-  governance verifier
```

The verifier must recompute the key ID. A key ID is not an identity commitment: keys may rotate,
while the custodian's stable, domain-separated HMAC identity commitment must remain the same person
across roles and versions.

## 3. Strict JSON and canonicalization

Before hashing:

1. reject duplicate keys, NaN, Infinity, unknown fields and schema violations;
2. validate against the exact Draft 2020-12 schema from the same role delivery;
3. remove only the top-level commitment and signature fields named by that schema's
   `x-researchops-signing` object;
4. serialize the remaining body with RFC 8785 JCS;
5. prepend the exact UTF-8 domain string, including its final newline;
6. compute SHA-256.

Do not substitute ordinary pretty JSON or a generic `sort_keys` serializer for RFC 8785. Statistical
matrix values use canonical decimal strings so the signed receipts themselves contain no floating
JSON numbers.

For a schema with domain `D`, commitment field `C` and signature field `S`:

```text
body       = document with top-level C and S removed
commitment = SHA256(UTF8(D) || JCS(body))
```

Write lowercase hex `commitment` into `C`. Sign this message:

```text
UTF8("researchops-review-signature-v1\0")
|| UTF8(document_type)
|| 0x00
|| HEX_DECODE(commitment)
```

Write `base64(signature)` into `S.signature_b64` and repeat the commitment as
`S.signed_sha256`. Signer role, identity commitment, key ID, raw public key and trust/pre-governance
anchor are part of the committed body; only the signature object itself is excluded.

## 4. Document domains

| Document | Commitment domain |
| --- | --- |
| Pre-invitation governance anchor | `researchops-external-review-pre-governance-v1\n` |
| Domain review record | `researchops-domain-review-record-v2\n` |
| Statistical execution lock | `researchops-stat-xcheck-execution-lock-v1\n` |
| Statistical comparison receipt | `researchops-stat-xcheck-sanitized-receipt-v1\n` |
| Post-review governance closeout | `researchops-external-review-governance-v2\n` |

The statistical result and comparison matrix are separately hash-bound raw UTF-8 files. Their
hashes, byte counts, schemas and full 75-field coverage are committed inside the signed execution
lock and comparison receipt; this avoids a floating-point self-hash cycle.

The statistical delivery prelocks the comparator source and dependency lock. The separate
comparison-verifier delivery additionally prelocks the reference generator, eight-file reference
source manifest, 64-field reference schema and attempt-ledger schema. The comparison verifier must
rebuild the reference projection from those exact bytes; a caller-supplied replacement projection is
not trusted. The remaining 11 runtime fields come from the signed execution lock and must match the
external result's top-level runtime bindings and runtime anchor.

## 5. Verification order

1. Resolve the public Git commit/tree and top-level package commitment.
2. Recompute the role delivery commitment from its allowlisted files.
3. Verify the pre-invitation governance anchor and accepted trust anchor.
4. Recompute each signer's key ID and verify its role/key registry binding.
5. Strict-parse and schema-validate the document.
6. Recompute the document commitment using the schema's domain and exclusions.
7. Require `signed_sha256 == recomputed commitment` and verify the Ed25519 signature message.
8. Verify package, role delivery, roster, identity, qualification and conflict commitments.
9. Enforce strict timestamp order and cross-role identity separation.
10. For statistics, recompute all 75 matrix rows, coverage/counts and discrepancy projection.
11. Verify the contiguous statistical attempt ledger, predecessor links and terminal-receipt set.
12. Preserve and publish commitments for negative, failed, invalid and superseded attempts.

Any mismatch produces a terminal non-approved receipt. Never repair a signed document in place;
create a versioned successor referencing the predecessor commitment.

## 6. Public projection

The public projection may include package/role/governance/document commitments, public keys and key
IDs with consent, controlled role/expertise categories, invitation outcome counts, 120/120 and 3/3
coverage, structured decision/discrepancy counts, engine/runtime commitments and signature status.

It must not include names, contact details, organizations, CVs, qualification/conflict originals,
identity HMAC secrets, private keys, free-text notes or locators, raw R/SAS logs, local paths,
Provider/model outputs or private/non-synthetic evaluation content.

Even complete valid signatures do not automatically freeze Eval v2, register a Provider, authorize
private access/non-synthetic release or permit a model-quality/generalization claim.
