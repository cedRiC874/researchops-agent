# Runtime and independence attestation template

> Complete and sign outside the repository before comparison and output evidence binding. Identity and
> conflict originals remain external; a later public receipt contains only commitments.

The statistical reviewer must attest:

- engine is exactly R or SAS;
- implementation was independently written without reusing Python source or tests;
- the reviewer does not claim input or result blindness because the input and historical reference
  results are public;
- whether any repository README/artifact/result had been seen previously, with an external note;
- input bytes matched the two locked SHA-256 values;
- no Python runtime or Python package participated in computation;
- execution was offline, deterministic and single-threaded unless a different thread count was
  precommitted;
- runtime version, OS, locale, timezone, BLAS/LAPACK or SAS product/hotfix state was captured;
- source, dependency/runtime lock, stdout/log and output were hashed before comparison;
- tolerance was not changed after seeing output;
- every discrepancy is retained and classified; no failed version was overwritten;
- reviewer identity differs from both domain experts and the primary Python implementation author;
- all relevant conflicts were disclosed to and adjudicated by the external governance verifier.

Required external timestamps must satisfy:

```text
package_anchor
  < statistical_reviewer_roster_anchor
  < implementation_lock
  < external_execution
  < external_output_lock
  < comparison
  < evidence_binding
  < future_model_results_start
```

Signatures prove possession of a signing key and document integrity. They do not, by themselves,
prove real-world identity, qualification, independence or absence of conflict.
