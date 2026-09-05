# Provider completion external preregistration design v1

Status: **frozen field-contract candidate / under PR review / not merged / not implemented / not authorized / not run**.

This contract-only change is based on `main@5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab`.
It contains the two frozen contracts, 14 schemas, contract tests, and status documentation.
Verifier implementation is being developed separately and is not included here.

Frozen local anchors:

- external preregistration design: 65,751 bytes, file SHA-256
  `3b16e6d65ec7030fb1f271b57cbde369a55fbf4f966ad57cb73e591d5333d61d`, semantic
  commitment `480a8511d35d295d8a99ba7561a19f742b189094dec589bbd8317899c1f833c8`;
- closure evidence contract: 39,575 bytes, file SHA-256
  `2c904737b2776ac1df2358df86e320e6421c6eed17b48ab97d49f7512d37e439`, semantic
  commitment `a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0`.

This package defines the future externally held preregistration envelope for one synthetic-only
completion-telemetry closure run. It contains no task text, prompt, plaintext/real-task order or
mapping, seed, golden,
scorer, storage locator, Provider response, API Key, private signing key or participant/private
data. It is not an evaluation protocol and cannot produce `task_pass`.
The envelope does persist an ordered list of opaque `PCECASE-*` handles because the runtime
denominator and audit run set must be precommitted; those handles reveal only count and order, not
task semantics or the custodian's real identifiers.

The envelope is deliberately separate from the Eval v2 private-holdout kit. The private kit fixes
50+ non-synthetic cases, two Providers, three repetitions and an R/SAS cross-check, while this
package is limited to telemetry closure on externally held synthetic tasks. The two protocols may
reuse strict JSON, domain-separated hashes, Ed25519 role separation and out-of-band anchors, but
neither protocol grants authority to the other.

## What “new/unseen” means here

The future envelope must bind a separately signed and externally anchored candidate-freeze receipt
that strictly predates the custodian's salted task bundle commitment. It must also commit an
enumerated seen-task exclusion inventory and attest exact
canonical-digest intersection count zero. The freeze authority and task custodian sign the same
envelope with different keys and different opaque organization commitments. A third, independently
identified ledger-witness key must anchor four ordered stages: candidate freeze, preregistration
freeze, authorization consumption, and the post-run closure receipt. A future one-time
authorization must bind the preregistration-freeze entry; after consumption, task release and Key
loading must wait until the independently supplied consumption-entry sequence/head is confirmed.

That evidence supports only an external attestation about ordering and exact digest overlap. It
cannot prove that no person saw the task earlier, that the custodian is organizationally
independent, that no hidden ledger fork exists, or that a semantic/translated paraphrase is novel.
It provides no evidence of task quality or production-distribution representativeness.

## Current gate

PR #38 was merged as `5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab`; its merge tree is identical
to the reviewed fixed-head tree and all post-merge `main` workflows succeeded. Those facts satisfy
only the T6-B merge prerequisite. In particular:

- Adapter-path first-live validation has not run;
- no reviewed first-live evidence or runtime registry successor exists;
- registry v2 still has `runtime_binding_allowed=false` for every entry;
- the semantic/signature/external-ledger and full outer closure verifiers are not implemented
  in this branch;
- the dedicated closure-evidence contract, candidate freeze receipt, external ledger entries and
  post-run closure receipt have schemas but no semantic implementation or real signatures
  in this branch;
- no actual envelope, external trust anchor, task commitment or one-time online grant exists.

Consequently this package cannot mint runtime authority, load a Key, release task plaintext, make a
network call, close STATUS, promote a Provider or support a quality/private/generalization claim.

The remaining sequence is:

1. obtain a separate one-time authorization and run the two-request Adapter first-live validation;
2. review its evidence and create a separately reviewed registry successor;
3. implement and review the offline preregistration, pre-receipt evaluator and final closure verifier;
4. have an external custodian commit and sign a genuinely new synthetic task bundle;
5. obtain a second, separately bound one-time authorization for the closure run.

The field contract separates the runner's actual write stage from publication disposition. A
normal complete or prefix-only artifact can be verified according to its exact writer sequence;
anything with detected sensitive content, an unavailable privacy scan, or an untrusted database
origin is quarantined and can produce only a signed failure attestation. The signed closure receipt
contains only pre-anchor eligibility. Final `closure_claim_allowed` can be returned only after the
later witness entry and its out-of-band observation are verified.

Privacy verification is limited to the declared closure bundle, signed receipts, external ledger
documents and declared project artifacts. It does not prove host-wide absence from stdout/stderr,
SDK caches, temporary files, crash dumps or unlisted paths. Likewise, local observed-cost limits
are not a Provider invoice hard cap, and Provider-side retention remains unverified.

Machine files:

- [`external_preregistration_design_contract_v1.json`](external_preregistration_design_contract_v1.json)
- [`closure_evidence_contract_v1.json`](closure_evidence_contract_v1.json)
- [`external_preregistration_envelope_v1.schema.json`](schemas/external_preregistration_envelope_v1.schema.json)
- [`candidate_freeze_receipt_v1.schema.json`](schemas/candidate_freeze_receipt_v1.schema.json)
- [`seen_task_exclusion_manifest_v1.schema.json`](schemas/seen_task_exclusion_manifest_v1.schema.json)
- [`external_trust_manifest_v1.schema.json`](schemas/external_trust_manifest_v1.schema.json)
- [`external_ledger_entry_v1.schema.json`](schemas/external_ledger_entry_v1.schema.json)
- [`authorization_grant_v1.schema.json`](schemas/authorization_grant_v1.schema.json)
- [`consumption_receipt_v1.schema.json`](schemas/consumption_receipt_v1.schema.json)
- [`closure_receipt_v1.schema.json`](schemas/closure_receipt_v1.schema.json)
- [`closure_bundle_manifest_v1.schema.json`](schemas/closure_bundle_manifest_v1.schema.json)
- [`closure_run_event_projection_v1.schema.json`](schemas/closure_run_event_projection_v1.schema.json)
- [`postrun_attested_facts_v1.schema.json`](schemas/postrun_attested_facts_v1.schema.json)
- [`external_observation_bundle_v1.schema.json`](schemas/external_observation_bundle_v1.schema.json)
- [`transport_send_payload_v1.schema.json`](schemas/transport_send_payload_v1.schema.json)
- [`unseen_case_bundle_v1.schema.json`](schemas/unseen_case_bundle_v1.schema.json)
