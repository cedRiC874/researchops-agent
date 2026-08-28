# Eval v2 external review pre-results package v2

This directory is a candidate-neutral preparation package. It contains no completed review,
reviewer identity, contact detail, signature, R/SAS implementation, cross-check output or private
material.

The package binds `main@b905477449938b471c4b9af84398ad6e7ba2212b` and requires both domain
reviewers to assess all 120 public tasks. This removes reviewer-side golden selection from v2.
Every invitation outcome must remain represented in an external, precommitted ledger so that two
approved receipts cannot hide declined, withdrawn, rejected or excluded reviews.

Files:

- `protocol.json`: non-authorizing v2 controls and strict event ordering;
- `content_review_manifest.json`: exact public input identities and complete-scope counts;
- `package_commitments.json`: domain-prefixed commitment over every review payload file;
- `domain_expert_invitation.template.md`: repository-safe invitation text;
- `domain_review_worksheet.template.md`: external structured review fields;
- `domain_review_record.schema.json`: strict external work-record shape;
- `external_roster_and_invitation_ledger.template.md`: external-only roster and outcome rules;
- `governance_receipt.schema.json`: future sanitized roster/qualification/conflict governance receipt;
- `statistical_crosscheck/`: independently verifiable detached R/SAS delivery manifest, strict result
  schema and immutable tolerance policy.

The actual roster, contact details, qualification evidence, conflict declarations, free-text review
notes, private keys, R/SAS script and raw outputs must not be committed here. A future versioned
receipt layer must bind an externally timestamped roster, all invitation outcomes, independent
identity/qualification/conflict governance, and strict time inequalities.

Current status remains `not_invited / not_run / not_evidence`.
