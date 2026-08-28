# Independent R/SAS statistical cross-check preparation

Status: `not_assigned / not_run / not_evidence`.

The external analyst receives a detached directory containing only:

```text
synthetic_trial.csv
synthetic_trial_design.json
CROSSCHECK_README.md
external_review_protocol.json
reviewer_invitation.template.md
SIGNING_INSTRUCTIONS.md
pre_invitation_governance_anchor.schema.json
anchor_spec.json
tolerance_policy.json
result_contract.json
comparison_field_universe.json
comparison_matrix.schema.json
statistical_execution_lock.schema.json
statistical_crosscheck_receipt.schema.json
eval_v2_statistical_compare.py
requirements.lock
detached_delivery_manifest.json
runtime_and_independence_attestation.template.md
package_commitments.json
```

`package_commitments.json` must be obtained from the publicly anchored repository revision and used
to verify the detached manifest before any computation. The delivery must not proactively include
repository README files, artifacts, tests, Eval goldens, Python outputs or `src/researchops`.

The input and historical reference results are already public. This procedure is therefore a
non-blinded independent reproducibility cross-check, never a blinded cross-check. The analyst must
disclose prior exposure; independence rests on a separately authored R/SAS implementation, locked
runtime/source and an external output lock before comparison.

The statistical delivery includes the frozen comparator source and dependency lock so it cannot be
replaced after output lock. It does not include the reference-projection generator or any reference
values; those are available only to the separate comparison-verifier delivery.

Required order:

1. publicly anchor this preparation package;
2. externally anchor analyst identity/qualification/conflict and role separation;
3. analyst writes and hashes the R/SAS implementation and runtime lock;
4. run once offline against the already-public, hash-locked input;
5. sign the terminal execution lock over source, runtime, output and log hashes before comparison;
6. compare the exact 75-field universe using the immutable tolerance policy;
7. have an independent comparison verifier sign the matched/discrepant/failure terminal receipt;
8. bind the governance closeout before any future campaign result and retain every predecessor;
9. never relax tolerance or suppress a discrepancy after seeing results.

The external analyst should prefer base R formulas/matrix algebra for independence. A SAS
implementation is acceptable when product version, hotfixes, procedures and covariance options are
fully locked. No Python process or Python package may participate in the external computation.

Actual scripts, identity/COI originals and raw outputs remain external until the output lock. After
comparison, a later PR may publish a reviewer-approved sanitized script, aggregate result/runtime
summary and commitments. Private/non-synthetic material never enters this package.
