# External roster and invitation ledger template

> This completed ledger must remain outside the repository. Only its domain-separated commitment,
> counts and an independently signed governance receipt may later enter a versioned public overlay.

Freeze before the first invitation:

- package commitment and public Git anchor;
- roster version and UTC anchor;
- intended reviewer roles and expertise coverage;
- a comparison-verifier role distinct from the statistical reviewer and Python authors;
- compensation policy;
- invitation deadline and review close;
- replacement policy;
- outcome taxonomy;
- custodian identity and independent governance verifier;
- stable identity-commitment construction.

For each person, retain privately:

- direct identity and contact channel;
- organization and qualification materials;
- who independently verified qualification;
- full conflict declaration and adjudication;
- stable identity commitment generated as
  `HMAC-SHA256(custodian_secret, domain || canonical_identity)`;
- role-specific public signing key and consent to publish it;
- deterministic role key ID (`ERD-`, `ERS-`, `ERC-` or `ERG-`) recomputed from the raw public key;
- invitation sent/received timestamps and outcome;
- completed worksheet commitment or decline/withdraw/reject/exclusion commitment.

The same custodian secret and canonical identity rule must be used across roles so one person cannot
appear as two reviewers by changing salt. The secret never enters the repository. Domain separation
must distinguish identity, organization, qualification, conflict and document commitments.

Public aggregate fields for a future overlay:

```text
roster_version
roster_commitment_sha256
invitation_ledger_commitment_sha256
invited_count
completed_count
declined_count
withdrawn_count
rejected_count
excluded_count
replacement_count
reviewer_identity_commitments_distinct
cross_role_identity_commitments_distinct
qualification_governance_receipt_sha256
conflict_governance_receipt_sha256
package_anchor_at < roster_anchor_at < receipt_at < evidence_bound_at
pre_governance_commitment_sha256
domain_delivery_commitment_sha256
statistical_delivery_commitment_sha256
```

Never publish names, email addresses, phone numbers, CVs, certificates, employer/client details,
contracts, invoices, handwritten signatures, raw conflict declarations or free-text review notes.
