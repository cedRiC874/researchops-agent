# Domain review worksheet template

> External working document. Completed worksheets and free-text comments do not enter the repository.

Each reviewer must create exactly one row for every task in the bound 120-line public corpus, in file
order. The reviewer may refer to the public task's `prompt`, `context` and `expected` object. No
Candidate/Provider/model result may be provided alongside the task.

Required per-task fields:

| Field | Allowed values or rule |
| --- | --- |
| `task_id` | exact ID from the bound corpus |
| `dataset_id` | exact bound dataset ID |
| `scenario` | exact bound scenario |
| `dataset_design_fit` | `approve / needs_revision / reject / out_of_scope` |
| `expected_outcome_fit` | same four decisions |
| `tool_sequence_arguments_fit` | same four decisions |
| `numeric_evidence_direction_fit` | same four decisions |
| `limitations_and_language_fit` | same four decisions |
| `approval_safety_fit` | same four decisions |
| `overall_decision` | `approve / needs_revision / reject / out_of_scope` |
| `severity` | `none / minor / major / critical` |
| `confidence` | `low / medium / high` |
| `reason_codes` | zero or more controlled codes below |
| `free_text_note_ref` | external opaque reference only; never publish the note or locator |

Controlled reason codes:

```text
dataset_unit_mismatch
design_assumption_missing
repeated_measure_ignored
missingness_handling_incorrect
causal_language_overclaim
classification_continuous_mismatch
tool_sequence_incorrect
tool_argument_incorrect
numeric_direction_incorrect
tolerance_inappropriate
evidence_requirement_incomplete
required_assertion_incomplete
forbidden_assertion_incomplete
approval_boundary_incorrect
safety_boundary_incorrect
prompt_ambiguous
other_external_note_only
```

Dataset-level review must separately address:

- source/license/version and selected-asset boundary;
- row unit, missingness and identifier/sensitivity boundary;
- observational versus randomized interpretation;
- repeated-measure handling for Parkinson's data;
- classification outcome and missing marker for Cleveland;
- species stratification and missingness for Penguins.

`overall_decision=approve` requires all six axis decisions to equal `approve`. Severity cannot turn
`needs_revision`, `reject` or `out_of_scope` into approval. An approved sanitized receipt is allowed
only after both reviewers have completed the exact same 120 unique bound task IDs and all three
dataset IDs; every dataset, task overall and task axis decision equals `approve`; and counts for
needs-revision, reject, out-of-scope, unresolved major/critical issues and cross-reviewer disagreement
are all zero. Any task/golden fix creates a new corpus version and review scope. Minor issues and every
non-approved invitation outcome remain represented by external commitments; they cannot be silently
omitted.

The external record must validate against `domain_review_record.schema.json`. A schema-valid record
is still not completion evidence until a later verifier enforces exact IDs, roll-up consistency,
cross-reviewer agreement, signatures, timestamps and governance receipts.
