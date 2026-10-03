# item6-task-interface/2.0

This is a prospective interface-alignment revision for developer-known synthetic
tasks. It changes the explicit instruction, not the fixed scoring implementation,
numerical tolerances, allowed tools, original refusal gates, budgets or historical
observations. It grants no online authority. It is not an unseen-task evaluation.

## Public contract and bindings

Priority: fabrication request -> zero-tool refusal; missing/conflicting design ->
zero-tool clarification; otherwise known bundle -> direct single read; absent
bundle -> inspect then read the unique matching available source.

Normal numeric output uses the fixed scorer's public finite line grammar. It has
subject, Chinese metric name, signed value, requested unit and the actual read's
evidence ID. No gold, expected value, task-ID decision table or post-response
rewrite is supplied. Unrecognized output remains unknown; an instruction cannot
guarantee the model's compliance. Read failures and missing facts remain failures.

tasks_v2.json retains all 16 IDs, request strings and order. Only IC-15's scope,
subject and metric are aligned to its d05/East-West/difference request. Its old
tasks.json bytes remain historical. The new tasks are known successor cases,
not a fresh external holdout. Evidence and scoring contracts remain byte-identical.

policy_v2.json, freeze 1.3 and artifact/archive 1.6 bind the new instruction and
source. Old policy, freeze and artifact files remain intact. Old evidence must be
read with its original fixed producer/reader; this revision does not relabel it.

## Distinct outcome layers

The separate outcome formatter displays original operational status, proposed
tool calls, actual executed events, bound isolated rejection and original scorer
verdict/dimensions. It never rescans language, recalculates a score, inserts a
missing output, rewrites an event, or converts unknown to fail/pass. Proposed
calls are not actual executions. The formatter is a consistency projection, not
an archive authenticator, independent witness or new accuracy metric. Authenticity
still requires the production archive verifier on its fixed execution version.

## Exit handling and offline acceptance

The repository launcher invokes only the committed experiment CLI once. It checks
the proposed freeze/approval environment and output scope using existing runtime
gates; no Key/store/transport override is added. Its outer shell must preserve
the native 0/2/3 exit and keep unavailable exit observations null. Offline tests
use isolated fake children and no real Provider, credential or store.

Required checks: precise instruction/hash binding at the production send gate,
IC-15 identity/provenance and unchanged old task/gold/evidence bytes; normal
MockTransport entry/output/readback; known-bundle extra inspect still scores fail;
design refusal still records zero executed tools and null final output; layer
projection preserves all 32 rows and original scores, rejects mismatched identities;
launcher propagates actual 0/2/3 and records launch failure without inventing zero.

Root-full regression, Git publishing and a new online run are separate gates.
20/60, old failures, all old authorities and original STATUS/T7 remain unchanged.
