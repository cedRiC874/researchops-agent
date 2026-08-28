# Unified external-review invitation template

> Template only. Complete recipient identity, contact channel, deadline, compensation and secure
> delivery details outside the repository. A completed invitation is an external governance record,
> not a repository file.

Subject: Independent pre-results review of the ResearchOps Agent evaluation package

We invite you to perform one independent review role for a frozen, publicly anchored evaluation
package. This is not a request to endorse a model, Provider, product or prior result.

Select exactly one externally assigned role:

```text
domain_reviewer_a
domain_reviewer_b
statistical_reviewer
comparison_verifier
```

Every role receives the same public package commitment and a role-specific delivery commitment.
Resolve both commitments from the cited public Git commit/tree before opening the delivery. Do not
accept a digest supplied only in email or chat.

## Common conditions

- Work independently from the other reviewers and the primary Python implementation authors.
- Privately disclose employment, advisory, financial, authorship, collaboration and personal
  conflicts to the external custodian.
- Compensation, if any, is for time and must not depend on approval or a matched result.
- You may decline, withdraw, reject, report discrepancies or require a new version.
- Every invitation outcome is retained in the external ledger; successful reviews cannot hide
  declined, withdrawn, rejected, excluded or failed outcomes.
- Never include names, email addresses, phone numbers, CVs, certificates, raw conflict statements,
  free-text notes, private keys, local paths, health-data rows or private evaluation material in a
  public receipt.
- Generate and retain your own Ed25519 private key outside the repository. Publish only the raw
  public key, deterministic key ID, sanitized commitments and signature allowed by the relevant
  schema.

## Domain reviewer A or B

- Both domain slots receive an identical delivery.
- Independently review all 120 public tasks in bound file order, not a selected subset.
- Review all three datasets: Palmer Penguins, Parkinson's telemonitoring and UCI Cleveland.
- Assess dataset/design boundaries, expected outcomes, tool sequences/arguments, numeric and
  evidence direction, missing/repeated-measure handling, observational language, approvals and
  safety rules.
- The delivery excludes internal-review decisions and all Candidate/Provider/model results. If you
  previously saw any excluded public material, disclose that fact in the signed record.
- Complete the strict domain-review record and sign it only after all 120 task rows and three dataset
  rows are present. A non-approved or unresolved record remains evidence and cannot be omitted.

## Statistical reviewer

- Implement the frozen statistical specification independently in R or SAS; do not reuse Python
  source or tests.
- The input and historical reference results are public, so the accurate claim is
  `non_blinded_independent_reproducibility`, never blinded validation.
- Lock implementation, runtime, dependency and output commitments before comparison.
- Sign the execution-lock document even when execution fails or the output is invalid.
- Do not alter code, methods, field universe or tolerance after seeing comparison results.
- An independent comparison verifier—not the statistical reviewer—signs the terminal comparison
  receipt. All discrepancies and predecessor attempts remain published by commitment.

## Comparison verifier

- Receive the signed statistical execution lock only after its external output timestamp is fixed.
- Use the frozen comparator and exact 75-field universe; do not alter tolerance or the external result.
- Verify every field, summary count, discrepancy and attempt-ledger entry.
- Sign a terminal receipt for matched, discrepant, failed, invalid or outcome-unknown states; never
  omit an unfavorable or superseded attempt.
- Remain identity-distinct from the statistical reviewer, both domain reviewers and the primary
  Python implementation authors.

## Deliverables

Role-specific deliverables and schemas are listed in the domain or statistical delivery manifest.
Follow [`SIGNING_INSTRUCTIONS.md`](SIGNING_INSTRUCTIONS.md) exactly. A valid signature proves key
possession and document integrity; identity, qualification, independence and conflict status still
require the separately signed governance chain.

Repository-safe package ID: `eval-v2-external-review-pre-results-v2`.

Public Git anchor, resolved package commitment, role delivery commitment, deadline, compensation,
custodian contact and secure return channel: **complete outside the repository before sending**.
