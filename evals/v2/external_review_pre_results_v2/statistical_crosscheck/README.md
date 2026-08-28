# Independent R/SAS statistical cross-check preparation

Status: `not_assigned / not_run / not_evidence`.

The external analyst receives a detached directory containing only:

```text
synthetic_trial.csv
synthetic_trial_design.json
CROSSCHECK_README.md
external_review_protocol.json
anchor_spec.json
tolerance_policy.json
result_contract.json
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

Required order:

1. publicly anchor this preparation package;
2. externally anchor analyst identity/qualification/conflict and role separation;
3. analyst writes and hashes the R/SAS implementation and runtime lock;
4. run once offline against the already-public, hash-locked input;
5. lock source, runtime, output and log hashes before comparison;
6. compare using the immutable tolerance policy;
7. bind the evidence before any future campaign result;
8. retain every discrepancy and the original version; never relax tolerance after seeing results.

The external analyst should prefer base R formulas/matrix algebra for independence. A SAS
implementation is acceptable when product version, hotfixes, procedures and covariance options are
fully locked. No Python process or Python package may participate in the external computation.

Actual scripts, identity/COI originals and raw outputs remain external until the output lock. After
comparison, a later PR may publish a reviewer-approved sanitized script, aggregate result/runtime
summary and commitments. Private/non-synthetic material never enters this package.
