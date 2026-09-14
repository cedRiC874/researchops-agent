# T6-B-Internal v1 offline implementation batch

Current status (2026-09-14): the separately authorized repaired-version unfiltered root regression passed: 2,051 tests / 0 failures / 0 errors / 15 skips, actual process exit 0, stable source and verification inputs. Narrow publication may proceed to its remaining fixed-head review and CI gates. The original failed run and intermediate records below remain historical checkpoints, not rewritten successes. No online acceptance is claimed.

- Isolated worktree: `researchops-agent-t6-b-internal-v1`; branch `codex/t6-b-internal-v1`.
- Baseline: `c945d168ee084f82ab690a2f118912bf28956c27`, initially clean.
- Existing file-Key successor and AB compatibility worktrees remain paused; their modified and untracked files were not copied or changed.
- This batch owns the new `researchops_internal_telemetry` package, Internal contracts/cases, targeted integration hooks and its tests/documentation.
- The initial offline implementation grant did not authorize Git publication. Later explicit conditional authority permits only the 37-file narrow commit/push/PR and regular merge after fixed-head review and required CI; real claim and Provider calls remain unauthorized.
- The 30 synthetic cases are developer-known. They were authored without model calls or screening; they are not external unseen tasks.
- Price evidence remains pending. Tests may use explicitly synthetic price/approval/store fixtures, never real approval or current-price claims.
- Internal source-only generation and current CI migration are complete. Historical v11 and all external contracts remain unchanged.
- External validation remains deferred/incomplete; Depth-60 20/60 and STATUS/T7 remain unchanged.

Verification records will distinguish targeted tests from this version's one permitted final unfiltered root regression. Temporary Git fixture commits (if needed by execution-identity tests) are not project commits or release anchors.

## Preserved development observations

| Checkpoint | Tests | Failures | Errors | Exit | Interpretation |
|---|---:|---:|---:|---:|---|
| Initial targeted contract tests | 8 | 0 | 8 | 1 | All stopped in task JSON privacy scanning: encoded single backslash was mistaken for UNC. No dispatch. |
| JSON-aware Internal scan correction | 9 | 0 | 0 | 0 | Targeted only; genuine UNC/path/encoded-secret rejection retained. |
| First production-path integration, sandbox identity | 1 | 1 | 0 | 1 | Isolated store directory locking failed before runtime preparation/dispatch. No native Provider call. |
| Production-path integration, normal local identity | 1 | 1 | 0 | 1 | Passed temporary-store ownership, then mapping loading failed: the source selector/fixture omitted the required root `probe_out_v3.json`. Dispatch count 0. Added that runtime input to the new source commitment, not merely the test copy. |
| Corrected source closure, production entry to sealed archive | 1 | 0 | 0 | 0 | 30 actual SDK/MockTransport requests; 57.074 s. Targeted integration only, not live evidence or full regression. |
| Expanded initial integration module | 4 | 0 | 0 | 0 | 238.128 s; includes 8 injected failure subcases, expiry, duplicate claim, completed 30. Later byte-snapshot/source-hardening changes are not covered by this older result. |
| Contract/admission targeted module pair | 15 | 0 | 0 | 0 | 1.908 s; no Provider/store activity. Not a full regression. |
| Initial historical v11 replay | 2 | 0 | 1 | 1 | 40.654 s; the test helper selected the legacy 256-path reader for a 274-file profile plus its two documents. Corrected to the already-existing 320-path reader; no reader limit or historical hash was changed. |

These are uncommitted development snapshots, not source-stable release evidence. No full regression had started at those checkpoints.

## Stable-source handoff

- Source commitment: `02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`.
- Source manifest SHA-256: `671a625a1092db071dfa01bc43bed29114d3fcef46a1d54a2ac1483eba0ab0d8`.
- Final pre-anchor related batch: 95/0/0/0 tests/failures/errors/skips, exit 0, 496.957 s.
- Last Key-owner change: 2/0/0/0, exit 0, 115.715 s.
- Anchor migration tests: 3/0/0/0, exit 0, 149.391 s; local CI integrity script valid.
- Relevant pilot service checks: 10 passed, exit 0.
- These overlapping targeted results are not combined into a full-suite count.
- Full-regression driver: `scripts/run_internal_offline_regression_v1.py`; exclusive batch directory prevents duplicate launch. It records source and verification-input identities before and after the unfiltered discovery.

## Single full regression: completed, failed

- Started UTC: `2026-09-13T15:19:19.769587Z`; Python PID `34656`.
- Unfiltered root discovery; verification-input SHA-256 `5ed8c38ef4b7c4cef7ae0ffae509d24fb24d072ca06e7973bb23f3f860b42840`.
- Started marker and incremental log: `output/internal-offline-regression-v1/batch-20260913/`.
- Final `receipt.json` was not present at the initial startup check; it was produced at 2026-09-13T19:05:13.343848Z.
- Result: 2,047 tests, 1 failure, 0 errors, 15 skips, 0 expected failures, 0 unexpected successes. Actual session 25703 exit code: 1, matching the receipt.
- Elapsed: 13,554,125 ms. `source_stable=true`; before/after source commitment and verification-input identity match the start record. This is a stable failing version, not a pass.
- Skips: 5 host symlink-permission cases; 10 canonical Python 3.12.13 counterexample-replay cases. No skip guard was changed or counted as passing.
- Failure: the original admission-boundary diagnostic replay pins FIRST_LIVE but still reads current SURFACE bytes. The frozen diagnostic expected 120,430 B; the new Internal scope source is 120,490 B. See review.md for the bounded read-only diagnosis and proposed scope requiring new authorization.
- PID 34656 was later reused by an unrelated conhost process. The original Python completion was verified through its retained tool session; the reused PID was not stopped or modified.
- Do not restart or duplicate the completed run. Conditional publication authorization is not satisfied. No project Git publication, Provider call, real Key/store action or frozen-criterion change occurred.
- The user-authorized 30-minute researchops-30 monitor collected the terminal result and was then paused; app configuration and scheduling DB both report PAUSED, with next_run_at=null. No Goal continuation was resumed.

## Authorized historical replay repair

- Two implementation/test files changed: `scripts/inspect_t6c_admission_boundaries.py` and `tests/test_t6c_admission_boundaries.py`; the original diagnostic receipt was not changed.
- New explicit `--historical-inputs` restores the exact mixed snapshot: FIRST_LIVE/REGISTRY/PREDECESSOR from fixed 5f6f9cde, SURFACE from fixed c945d168. Every selected blob was compared against its recorded length/hash first. Neither commit alone is described as the original complete execution tree.
- The old `--historical-first-live` and default current-tree behaviors remain; conflicting modes reject before input IO, unavailable historical objects never fall back to current data.
- Targeted module: 9 tests, 0 failures, 0 errors, 0 skips; exit 0; 81.869 s. Includes real local-Git replay and CLI verification, not a fabricated successful reader result.
- Diagnostic receipt SHA-256 remains `cf6ffacf80a13dfe95dd8767172125933b51728a24191f0dc69924419a573976`; failed full receipt remains `8d87d10f09351972c6d2d19cbae02d57e291eb97790485ce6dca1c4e02e82491`.
- Runtime source and source manifest remain unchanged and verified. Current verification-input SHA-256 is now `51018236d9b6651c8e63bd373164b6190a5dd1e446f4e275aafa05e63045bf19`; the old full result does not bind these changed script/test bytes.
- No successor regeneration, full rerun, project commit/push/PR, Provider call, real Key/store access or automation resume. Further validation/publication remains subject to its authorization gates.

## Newly authorized repair1 full regression and supervision

The user subsequently authorized one repaired-version full regression and renewed the conditional narrow publication/30-minute supervision. This does not erase or replace the failed first run.

- Old driver bytes were preserved at `output/internal-offline-regression-v1/batch-20260913/driver.before-repair.py`; SHA-256 remains `b8559da192ca341fd1d68e7a958c74ab1a29ab2b317db764a113e0fb06ad454a`.
- The offline driver now accepts only its two named batch IDs; default remains the old batch. Both still use exclusive directory creation. The discovery and TextTestRunner ASTs are unchanged; four invalid batch inputs were rejected before source/test execution.
- New command: `python -B scripts/run_internal_offline_regression_v1.py --batch batch-20260914-repair1`, with the same offline/numerical process settings as the first run.
- Started UTC `2026-09-13T20:01:21.452431Z`; Python PID `28224`, process-start UTC `2026-09-13T20:01:18.4594269Z`; tool session `65015`.
- Driver SHA-256 `34d8846383f29e6a7aaf4a59c8e20e203474171dcc4f9ff395757992c6406f63`.
- Verification-input SHA-256 `1fe00981bcbc68a29af999517f949bbf517a90f90894efcfb82bf31bf54f71e1`; source commitment and source manifest remain unchanged.
- Results will be written only under `output/internal-offline-regression-v1/batch-20260914-repair1/`. Final counts, actual exit code and after-run stability are pending; no full pass is claimed.
- `researchops-30` was reused (not duplicated), updated to this batch and verified ACTIVE on the same main thread. Next scheduled check at configuration time: 2026-09-13T20:33:00Z / Beijing 2026-09-14 04:33.
- The explicit publication list now contains 37 files: the previous 35 plus the authorized historical diagnostic script and its test. The existing driver remains in that list. Output logs, old driver backup and local authorization/store data are excluded.
- On first scheduled observation of a qualifying pass, proceed to the authorized commit/push/PR, fixed-head review and required CI, regular merge and actual main CI. Failures or substantive conflicts stop publication; no automatic repair or third full regression. No Provider or real store actions are authorized.

## repair1 completed: local acceptance, not online or release acceptance

- Finished UTC `2026-09-14T00:02:59.317661Z`; elapsed 14,497,844 ms. The retained original session `65015` returned actual exit 0, independently matching the final receipt.
- Unfiltered root discovery: **2,051 tests / 0 failures / 0 errors / 15 skips**, 0 expected failures, 0 unexpected successes. The four additional tests relative to the failed 2,047-test run belong to the authorized historical-diagnostic extension; no test filter or denominator reduction was introduced.
- The 15 skips remain 5 host symlink-privilege cases and 10 canonical Python 3.12.13 counterexample replay cases. They are not passing tests, and their guards are unchanged. GitHub CI is still a separate pending gate.
- `full_regression_passed=true`, `source_stable=true`; before/after source commitment `02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c`, source manifest SHA-256 `671a625a1092db071dfa01bc43bed29114d3fcef46a1d54a2ac1483eba0ab0d8`, verification-input SHA-256 `1fe00981bcbc68a29af999517f949bbf517a90f90894efcfb82bf31bf54f71e1` were also rechecked read-only before publication.
- Final receipt SHA-256: `5e67715858dddcac0a9c7d9259acd2f8d56da2a66ab5c91bd9c87c5c4b76d03d`. Receipt/log stay in the local repair1 output directory, outside the explicit publication list. The original failure receipt and frozen diagnostic hashes remain unchanged.
- Safe fetch confirmed actual remote main is still `c945d168ee084f82ab690a2f118912bf28956c27`; no base-content synchronization is needed and no branch PR already exists. Final tracked/shared/history diffs were rechecked; unchanged reviewed implementation/test bytes retain their stable bindings. No unresolved local blocker remains.
- Only these two batch records receive terminal evidence updates before the narrow publication. The original implementation/targeted-work wording in the Internal package README is an earlier snapshot; this record supplies its current status. No new full run, successor, production/test edit or online action was performed.
- Still pending: explicit staged-byte/fixed-head review, new PR required checks, authorized regular merge, actual merged-main required checks. Pricing, task approval and a new online authorization remain separate later gates. Internal online acceptance is false; Depth-60 remains 20/60 and the old external milestone/STATUS/T7 remain open.
