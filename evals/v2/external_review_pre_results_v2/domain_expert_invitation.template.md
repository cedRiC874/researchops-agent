# External domain-review invitation template

> Template only. Fill names, contact details, deadline and compensation terms outside the repository.
> Do not commit the completed invitation or recipient details.

Subject: Independent pre-results review of public research-analysis tasks

We are inviting you to conduct an independent domain review of a frozen public evaluation package
for the ResearchOps Agent project. The role is to assess scientific and statistical correctness of
dataset boundaries, task specifications and public goldens. It is not a request to endorse a model,
Provider or product.

Scope:

- all 120 frozen public tasks, not a selected subset;
- Palmer Penguins observational morphometrics;
- Parkinson's telemonitoring repeated-measure data;
- UCI Cleveland observational classification data;
- expected outcome, tool plan, numeric/evidence direction, limitations, approval and safety rules.

Independence conditions:

- review the package independently of the other reviewer;
- do not inspect Candidate/Provider/model outputs, scores, token usage, cost or failure diagnostics;
- the delivery does not include `internal_review.json` or its 120/120 internal decisions; disclose
  whether those public internal decisions were previously seen;
- declare relevant employment, advisory, financial, authorship, collaboration and personal conflicts;
- disclose whether any excluded material was previously seen;
- compensation, if any, is for time and is not conditional on approval;
- you may approve, request changes, reject or withdraw.

Privacy and publication:

- identity, contact details, CV/qualification evidence, conflict declaration and free-text notes stay
  with the external custodian;
- only a pseudonymous commitment, agreed expertise categories, structured aggregate decisions,
  a public key and a signature may later be published, with your explicit consent;
- do not include patient rows, subject identifiers, private tasks, credentials or local paths.

Deliverables:

1. one structured decision for each of the 120 tasks;
2. dataset-level boundary decisions for all three datasets;
3. a conflict declaration and qualification material supplied privately to the custodian;
4. a signed sanitized receipt only after all unresolved critical/major issues are adjudicated;
5. commitments for unresolved or negative findings even when no approved receipt is produced.

Repository-safe package ID: `eval-v2-external-review-pre-results-v2`.
The completed external work record must validate against `domain_review_record.schema.json`; schema
validity alone does not mark the review completed.

Deadline, compensation, custodian contact and secure delivery channel: **complete outside repository**.
