# T6-C implementation publication status

Snapshot: 2026-09-12. The fixed implementation passed the recorded local offline
batch-v6 regression. Remote CI is pending. This publication candidate has not
been merged or released, and the local result does not establish remote CI success.

## Publication scope and dependency

PR-B publishes 305 fixed source, test, contract, script and configuration files,
plus this page, the public [status ledger](../STATUS.md), and exact-byte Git
attributes for the nine new scripts. The implementation
bytes are the same bytes bound by the accepted batch-v6 input inventory.

The script-specific attributes retain their recorded CRLF bytes rather than
silently normalizing them during staging/checkout. Script content, source
commitments and test selection were not changed for publication.

| Area | Public entry points |
| --- | --- |
| Timed first-live and campaign runtime | [runtime package](../src/researchops_completion_timing/) |
| External admission and timed closure verification | [verifier package](../src/researchops_external_closure/) |
| Frozen timing, admission and closure rules | [timing contract](../evals/provider_completion_timing_v1/timing_contract_v1.json), [admission contract](../evals/provider_completion_admission_link_v1/admission_link_contract_v1.json), [timed closure contract](../evals/provider_completion_timed_closure_v1/contract_v1.json) |
| Current source identity | [v11 plan](../evals/phase6_deepseek_depth60_plan_v11.json), [v4 recipe](../evals/provider_completion_execution_binding_v4/execution_component_recipe_v4.json), [implementation manifest](../evals/provider_completion_execution_binding_v4/implementation_manifest_first_live_v3.json) |
| Offline checks | [tests](../tests/), [CI workflow](../.github/workflows/ci.yml) |
| Required historical replay inputs | [archive scope](diagnostics/README.md) |

PR-B depends on [PR-A #41](https://github.com/cedRiC874/researchops-agent/pull/41),
which contains the historical replay inputs and is not merged. Its fixed head is
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

## Recorded local offline acceptance

The single unfiltered batch-v6 run completed on 2026-09-12. The original local
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

Remote CI for the publication candidate is pending, including canonical
Python-patch replay. Neither the fixture PR nor the runtime PR is reported as
merged. Passing local offline checks does not close T7, register a second
Provider, authorize a Key load or Provider call, or authorize an unseen-task run.
Depth-60 remains 20/60; historical Kimi and Depth-60 attribution is not backfilled.
Actual live evidence and any further execution authority remain separate gates.
