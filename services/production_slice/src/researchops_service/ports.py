from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from .domain import InspectionJob, StoredObject, SubmitResult


class JobRepository(Protocol):
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
    ) -> SubmitResult: ...

    def get(self, job_id: str) -> InspectionJob | None: ...

    def healthcheck(self) -> bool: ...


class WorkQueue(Protocol):
    def claim_next(
        self, *, worker_id: str, lease_seconds: int, now: datetime
    ) -> InspectionJob | None: ...

    def begin_publishing(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        object_key: str,
        sha256: str,
        byte_size: int,
        now: datetime,
    ) -> InspectionJob: ...

    def complete(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        stored: StoredObject,
        now: datetime,
    ) -> InspectionJob: ...

    def fail_or_retry(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        error_code: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob: ...

    def mark_outcome_unknown(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob: ...

    def claim_unknown(
        self, *, worker_id: str, lease_seconds: int, now: datetime
    ) -> InspectionJob | None: ...

    def reconcile_succeeded(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        stored: StoredObject,
        now: datetime,
    ) -> InspectionJob: ...

    def reconcile_absent(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob: ...

    def release_unknown(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> InspectionJob: ...


class AggregateInspector(Protocol):
    def inspect_dataset(self, dataset_id: str) -> Mapping[str, Any]: ...


class ObjectStore(Protocol):
    def put_json(
        self, *, object_key: str, payload: bytes, sha256: str
    ) -> StoredObject: ...

    def get_json(self, *, object_key: str, expected_sha256: str) -> bytes: ...

    def head_json(self, *, object_key: str) -> StoredObject | None: ...

    def healthcheck(self) -> bool: ...
