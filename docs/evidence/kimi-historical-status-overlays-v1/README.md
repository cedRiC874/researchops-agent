# Kimi historical status overlays v1

These machine-readable overlays clarify post-lock state without rewriting any locked Candidate,
Pilot Pack, review or failure-evidence byte. They are status and disclosure records only; they do not
authorize a Provider request, retry, Candidate execution, campaign registration, quality claim,
private evaluation or non-synthetic data.

- [`pilot_pack_v7_v8_post_lock_status.json`](pilot_pack_v7_v8_post_lock_status.json) binds Pack 7/8,
  their reviews, predecessors, task commitment and the two separately published post-lock failure
  projections. It records that each one-call observation occurred but was inherited by neither the
  Candidate nor the Pack and created no compatibility or quality result.
- [`kimi_v1_chain_linkability_disclosure.json`](kimi_v1_chain_linkability_disclosure.json) binds the
  v1 public artifact-commitment file and discloses that its event-chain head is an intentionally
  linkable opaque commitment. It is not an authorization identifier or authorization binding.

The configured supervised baseline remains Candidate v5 / Pack 6. Candidate v6/Pack 7 and Candidate
v7/Pack 8 are historical artifacts. Because PR-A adds source files after Candidate v5 was locked, the
existing full Candidate verifier continues to reject online execution until a separately locked
current successor exists.
