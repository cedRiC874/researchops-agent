# T6-C implementation publication status

Publication-candidate snapshot: 2026-09-13. The current source passed the recorded
local offline regression after the CI test-path repair. This page does not claim
the subsequent PR-head or final-main CI result; consult [PR #42](https://github.com/cedRiC874/researchops-agent/pull/42)
and its Actions links for those later observations. Local success is not remote
CI success, release approval or online authority.

## Publication scope and dependency

The initial PR-B publication comprised 305 source, test, contract, script and configuration files,
plus this page, the public [status ledger](../STATUS.md), and exact-byte Git
attributes for the nine new scripts. Production source and frozen-contract
bytes remain those bound by batch-v6.
Six test files were subsequently repaired or added as described below; the older
test results are preserved with their original scope and environment.

The script-specific attributes retain their recorded CRLF bytes rather than
silently normalizing them during staging/checkout. Script content, source
commitments were not changed for publication or the test-only repair. The original
publication did not change test selection; the later full run includes the five
new regression tests and is reported separately below.

| Area | Public entry points |
| --- | --- |
| Timed first-live and campaign runtime | [runtime package](../src/researchops_completion_timing/) |
| External admission and timed closure verification | [verifier package](../src/researchops_external_closure/) |
| Frozen timing, admission and closure rules | [timing contract](../evals/provider_completion_timing_v1/timing_contract_v1.json), [admission contract](../evals/provider_completion_admission_link_v1/admission_link_contract_v1.json), [timed closure contract](../evals/provider_completion_timed_closure_v1/contract_v1.json) |
| Current source identity | [v11 plan](../evals/phase6_deepseek_depth60_plan_v11.json), [v4 recipe](../evals/provider_completion_execution_binding_v4/execution_component_recipe_v4.json), [implementation manifest](../evals/provider_completion_execution_binding_v4/implementation_manifest_first_live_v3.json) |
| Offline checks | [tests](../tests/), [CI workflow](../.github/workflows/ci.yml) |
| Required historical replay inputs | [archive scope](diagnostics/README.md) |

PR-B depends on [PR-A #41](https://github.com/cedRiC874/researchops-agent/pull/41),
which contains the historical replay inputs and was regular-merged as
`82814dba94c62bc53e8c7612b57834921b6f070f`. Its main run
[34712025726](https://github.com/cedRiC874/researchops-agent/actions/runs/34712025726)
passed all three jobs (1,091 root tests, 11 skips). Its reviewed fixed head is
`dda97aff89b383c9dfa8361819b536784ebdb71e`; its tree is
`80b0888a5460c1370322048c4b4ab65ef51afb16`. PR-B must retain those inputs for the
unchanged tests. The archive preserves earlier source bytes and superseded
contract candidates with their original defects and provenance.

The earlier external preregistration contract was merged through
[PR #39](https://github.com/cedRiC874/researchops-agent/pull/39) at
`41751bfce273449673f25f60f876bc3afa891e6b`. That contract-only merge is separate
from the current implementation publication.

## Fixed source identity

| Identity | Value |
| --- | --- |
| Current first-live plan | v11 under execution-component recipe v4 |
| Plan commitment | `7ef11c4658967e47a56d92323e6ac118dfaef57ffd699b721bac1e25d0383b8d` |
| Implementation commitment | `b4c384f5cd832f0bcbcfe1effbdf03da799aa4bd9b8f6910619ea575b487a24c` |
| Source-bundle SHA-256 | `0d4f915635fd89cb69a5bb8e29a334d21c995ccbeecdcfd0566dba8dbc2ee857` |
| v11 plan file SHA-256 | `13892cb977af1366e25c4cee586ce2dd98bd2ed3acffd8b8c0c5b5beb739383f` |
| Implementation manifest file SHA-256 | `e10331a157f8ccf6a49f98b0049c5e2e67d2dbbea0b74d3fdfa022e797f3b2a9` |
| Campaign successor | v12 not generated |

These are source-integrity commitments, not runtime admission, live evidence or
online authorization. Historical source commitments retain their original
meaning; later source does not inherit earlier execution results.

## CI test-path repair and new local acceptance

The fixed `33177744` Windows PR/push jobs each reported 2,015 tests, 24 failures,
1 error and 11 skips. These failures remain in GitHub and in the
[sanitized repair summary](evidence/t5-ci-test-path-repair-v1/summary.json):
20 loaded-module origin spelling failures, one patched-root comparison, one work
database path comparison, two fault injections that never matched, and one
historical-root containment failure. Native Windows short-path reproduction and
pass-through observations established these distinct rejection paths. The
historical case was separately reproduced from unchanged historical Git bytes;
local Git ownership rejection was not misreported as that CI root cause.

The original CI repair canonicalizes owned test-fixture paths. Injection tests retain exit
2, assert the exact open/write hit, verify eleven retained bytes and require no
second output file. Production origin, source, authorization and audit checks
were not weakened. A newly added regression initially leaked borrowed async
TestCase mocks: the 108-case related run had 19 later Kimi loop-setup failures.
That failure is retained. Synchronous helper cleanup now restores all original
network/source/transport/key-loader functions; the before/after two-case ordering
probe changed from failure to success. The affected 60-case suffix then passed;
the unchanged preceding 48 cases were retained. This is not a claim of a single
green 108-case run.

One new unfiltered root run on `c8ded362551f544ecf52e49995854c4e81fdcd7b`
completed with **2,020 tests, 0 failures, 0 errors, 16 skips**, test/wrapper exits
0/0. All 504 bound inputs, including 205 test files, matched before and after.
The actual log summary and receipt hashes were independently compared, and both
pre/post current-source plus historical-rejection verification passed. Source
and frozen contracts did not change, so no successor or formal v12 was generated.

The 16 skips are ten canonical-Python replay tests, five unavailable native
symlink tests, and one optional raw-artifact recomputation whose SHA-matching raw
input is intentionally absent from this clean publication checkout. No skip
condition was added to obtain this result; no raw model output is published to
reduce the skip count. The second authorized new local full-run slot was unused.

The pilot offline selection and production service-test directory each exited
0 in isolated, noncredential environments. Double-quiet pytest output omitted
its numeric summary; the linked JSON does not invent a reported test count.
Those checks do not replace the new-head hosted PostgreSQL/Docker E2E gates.

The new local receipt SHA-256 is
`c934e2bc72ad99c4cea46350c7fbe0c4f4eda7df6d9741351677cb99aed3a39e`;
the 450,840-byte log SHA-256 is
`fb8dded3af9210eb3cc942e5116e6520544281a03f23820c95083944e5f719f6`.
The input-manifest SHA-256 is
`5e05c4e273124440902efe93a2ba207a6422e4bfb116e4846a51e69a6a910e74`.
Raw local logs/receipts and operator paths are not published. This summary is
not the original execution receipt or independently sufficient public proof of
that local run. Subsequent documentation-only publication changes do not imply
another local full regression; their diff and input-byte equality are checked.

## Preserved batch-v6 local offline acceptance

The earlier unfiltered batch-v6 run completed on 2026-09-12. The original local
receipt reports 2,015 tests, 0 failures, 0 errors and 15 skips, with test exit 0
and wrapper exit 0. Before/after comparisons matched all 503 bound inputs,
including 204 Python test files. The separate post-run source and historical
anchor verification also passed. Publication preparation does not constitute a
second full regression or new acceptance run.

Publication-copy checks matched all 503 controlled inputs and the current v11
source-only verifier passed. A separate historical-Git check in the publication
worktree returned `external_closure_git_object_unavailable`: ordinary Git refused
that worktree for dubious ownership, while an explicitly scoped read confirmed
the required object exists. No production verifier or Git trust configuration
was weakened. This environment-specific failure is retained, not counted as a
passing publication-tree check; hosted CI on its fresh checkout remains required.

The 15 skips comprise ten historical replay tests pinned to Python 3.12.13 and
five native symlink tests unavailable to the local Windows execution token.
Skipped tests are not passing branch evidence. Test counts are unittest-reported
outcomes, not a count of unique scenarios or complete branch coverage.

The following identifiers refer to the unchanged original local files. They are
recorded for traceability; those files are retained locally and are not included
in this PR. This page is a summary, not a replacement receipt or an independently
reproducible public record of that local execution.

| Local batch-v6 record | Original SHA-256 |
| --- | --- |
| `result.json` | `8f8a92dc0fac6247cf8c821ab46170a7d8fe31beb432f807f525b960c45e08f3` |
| `unittest.log` (451,464 bytes) | `afdb3b8f6e960b9a4e36469a52a1c420fac8a7ab8889b37aeafddcb5ca67a01d` |
| `inputs.json` (503 bound inputs) | `b5d77d24bf7e258a369b20c9fd9a3ad548c51baab5d64a29c926d6c10d1c1831` |

Earlier failed runs, the prior 2,000-test result and intermediate review records
remain preserved locally. They are not overwritten, reattributed or presented
as results for the current fixed source. Full local logs, operator paths,
temporary checkpoints, email output, credentials and private task packages are
outside this publication.

## Remaining boundaries

This publication snapshot does not establish subsequent PR-B or final-main CI,
including canonical Python-patch replay. PR-A's merge and main checks are recorded
above; PR-B's later merge/check state must be verified separately. Passing local
offline checks does not close T7, register a second
Provider, authorize a Key load or Provider call, or authorize an unseen-task run.
Depth-60 remains 20/60; historical Kimi and Depth-60 attribution is not backfilled.
Actual live evidence and any further execution authority remain separate gates.
