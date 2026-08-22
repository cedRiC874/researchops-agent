from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RETRY_WAIT = "retry_wait"
    RUNNING = "running"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class InspectionJob:
    job_id: str
    actor_hash: str
    idempotency_digest: str
    request_sha256: str
    dataset_id: str
    status: JobStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    expected_object_key: str | None
    expected_sha256: str | None
    expected_bytes: int | None
    artifact_sha256: str | None
    artifact_bytes: int | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    job: InspectionJob
    created: bool


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    sha256: str
    byte_size: int


class ProductionSliceError(RuntimeError):
    code = "production_slice_error"


class InvalidLogicalId(ProductionSliceError):
    code = "invalid_logical_dataset_id"


class InvalidIdempotencyKey(ProductionSliceError):
    code = "invalid_idempotency_key"


class IdempotencyConflict(ProductionSliceError):
    code = "idempotency_conflict"


class JobNotFound(ProductionSliceError):
    code = "job_not_found"


class ResultNotReady(ProductionSliceError):
    code = "result_not_ready"


class LeaseLost(ProductionSliceError):
    code = "lease_lost"


class UnsafeAggregatePayload(ProductionSliceError):
    code = "unsafe_aggregate_payload"


class TransientDependencyError(ProductionSliceError):
    code = "transient_dependency_error"


class ObjectOutcomeUnknown(ProductionSliceError):
    code = "object_outcome_unknown"
