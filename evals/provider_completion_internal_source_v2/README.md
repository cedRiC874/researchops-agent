# Internal current-tree source integrity v2 — offline only

This named successor commits the current public source/configuration/policy file
set. It is not an Internal30 run, a replacement runtime admission, an external
campaign, or a new evaluation. The original v1 source/admission/run code and the
Internal30 manifest remain unchanged. The old online gate must reject the changed
current tree; passing v2 does not authorize it to execute.

## Selection and lineage

`recipe_v2.json` declares bounded `src`/`evals` enumeration, explicit files and the
single self-manifest exclusion. Unlike v1, v2 explicitly includes the new generator,
public-derivation utility, normative behavior CONTRACT and v1.1 REVISION. The old
manifest, this recipe, the schema and validator source are committed inputs. CI
current hash literals and regression receipts are not selected, avoiding self-hash
cycles. Test/CI inputs require a separate before/after verification-input digest.

Lineage is anchored to commit `551b9e252670be871ff75b0638b033b07d3c6f08` and its fixed
tree. Validation reads Git without replacement objects or fetching, verifies the
original manifest's full bytes against a fixed digest, recomputes the v1 domain
commitment, and checks every original file row against the fixed commit's blob.
The local historical manifest must still be byte-identical. Self-reported hash
fields alone cannot establish lineage. No historical module or model is executed.

## Integration and validation

The integration base is `8bfc56e1e56dbd1161190f84ded273a5abd12273` (PR #46's
regular merge, whose actual main checks passed). The 47-file preparation checkpoint
and all older verification and failure records are retained. Historical evidence
is not relabeled as current source or as behavior-evaluator results.

`scripts/verify_pre_v6_integrity.py` uses v2 only for current offline integrity.
It also verifies the fixed Internal v1 lineage and requires the unchanged online
v1 source gate to reject the changed current tree. CI binds separate current v2
and historical v1 anchors; no runtime source/admission/run dispatcher uses v2.

After implementation and public inputs are stable, the generator requires the
explicit expected HEAD and exclusively creates `source_manifest_v2.json`. Its
generated digests are mechanically synchronized to the current CI/test anchors;
predecessor manifests and historical literals are not overwritten.

The separately authorized one-time root regression uses
`scripts/run_behavior_offline_regression_v2.py --confirm-unfiltered-offline`.
It discovers all root tests without filtering, rejects duplicate IDs, retains a
create-once start marker, and reports the discovered/run denominator and actual
failures/errors/skips. Source and verification-input hashes must match before and
after; public behavior Markdown, test/CI/driver inputs and generated source are
bound separately. Outputs live under `output/internal-offline-regression-v2/`,
outside the source selector. A failure does not authorize retry or repair.

Prepared local checks, this version's full regression, hosted PR checks and actual
merged-main checks remain distinct evidence. Source integrity v2 grants no online
execution, external acceptance or historical reattribution. STATUS/T7 and 20/60
stay unchanged.
