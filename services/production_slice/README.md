# ResearchOps Production-Like Vertical Slice

This independent service proves one infrastructure path without changing the frozen
Eval v2 candidate:

```text
FastAPI -> PostgreSQL durable lease queue -> worker
        -> aggregate-only inspect_dataset -> private S3/MinIO object -> OTel
```

The first business operation is intentionally narrow: create an asynchronous
`inspect_dataset` job using a logical `dataset_id`, then retrieve the resulting
aggregate-only JSON through the API. It does not call an LLM, expose arbitrary paths,
publish externally, or implement approval/resume. Full approval and recovery remain
Phase 4 capabilities.

## API

- `POST /v1/inspection-jobs` requires `Authorization: Bearer ...` and
  `Idempotency-Key`; the body is exactly `{ "dataset_id": "..." }`.
- `GET /v1/inspection-jobs/{job_id}` returns status and safe metadata.
- `GET /v1/inspection-jobs/{job_id}/result` proxies a hash-verified aggregate result;
  it does not reveal the backing object key or a filesystem path.
- `GET /health/live` and `GET /health/ready` separate process liveness from dependency
  readiness.

The PostgreSQL queue uses `FOR UPDATE SKIP LOCKED`, lease tokens and compare-and-set
updates. Delivery is at-least-once, not exactly-once. Before object storage, the worker
persists the deterministic key/hash/byte intent and enters `publishing`. An uncertain
write becomes `outcome_unknown`; it is reconciled with object metadata instead of being
blindly replayed.

## Local tests

The default tests use in-memory adapters and make no network calls:

```powershell
cd services/production_slice
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
```

## Compose E2E (requires Docker)

Copy `.env.example` to `.env`, then create five local secret files under
`services/production_slice/secrets/` using the names referenced by `compose.yaml`:

```powershell
Copy-Item services/production_slice/.env.example services/production_slice/.env
docker compose -f services/production_slice/compose.yaml up --build
```

The 2026-08-22 local E2E completed successfully: migration versions 1/2, a Palmer
job reached `succeeded` in one attempt, the result was 344×8, idempotency reuse
returned the same Job ID, PostgreSQL event hashes and MinIO metadata matched, and the
API/worker Trace IDs were equal. See [evidence/e2e-20260822.json](evidence/e2e-20260822.json).

To stop the running services without deleting volumes:

```powershell
docker compose -f services/production_slice/compose.yaml down
```

Do not add `-v` unless the PostgreSQL and MinIO data volumes are intentionally being
destroyed.

The API binds only to `127.0.0.1:8080`; PostgreSQL and MinIO stay on the internal
network. The prepared Eval v2 registry directory is mounted read-only. The collector
uses a local detailed debug exporter so Trace IDs can be checked. Local MinIO does not
enable server-side encryption because no KMS is configured; set
`RESEARCHOPS_OBJECT_SERVER_SIDE_ENCRYPTION=true` only with a compatible production
object-store encryption setup.

This slice is not production-ready: Compose is single-region and lacks HA, cloud IAM,
KMS-managed secrets, TLS termination, backup/restore drills and external audit-chain
anchoring. PostgreSQL polling is suitable for an MVP at moderate throughput; the queue
port can later be replaced with a managed broker.

See [VERIFICATION.md](VERIFICATION.md) for the current test, bridge and bundle-hash
snapshot.

## One-command repeatable E2E

After `.env` and the five local secret files exist, run:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\services\production_slice\scripts\run-e2e.ps1
```

Useful options:

```powershell
# Reuse the current image without rebuilding.
.\services\production_slice\scripts\run-e2e.ps1 -SkipBuild

# Select another registered external dataset.
.\services\production_slice\scripts\run-e2e.ps1 `
  -DatasetId uci_heart_disease_cleveland_45

# Stop containers after verification but preserve volumes.
.\services\production_slice\scripts\run-e2e.ps1 -StopAfter
```

Each invocation writes a new, non-overwriting directory under
`artifacts/production_slice/e2e/` containing a sanitized summary, Compose status,
verification Markdown and file-hash manifest. It never persists the API token,
Authorization header, object response body or row-level data.

## Linux CI

The workflow at
[`../../.github/workflows/production-slice-e2e.yml`](../../.github/workflows/production-slice-e2e.yml)
runs on Ubuntu 24.04 for relevant pushes, pull requests and manual dispatches. It:

1. installs the independent exact test lock;
2. generates ephemeral random CI secrets and a deterministic synthetic 344×8 registry;
3. runs the 18 process-level contract tests;
4. builds and starts the real PostgreSQL/MinIO/OTel/API/worker Compose stack;
5. runs the same `run-e2e.ps1` and uploads only its sanitized evidence;
6. stops Compose without deleting volumes, then enforces the E2E exit status.

The workflow does not consume GitHub Secrets or provider API keys. Action dependencies
are pinned to full commit SHAs. It has been locally syntax/fixture validated but will not
execute on GitHub until the changes are committed and pushed or opened as a pull request.
