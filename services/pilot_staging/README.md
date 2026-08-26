# ResearchOps Pilot-Ready Staging

This service is an independent, invite-only usability pilot for external research
users. It deliberately lives outside `src/researchops/`, so the locked Eval v2
candidate is not modified.

The participant path is:

```text
one-time invite -> pseudonymous Secure/HttpOnly session -> explicit consent
  -> prepared public task -> server-side provider queue -> DLP gate
  -> answer reveal + server timer -> non-expert usability feedback -> next task
```

It uses the locked candidate commitment:

```text
1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7
```

The frozen pilot Provider identity remains DeepSeek `deepseek-v4-flash` through the
OpenAI-compatible Responses transport. Candidate v4 binds the offline-tested Anthropic Models
preflight contract but does not authorize or register Anthropic for this pilot. Changing the
pilot Provider/model still requires a new candidate and campaign. The service verifies
the candidate source commitment before its worker starts and does not modify the
frozen prompt, scorer or tool schema.

## What is implemented

- separate admin and participant authentication boundaries;
- one-time invite exchange, HMAC-only token persistence, expiry and revocation;
- browser session cookie plus CSRF token, strict Host checks and security headers;
- explicit consent receipt and immediate withdrawal block;
- six prepared tasks covering three public datasets and six scenarios;
- PostgreSQL queue with `FOR UPDATE SKIP LOCKED`, one Agent task execution per assignment,
  a hard campaign assignment budget and an online kill switch;
- Provider secret mounted only into the worker, never accepted by the page or API;
- persistent Provider latency, answer-reveal time and human-review time;
- prompt remains above the answer; answer Markdown is safely rendered in the browser;
- non-expert fields only: understandability, usefulness, confidence, expert-review
  need, obvious problem, missing information, safety concern, conditional
  clarification usefulness and notes;
- output/notes filters for credentials, email addresses, absolute paths, subject IDs
  and likely row-level tabular dumps;
- automatic campaign pause on a withheld output or user-reported safety concern;
- append-only hash-chained pilot events and task-pack integrity checks;
- aggregate-only admin summary with fail-closed claim reason codes;
- withdrawal and 90-day retention purge command.

The API does not expose expected answers, golden data, scenario labels, machine score,
Provider/model identity or other participants to the participant UI. It does not
support user data upload or participant-authored tasks.

## Local verification (no Provider call)

The application tests use in-memory adapters and a fake candidate executor. The
candidate preflight separately checks the real locked files but makes no network call:

```powershell
$env:PYTHONPATH = "services/pilot_staging/src"
.\services\production_slice\.venv\Scripts\python.exe -m pytest `
  -p no:cacheprovider services/pilot_staging/tests/test_pilot_flow.py

$env:PYTHONPATH = "services/pilot_staging/src;src"
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  services/pilot_staging/tests/test_candidate_contract.py
```

## Local Compose smoke test

Copy `.env.example` to `.env`. Create these local files under
`services/pilot_staging/secrets/`:

- `postgres_password.txt` — random database password;
- `admin_token.txt` — random value of at least 24 characters;
- `token_pepper.txt` — random value of at least 32 characters.

Keep `RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED=false`. The Provider secret is not
needed for this offline infrastructure smoke test.

```powershell
docker compose -f services/pilot_staging/compose.yaml up --build postgres migrate api
```

The page is then available only on the local machine at
`http://127.0.0.1:8090/pilot`. The page cannot yet run a task because the online kill
switch is off; that is expected and prevents accidental spend.

Bootstrap and freeze the prepared campaign from the running API (replace the image
digest only after building the exact image used for the cohort):

```powershell
.\services\production_slice\.venv\Scripts\python.exe -m pip install --no-deps -e services/pilot_staging
.\services\production_slice\.venv\Scripts\researchops-pilot-admin.exe `
  --admin-token-file services/pilot_staging/secrets/admin_token.txt `
  bootstrap `
  --project-root . `
  --deployment-git-sha <40-character-git-sha> `
  --deployment-image-digest sha256:<64-hex-image-digest>
```

For a purely local UI smoke, the two deployment arguments may be omitted. The summary
then remains fail-closed with `mixed_build_or_candidate_versions`, so it cannot be
misreported as external validation evidence.

## Enabling the real Provider

This step can spend money. It is intentionally not part of tests or startup.

1. Put the server-owned DeepSeek key in
   `services/pilot_staging/secrets/provider_api_key.txt`. Do not paste it into a
   command, page, issue, log or artifact.
2. Set `RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED=true` in the local `.env`.
3. Start the worker explicitly:

```powershell
docker compose -f services/pilot_staging/compose.yaml --profile online up -d worker
```

The API container does not mount the Provider secret. The prepared formal pack reserves
30 **Agent task executions** (five people × six assignments), and assignments are never
automatically retried or overwritten. This is not a 30-request or currency cap: one
task can require multiple model turns (the locked executor allows up to eight), while
summary schema 1.2 reports observed sums and observed/unknown attempt coverage separately
for executor model calls, model-requested tool calls and backend executions. Controlled
failures retain these counts without retaining failed output text; exceptions remain
unknown rather than becoming zero. Token/cost and total upstream HTTP-request coverage
remain unavailable, and none of these counters is model planning accuracy.
Completion failures additionally carry one of four allowlisted local observation
sources, with an independent applicable/observed/unknown coverage denominator. Historical
rows remain `unknown`; no source is inferred from an old error code. New append-only
terminal events and retention tombstones bind both the frozen v1 digest and a versioned
v2 digest, without persisting Provider bodies or raw status details. These labels are
diagnostic observations, not causal Provider root-cause claims.
The telemetry scope includes only consented, non-withdrawn participants: withdrawal
removes their attempts from both observed sums and unknown denominators. Campaign-level
safety incidents remain aggregated and can still block a claim after withdrawal, so the
observed sums must never be presented as campaign-wide billing or API-request totals.
The PostgreSQL adapter binds worker-start scope, terminal classification, stable failure
reason and the safe telemetry tuple to append-only events. Summary generation performs
bidirectional event/attempt checks and fails the artifact-integrity gate after otherwise
valid database telemetry tampering. The in-memory test adapter reports this event binding
as `not_applicable`, never as production evidence.
Before retention cascade-deletes participant attempts, PostgreSQL appends a minimal
`attempt_retention_deleted` tombstone containing only the attempt ID, final telemetry
digest, final execution classification and stable deletion reason. A separate
participant-level tombstone contains no participant ID and preserves only the aggregate
fact that a withdrawal occurred. The retention deadline wins: purge records the pre-delete
binding status, and an invalid status survives deletion and continues blocking claims.
Verified tombstones preserve integrity and withdrawal counts after lawful deletion, while
an unrecorded manual deletion remains fail-closed. The public summary reports participant
projection binding as `valid`, `invalid`, or `not_applicable`; claim-eligible PostgreSQL
evidence requires `valid`.

## Supervised 1–2 person pretest

Supervised mode is a separate, persistent campaign property. It uses the local Docker
PostgreSQL plus a Tailscale HTTPS Funnel, but every summary is permanently marked:

```text
execution_environment=supervised
supervised_pretest=true
evidence_status=supervised_external_user_pretest_only
external_validation_claim_allowed=false
```

Reading the same campaign later through a staging-configured process cannot remove
that blocker. Supervised records never enter the formal 3–5 participant / 20 interaction
claim gate. Historical `pilot_pack.supervised_v1.json` remains bound to the initial
two-person / 12-assignment campaign. Historical
`pilot_pack.supervised_v3.json` remains the completed same-participant UX regression.
The active `pilot_pack.supervised_v5.json` binds the v4 candidate while preserving
the same six public tasks and translations; no online v4 run or participant evidence
exists yet, and no predecessor result is inherited. Neither pack can be counted as a
second independent participant or aggregated with v1. The formal pack remains five ×
six / 30. These assignment limits do not claim an upstream API-request or monetary
ceiling, because one task may use multiple model turns.

Before starting:

- install Tailscale for Windows, sign in and run
  `tailscale set --hostname=researchops-pilot`; the preflight rejects other public
  certificate labels to avoid exposing a personal or organization name;
- create the four local secret files, including the server-owned Provider key file;
- keep the prepared three-dataset registry under `artifacts/self_pilot_data/run-01/`;
- commit the exact implementation first—the script rejects a dirty Git worktree;
- arrange the daily retention command before confirming the retention switch.

`-ConfirmRetentionSchedule` is an operator attestation, not a scheduler installer. Do
not pass it until the daily `--profile maintenance run --rm retention` command is
actually arranged for the supervised period.

One-command start from an Administrator PowerShell:

```powershell
.\services\pilot_staging\scripts\start-supervised.ps1 `
  -ConfirmOnline `
  -ConfirmRetentionSchedule
```

The script locates Docker Desktop and Tailscale, verifies the signed-in `.ts.net`
identity, prepares the non-secret `.env` atomically, binds the clean Git SHA, builds and
records the actual Docker image ID, verifies Secure cookies/Host allowlist/candidate,
starts PostgreSQL/migration/API/worker, waits for a locked-candidate heartbeat and API
readiness, then opens a foreground HTTPS Funnel. It only checks that the Provider secret
file exists; it never opens or prints it. Startup itself makes no model request, but it
enables paid calls when a participant clicks **查看答案**.

In a second PowerShell window, inspect the sanitized status:

```powershell
.\services\pilot_staging\scripts\status-supervised.ps1
```

Create the first campaign plus a two-hour one-time invitation:

```powershell
.\services\pilot_staging\scripts\new-invite.ps1
```

The command prints the new campaign ID and then prints the fragment invitation once;
the token is not written to a file. For the second participant, reuse the displayed
campaign ID:

```powershell
.\services\pilot_staging\scripts\new-invite.ps1 `
  -CampaignId EXT-PILOT-... `
  -TtlHours 2
```

After the session, stop the public route and containers while preserving PostgreSQL:

```powershell
.\services\pilot_staging\scripts\stop-supervised.ps1
```

The stop script stops the worker, clears its readiness heartbeat, stops the API, removes
the exact HTTPS Funnel route and runs Compose `down` without `-v`. Use the
[moderator guide](../../docs/SUPERVISED_PILOT_MODERATOR_GUIDE.md),
[recruitment checklist](../../docs/SUPERVISED_PILOT_RECRUITMENT_CHECKLIST.md) and
[single-session record](../../docs/SUPERVISED_PILOT_SESSION_RECORD.md). Each participant
sees the same six-task frozen pack and may skip a task or withdraw; skipped assignments
are non-qualifying and are never rerun.

## Creating participant invitations

Each command creates one token and prints it once. The token is put in the URL fragment,
so it is not sent in the initial HTTP request or normal reverse-proxy access log.

```powershell
.\services\production_slice\.venv\Scripts\researchops-pilot-admin.exe `
  --admin-token-file services/pilot_staging/secrets/admin_token.txt `
  invite `
  --campaign-id EXT-PILOT-... `
  --public-base https://pilot.example.org
```

Create one invitation per person and send links separately. Do not publish a common
link. Recruit 3–5 independent research users; the recommended cohort is five users ×
six tasks. Use the recruitment wording and moderator rules in
[`../../docs/EXTERNAL_RESEARCHER_PILOT_PROTOCOL.md`](../../docs/EXTERNAL_RESEARCHER_PILOT_PROTOCOL.md).

## Summary, incident resolution and retention

```powershell
# Aggregate-only summary (no notes, output body, token or participant ID).
researchops-pilot-admin summary --campaign-id EXT-PILOT-...

# Resolve a paused safety report only after technical review.
researchops-pilot-admin incidents --campaign-id EXT-PILOT-...
researchops-pilot-admin resolve-incident --campaign-id EXT-PILOT-... `
  --incident-id <uuid> --resolution dismissed

# Close only after every consented participant completed or withdrew.
researchops-pilot-admin complete --campaign-id EXT-PILOT-...

# Run daily from the staging scheduler; outputs counts only.
docker compose -f services/pilot_staging/compose.yaml `
  --profile maintenance run --rm retention
```

Retention-managed PostgreSQL participant-linked records are scheduled for deletion
within seven days after withdrawal; other managed participant records are capped at
90 days. The repository provides the count-only purge command but does not install a
scheduler or persist a purge receipt. Tailscale, Docker, Provider/reverse-proxy logs and
the separate contact sheet need their own minimization and retention configuration.
Export the final aggregate summary before the purge if it must be kept as long-term
evidence.

## External staging gate

Do **not** send the local HTTP URL to participants. Before a real participant joins,
the operator still must provide:

- a real HTTPS origin, DNS and a reverse proxy/load balancer with a fixed trusted path;
- managed or properly protected PostgreSQL with TLS verification, backup/PITR and a
  tested restore path;
- a secret manager/workload identity and key rotation procedure;
- a built image digest, immutable deployment Git SHA and rollback procedure;
- persistent, redacted telemetry and alerts without prompt, output, notes, cookies,
  Authorization or Provider secrets;
- a daily retention schedule and an incident contact;
- an ethics/IRB determination before using the activity as generalizable human-subject
  research or a publication study.

The current build intentionally adds
`operator_eligibility_adjudication_not_implemented` to every non-supervised summary.
Participant self-attestation and an offline recruitment checklist are not yet a trusted
eligibility receipt, so even otherwise sufficient formal-pilot metrics cannot enable an
external claim. A later change must add an operator-reviewed, conflict-aware,
pseudonymous eligibility record before the formal 3–5 person claim gate can open.

Set `RESEARCHOPS_PILOT_ENVIRONMENT=staging` only after the public base URL is HTTPS,
Secure cookies are enabled, PostgreSQL uses `sslmode=verify-full`, and wildcard Hosts
are absent. Configuration validation fails closed otherwise.

This remains a pilot-ready staging implementation, not production. Local Compose has
one PostgreSQL volume, no HA/PITR/KMS/TLS edge and no production SLA. A passed pilot may
only support the claim
`external_researcher_usability_on_prepared_public_data`; it does not establish domain
correctness, expert review, private-holdout or unknown-distribution generalization,
production readiness, security certification, or approval-after-pause recovery.

The exact local test and image snapshot is recorded in [VERIFICATION.md](VERIFICATION.md).

## Linux CI without a Provider key

`.github/workflows/pilot-staging-ci.yml` is configured for Ubuntu 24.04 on relevant pushes, pull
requests and manual dispatch. It installs exact dependencies, creates only three
non-Provider CI secrets and three deterministic synthetic registry entries, runs the
offline API/supervised/schema/script contracts, exercises the real PostgreSQL migration
and six-task lifecycle, then builds and starts an offline API Compose stack. It never
creates `provider_api_key.txt`, never starts the online worker and never calls a model.
Actions and container images are pinned; teardown preserves the no-`down -v` rule.
