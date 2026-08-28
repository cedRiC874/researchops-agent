# Eval v2 external review pre-results package v2

This directory is a candidate-neutral preparation package. It contains no completed review,
reviewer identity, contact detail, signature, R/SAS implementation, cross-check output or private
material.

The package binds `main@b905477449938b471c4b9af84398ad6e7ba2212b` and requires both domain
reviewers to assess all 120 public tasks. This removes reviewer-side golden selection from v2.
Every invitation outcome must remain represented in an external, precommitted ledger so that two
approved receipts cannot hide declined, withdrawn, rejected or excluded reviews.

Files:

- `unified_review_manifest.json`: one public package and role-delivery mapping;
- `protocol.json`: non-authorizing v2 controls and strict event ordering;
- `content_review_manifest.json`: exact public input identities and complete-scope counts;
- `package_commitments.json`: domain-prefixed commitment over every review payload file;
- `reviewer_invitation.template.md`: one repository-safe invitation for all three reviewer roles;
- `SIGNING_INSTRUCTIONS.md`: Ed25519/JCS commitment and verification procedure;
- `domain_review_delivery_manifest.json`: identical allowlist for domain reviewer A and B;
- `domain_review_worksheet.template.md`: external structured review fields;
- `domain_review_record.schema.json`: strict external work-record shape;
- `external_roster_and_invitation_ledger.template.md`: external-only roster and outcome rules;
- `pre_invitation_governance_anchor.schema.json`: signed roster/key/COI anchor before invitation;
- `governance_receipt.schema.json`: future sanitized roster/qualification/conflict governance receipt;
- `statistical_crosscheck/`: independently verifiable detached R/SAS delivery manifest, strict result
  and comparison schemas, two-stage signed receipt, and immutable tolerance policy.
- `statistical_crosscheck/comparison_verifier_delivery_manifest.json`: separately frozen reference,
  comparator, attempt-ledger and terminal-receipt allowlist;
- `statistical_crosscheck/reference_source_manifest.json`: exact Python reference source/data/dependency
  bytes, verified before delayed imports and rechecked after reference computation;
- `scripts/eval_v2_reference_projection.py`: deterministic fixed-Python reference projection builder;
- `scripts/eval_v2_statistical_compare.py`: fixed 75-field comparator with repository-external atomic output.

The actual roster, contact details, qualification evidence, conflict declarations, free-text review
notes, private keys, R/SAS script and raw outputs must not be committed here. A future versioned
receipt layer must bind an externally timestamped roster, all invitation outcomes, independent
identity/qualification/conflict governance, and strict time inequalities.

Current status remains `not_invited / not_run / not_evidence`.

This pre-results package freezes schemas, role deliveries, reference generation and the 75-field
comparator. It does not yet contain an in-repository closeout verifier for Ed25519/JCS, trust/time
anchors, attempt-ledger chain continuity or receipt-to-matrix/discrepancy reconciliation. Those
checks are an external execution gate and must be implemented or independently supplied before any
real receipt can be accepted as evidence.
