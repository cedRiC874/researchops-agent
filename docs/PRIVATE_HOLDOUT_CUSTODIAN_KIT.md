# Eval v2 Private Holdout Custodian Guide

## Current status

Version 1.1 is an offline **synthetic conformance implementation**. It deliberately rejects every
non-synthetic release. The repository remains `design_only` with private `0/50`, Provider `1/2`, no
private corpus commitment, no access authorization, no completed external golden review, and no R/SAS
cross-check. Nothing in this kit authorizes private access or supports a performance claim.

This staged boundary is intentional: the project can review schemas, signatures, ledger mechanics,
aggregate arithmetic and failure behavior without creating a private corpus or paying for an online
evaluation.

## Roles and independent anchors

The project owner freezes public code/configuration but never receives private tasks. A freeze
authority approves one exact future campaign; a custodian holds and reviews the corpus, signs its
opaque salted commitment and executes only inside its own environment.

Authority and custodian must use different Ed25519 public keys, key IDs and anchored organization
commitments. The verifier checks those differences but cannot independently prove organizational
control. The following values must arrive through a channel independent of the release:

- trust-manifest SHA-256;
- freeze-request document SHA-256;
- full private-candidate commitment SHA-256;
- ledger base sequence/head;
- pre-access reservation entry SHA-256;
- ledger final sequence/head.

Matching caller-supplied values is not proof that the caller obtained them independently. A future
real activation therefore still needs an append-only/WORM witness and external review of the full
candidate artifacts.

## Preconditions for any future non-synthetic activation

- campaign is strictly valid and `frozen`, not `design_only`;
- at least 50 custodian-held cases and at least three reviewed non-synthetic datasets;
- at least two frozen Provider/model/transport/config identities;
- exactly three distinct precommitted orders per Provider;
- two or more qualified reviewers with conflict declarations;
- independent R or SAS statistical cross-check completed before results;
- source, prompt, tool schema, scorer, dependencies, datasets, split, reporter, sanitizer,
  Completion Telemetry, verifier, protocol/schema bundle, budget and retention policy all frozen;
- external trust/freeze/candidate/ledger anchors registered before access;
- a separately reviewed verifier version explicitly enables non-synthetic releases.

The current repository fails several of these gates. Editing booleans cannot turn the public-only
candidate into a private candidate.

## Two-stage ledger sequence

1. Authority signs `freeze_request` with `private_access_authorized=false`.
2. Custodian signs a salted `commitment_statement`; the salt, corpus locator, tasks and goldens stay
   outside the release.
3. Authority and custodian dual-sign one `authorization_grant`, binding the exact candidate, corpus,
   Provider plan, run commitment, ledger base, budget, retention policy and expiry.
4. Before private access, the custodian atomically creates and signs `access_reserved` in an external
   append-only registry. It binds freeze, candidate, corpus, grant, nonce, run, Provider, budget and
   retention commitments. Under tested normal-process filesystem semantics, a later head-update
   failure leaves the marker consumed; power-loss durability still requires an external WORM witness.
5. Any resume requires a consecutive `resume_authorized` event signed by both roles and bound to the
   same commitments; ambiguous in-flight work is not replayed.
6. A `terminal` event closes complete, stopped and aborted runs alike and binds aggregate results.
7. The custodian signs the full ledger tail, consumption receipt and release manifest.
8. The repository verifier checks the eight documents and all caller-supplied anchors offline.

The reference atomic writer in v1.1 accepts synthetic entries only and returns
`private_access_may_proceed=false`. It exists to test crash/replay semantics, not to activate a real
private run.

## Aggregate and budget gates

The commitment pre-registers each required metric's eligible denominator and anonymous dataset/
scenario partitions. A complete release must cover every eligible case for every Provider and all
three repetitions. The verifier recomputes:

- coverage, rates and Wilson 95% intervals to six decimal places;
- higher-is-better and lower-is-better metric directions;
- total execution count and inter-run stability denominators;
- P50/P95 ordering;
- model-call, input-token, output-token and cost totals against the signed budget;
- anonymous dataset/scenario cell coverage and small-cell suppression.

Cells below the frozen threshold expose no numerator, rate or confidence interval. Usage that is
unknown cannot be replaced by zero. A complete result requires complete usage/cost coverage;
budget-overrun cannot be reported as complete.

Visible cell numerators are reconciled to task success exactly when possible. With suppressed cells,
the verifier enforces lower/upper bounds and reports `bounded`; it does not label that relation exact.

## Release and path boundaries

The public release contains only strict allowlisted commitments, public Provider identities,
aggregates, coverage, timestamps, stable status fields, hashes, key IDs and signatures. It excludes
tasks, case IDs/order, seeds, prompts, goldens, rubrics, locators, paths, rows, tool payloads, raw
outputs, notes, logs and traceback.

The scanner checks the eight allowlisted files in a pre/post identity snapshot, strict schemas and
configured sensitive patterns, including common recursive encodings. It is not a universal PII,
arbitrary covert-channel or immutable-directory detector. Successful verification therefore reports
only that the bounded snapshot contained the allowlisted files and no prohibited fields or known
patterns were detected.

`check-private-root` is a point-in-time metadata preflight. It rejects repository/temp placement,
reparse ancestry and hardlinks but does not hold a handle for a later reader, so it cannot prevent a
post-check path swap. A future real executor must combine reservation and private reads in one
controlled process with no-follow handles and post-open identity checks.

## Data retention boundary

Protocol 1.1 forbids pilot-participant-derived material and direct/quasi identifiers. It does not
claim to machine-enforce the pilot database's 90-day maximum or seven-day withdrawal deletion for a
private corpus. Supporting participant-derived private material requires a future retention and
revocation schema, scheduler evidence, corpus-invalidation behavior, and independent Provider/proxy/
backup deletion attestations.

Private plaintext in synthetic conformance is attested deleted within the frozen short deadline.
That signed attestation is not independent proof of physical deletion.

## What verification proves

A valid v1.1 result proves only that one synthetic eight-file release:

- satisfies the pinned schema/protocol bundle;
- has valid signatures from the two anchored, distinct keys;
- matches all supplied external anchor values;
- contains a two-stage ledger consistent within that anchored ledger tail;
- passes aggregate, coverage, suppression and budget arithmetic;
- preserves all private/model-quality/generalization/SLA claim flags as false.

It cannot prove that the custodian disclosed every ledger, that anchors came from independent
channels, that a private corpus/golden is correct, that actual deletion occurred, or that Provider/
proxy/backup logs were purged. It authorizes no statement about private-holdout performance.
