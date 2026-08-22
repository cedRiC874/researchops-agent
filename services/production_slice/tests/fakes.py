from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from researchops_service.domain import (
    IdempotencyConflict,
    InspectionJob,
    JobStatus,
    LeaseLost,
    ObjectOutcomeUnknown,
    StoredObject,
    SubmitResult,
    TransientDependencyError,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class InMemoryJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, InspectionJob] = {}
        self.by_digest: dict[str, str] = {}
        self.events: list[tuple[str, str]] = []
        self.healthy = True
        self._lock = threading.Lock()

    def create_or_get(
        self,
        *,
        actor_hash: str,
        idempotency_digest: str,
        request_sha256: str,
        dataset_id: str,
        max_attempts: int,
        traceparent: str | None,
        now: datetime,
    ) -> SubmitResult:
        with self._lock:
            existing_id = self.by_digest.get(idempotency_digest)
            if existing_id is not None:
                job = self.jobs[existing_id]
                if job.actor_hash != actor_hash:
                    raise IdempotencyConflict("actor mismatch")
                return SubmitResult(job, False)
            job = InspectionJob(
                job_id=str(uuid.uuid4()),
                actor_hash=actor_hash,
                idempotency_digest=idempotency_digest,
                request_sha256=request_sha256,
                dataset_id=dataset_id,
                status=JobStatus.QUEUED,
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                expected_object_key=None,
                expected_sha256=None,
                expected_bytes=None,
                artifact_sha256=None,
                artifact_bytes=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                version=0,
                traceparent=traceparent,
            )
            self.jobs[job.job_id] = job
            self.by_digest[idempotency_digest] = job.job_id
            self.events.append((job.job_id, "queued"))
            return SubmitResult(job, True)

    def get(self, job_id: str) -> InspectionJob | None:
        return self.jobs.get(job_id)

    def healthcheck(self) -> bool:
        return self.healthy

    def claim_next(
        self, *, worker_id: str, lease_seconds: int, now: datetime
    ) -> InspectionJob | None:
        with self._lock:
            eligible = [
                job
                for job in self.jobs.values()
                if (
                    job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}
                    and job.available_at <= now
                )
                or (
                    job.status is JobStatus.RUNNING
                    and job.lease_expires_at is not None
                    and job.lease_expires_at < now
                )
            ]
            if not eligible:
                return None
            current = sorted(eligible, key=lambda item: (item.available_at, item.created_at))[0]
            if current.attempt_count >= current.max_attempts:
                failed = replace(
                    current,
                    status=JobStatus.FAILED,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code="attempts_exhausted_after_lease_expiry",
                    updated_at=now,
                    version=current.version + 1,
                )
                self.jobs[current.job_id] = failed
                self.events.append((current.job_id, "failed"))
                return None
            claimed = replace(
                current,
                status=JobStatus.RUNNING,
                attempt_count=current.attempt_count + 1,
                lease_owner=worker_id,
                lease_token=secrets.token_hex(16),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                error_code=None,
                updated_at=now,
                version=current.version + 1,
            )
            self.jobs[current.job_id] = claimed
            self.events.append((current.job_id, "claimed"))
            return claimed

    def begin_publishing(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        object_key: str,
        sha256: str,
        byte_size: int,
        now: datetime,
    ) -> InspectionJob:
        return self._transition(
            job,
            worker_id,
            JobStatus.RUNNING,
            JobStatus.PUBLISHING,
            now,
            expected_object_key=object_key,
            expected_sha256=sha256,
            expected_bytes=byte_size,
        )

    def complete(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        stored: StoredObject,
        now: datetime,
    ) -> InspectionJob:
        if (
            stored.object_key != job.expected_object_key
            or stored.sha256 != job.expected_sha256
            or stored.byte_size != job.expected_bytes
        ):
            raise LeaseLost("receipt mismatch")
        return self._transition(
            job,
            worker_id,
            JobStatus.PUBLISHING,
            JobStatus.SUCCEEDED,
            now,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            artifact_sha256=stored.sha256,
            artifact_bytes=stored.byte_size,
            error_code=None,
        )

    def fail_or_retry(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        error_code: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob:
        status = (
            JobStatus.RETRY_WAIT
            if retryable and job.attempt_count < job.max_attempts
            else JobStatus.FAILED
        )
        return self._transition(
            job,
            worker_id,
            JobStatus.RUNNING,
            status,
            now,
            available_at=retry_at,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            error_code=error_code,
        )

    def mark_outcome_unknown(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob:
        return self._transition(
            job,
            worker_id,
            JobStatus.PUBLISHING,
            JobStatus.OUTCOME_UNKNOWN,
            now,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            available_at=retry_at,
            error_code=error_code,
        )

    def claim_unknown(
        self, *, worker_id: str, lease_seconds: int, now: datetime
    ) -> InspectionJob | None:
        with self._lock:
            eligible = [
                job
                for job in self.jobs.values()
                if (
                    job.status is JobStatus.OUTCOME_UNKNOWN
                    and job.available_at <= now
                    and (job.lease_expires_at is None or job.lease_expires_at < now)
                )
                or (
                    job.status is JobStatus.PUBLISHING
                    and job.lease_expires_at is not None
                    and job.lease_expires_at < now
                )
            ]
            if not eligible:
                return None
            current = sorted(eligible, key=lambda item: item.updated_at)[0]
            claimed = replace(
                current,
                status=JobStatus.OUTCOME_UNKNOWN,
                lease_owner=worker_id,
                lease_token=secrets.token_hex(16),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                error_code=(
                    "publishing_lease_expired"
                    if current.status is JobStatus.PUBLISHING
                    else current.error_code
                ),
                updated_at=now,
                version=current.version + 1,
            )
            self.jobs[current.job_id] = claimed
            self.events.append((current.job_id, "reconciliation_claimed"))
            return claimed

    def reconcile_succeeded(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        stored: StoredObject,
        now: datetime,
    ) -> InspectionJob:
        return self._transition(
            job,
            worker_id,
            JobStatus.OUTCOME_UNKNOWN,
            JobStatus.SUCCEEDED,
            now,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            artifact_sha256=stored.sha256,
            artifact_bytes=stored.byte_size,
            error_code=None,
        )

    def reconcile_absent(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob:
        should_retry = job.attempt_count < job.max_attempts
        return self._transition(
            job,
            worker_id,
            JobStatus.OUTCOME_UNKNOWN,
            JobStatus.RETRY_WAIT if should_retry else JobStatus.FAILED,
            now,
            available_at=retry_at,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            error_code=(
                "reconciled_object_absent"
                if should_retry
                else "attempts_exhausted_after_reconcile"
            ),
        )

    def release_unknown(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob:
        return self._transition(
            job,
            worker_id,
            JobStatus.OUTCOME_UNKNOWN,
            JobStatus.OUTCOME_UNKNOWN,
            now,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            available_at=retry_at,
            error_code=error_code,
        )

    def _transition(
        self,
        job: InspectionJob,
        worker_id: str,
        expected: JobStatus,
        target: JobStatus,
        now: datetime,
        **values,
    ) -> InspectionJob:
        with self._lock:
            current = self.jobs[job.job_id]
            if (
                current.status is not expected
                or current.version != job.version
                or current.lease_owner != worker_id
                or current.lease_token != job.lease_token
                or current.lease_expires_at is None
                or current.lease_expires_at <= now
            ):
                raise LeaseLost("lease lost")
            updated = replace(
                current,
                **values,
                status=target,
                updated_at=now,
                version=current.version + 1,
            )
            self.jobs[job.job_id] = updated
            self.events.append((job.job_id, target.value))
            return updated


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.healthy = True
        self.fail_put = False
        self.store_then_fail = False

    def put_json(
        self, *, object_key: str, payload: bytes, sha256: str
    ) -> StoredObject:
        assert hashlib.sha256(payload).hexdigest() == sha256
        if self.fail_put and not self.store_then_fail:
            raise ObjectOutcomeUnknown("unknown")
        self.objects[object_key] = payload
        if self.fail_put:
            raise ObjectOutcomeUnknown("unknown")
        return StoredObject(object_key, sha256, len(payload))

    def get_json(self, *, object_key: str, expected_sha256: str) -> bytes:
        payload = self.objects[object_key]
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ObjectOutcomeUnknown("hash mismatch")
        return payload

    def healthcheck(self) -> bool:
        return self.healthy

    def head_json(self, *, object_key: str) -> StoredObject | None:
        payload = self.objects.get(object_key)
        if payload is None:
            return None
        return StoredObject(
            object_key,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )


class FakeInspector:
    def __init__(self, payload: Mapping[str, Any] | None = None) -> None:
        self.payload = payload or {
            "schema_version": "2.0",
            "tool_name": "inspect_dataset",
            "tool_version": "test-1.0",
            "dataset": {
                "dataset_id": "palmer_penguins_v0_1_0",
                "row_count": 344,
                "column_count": 1,
                "domain": "ecology",
                "repeated_subjects": False,
                "analysis_boundaries": ["aggregate only"],
                "model_access": "aggregate_tools_only",
            },
            "profile": {
                "row_count": 344,
                "column_count": 1,
                "duplicate_row_count": 0,
                "rows_with_missing": 0,
                "complete_row_count": 344,
                "columns": [
                    {
                        "name": "species",
                        "semantic_type": "categorical",
                        "non_null_count": 344,
                        "null_count": 0,
                        "missing_rate": 0.0,
                        "unique_count": 3,
                        "unique_rate": 3 / 344,
                        "possible_identifier": False,
                    }
                ],
                "missing_patterns": [],
                "warnings": [],
            },
            "privacy": {
                "row_level_values_exposed": False,
                "sample_values_exposed": False,
                "filesystem_path_exposed": False,
                "model_access": "aggregate_tools_only",
            },
        }
        self.transient_failure = False

    def inspect_dataset(self, dataset_id: str) -> Mapping[str, Any]:
        if self.transient_failure:
            raise TransientDependencyError("temporary")
        value = json.loads(json.dumps(self.payload))
        value.setdefault("dataset", {})["dataset_id"] = dataset_id
        return value
