from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from ..domain import (
    IdempotencyConflict,
    InspectionJob,
    JobStatus,
    LeaseLost,
    StoredObject,
    SubmitResult,
)


metadata = MetaData()

inspection_jobs = Table(
    "inspection_jobs",
    metadata,
    Column("job_id", String(36), primary_key=True),
    Column("actor_hash", String(64), nullable=False),
    Column("idempotency_digest", String(64), nullable=False, unique=True),
    Column("request_sha256", String(64), nullable=False),
    Column("dataset_id", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128)),
    Column("lease_token", String(64)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("expected_object_key", Text),
    Column("expected_sha256", String(64)),
    Column("expected_bytes", BigInteger),
    Column("artifact_sha256", String(64)),
    Column("artifact_bytes", BigInteger),
    Column("error_code", String(64)),
    Column("traceparent", String(55)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("version", Integer, nullable=False, default=0),
)

Index(
    "ix_inspection_jobs_queue",
    inspection_jobs.c.status,
    inspection_jobs.c.available_at,
    inspection_jobs.c.created_at,
)

job_events = Table(
    "job_events",
    metadata,
    Column("job_id", String(36), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("pk_job_events", job_events.c.job_id, job_events.c.sequence, unique=True)


class PostgresJobStore:
    """Job repository and durable PostgreSQL lease queue."""

    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            future=True,
        )

    def initialize_schema(self) -> None:
        raise RuntimeError(
            "Use migrations/0001_jobs.sql; create_all would omit production constraints."
        )

    def close(self) -> None:
        self.engine.dispose()

    def healthcheck(self) -> bool:
        try:
            with self.engine.connect() as connection:
                return connection.execute(select(func.now())).scalar_one() is not None
        except Exception:
            return False

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
        job_id = str(uuid.uuid4())
        values = {
            "job_id": job_id,
            "actor_hash": actor_hash,
            "idempotency_digest": idempotency_digest,
            "request_sha256": request_sha256,
            "dataset_id": dataset_id,
            "status": JobStatus.QUEUED.value,
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "traceparent": traceparent,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
            "version": 0,
        }
        statement = (
            insert(inspection_jobs)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[inspection_jobs.c.idempotency_digest])
            .returning(*inspection_jobs.c)
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
            created = row is not None
            if row is None:
                row = connection.execute(
                    select(inspection_jobs).where(
                        inspection_jobs.c.idempotency_digest == idempotency_digest
                    )
                ).mappings().one()
                if (
                    row["actor_hash"] != actor_hash
                    or row["request_sha256"] != request_sha256
                ):
                    raise IdempotencyConflict(
                        "同一 idempotency digest 已绑定不同请求。"
                    )
            else:
                self._append_event(
                    connection,
                    job_id=job_id,
                    event_type="queued",
                    payload={"dataset_id": dataset_id, "status": "queued"},
                    now=now,
                )
        return SubmitResult(_row_to_job(row), created)

    def get(self, job_id: str) -> InspectionJob | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(inspection_jobs).where(inspection_jobs.c.job_id == job_id)
            ).mappings().first()
        return None if row is None else _row_to_job(row)

    def claim_next(
        self, *, worker_id: str, lease_seconds: int, now: datetime
    ) -> InspectionJob | None:
        eligible = or_(
            and_(
                inspection_jobs.c.status.in_(
                    [JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value]
                ),
                inspection_jobs.c.available_at <= now,
                inspection_jobs.c.attempt_count < inspection_jobs.c.max_attempts,
            ),
            and_(
                inspection_jobs.c.status == JobStatus.RUNNING.value,
                inspection_jobs.c.lease_expires_at < now,
            ),
        )
        with self.engine.begin() as connection:
            row = connection.execute(
                select(inspection_jobs)
                .where(eligible)
                .order_by(
                    inspection_jobs.c.available_at,
                    inspection_jobs.c.created_at,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).mappings().first()
            if row is None:
                return None
            if row["attempt_count"] >= row["max_attempts"]:
                terminal = connection.execute(
                    update(inspection_jobs)
                    .where(
                        inspection_jobs.c.job_id == row["job_id"],
                        inspection_jobs.c.version == row["version"],
                    )
                    .values(
                        status=JobStatus.FAILED.value,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        error_code="attempts_exhausted_after_lease_expiry",
                        updated_at=now,
                        version=row["version"] + 1,
                    )
                    .returning(inspection_jobs.c.job_id)
                ).first()
                if terminal is not None:
                    self._append_event(
                        connection,
                        job_id=row["job_id"],
                        event_type="failed",
                        payload={"error_code": "attempts_exhausted_after_lease_expiry"},
                        now=now,
                    )
                return None
            lease_token = secrets.token_hex(16)
            updated = connection.execute(
                update(inspection_jobs)
                .where(
                    inspection_jobs.c.job_id == row["job_id"],
                    inspection_jobs.c.version == row["version"],
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    attempt_count=row["attempt_count"] + 1,
                    lease_owner=worker_id,
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    error_code=None,
                    updated_at=now,
                    version=row["version"] + 1,
                )
                .returning(*inspection_jobs.c)
            ).mappings().one()
            self._append_event(
                connection,
                job_id=row["job_id"],
                event_type="claimed",
                payload={
                    "attempt_count": updated["attempt_count"],
                    "status": "running",
                },
                now=now,
            )
            return _row_to_job(updated)

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
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.RUNNING,
            target_status=JobStatus.PUBLISHING,
            values={
                "expected_object_key": object_key,
                "expected_sha256": sha256,
                "expected_bytes": byte_size,
            },
            event_type="publishing",
            event_payload={"sha256": sha256, "byte_size": byte_size},
            now=now,
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
            job.expected_object_key != stored.object_key
            or job.expected_sha256 != stored.sha256
            or job.expected_bytes != stored.byte_size
        ):
            raise LeaseLost("Artifact receipt 与 publishing intent 不一致。")
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.PUBLISHING,
            target_status=JobStatus.SUCCEEDED,
            values={
                "artifact_sha256": stored.sha256,
                "artifact_bytes": stored.byte_size,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": None,
            },
            event_type="succeeded",
            event_payload={"sha256": stored.sha256, "byte_size": stored.byte_size},
            now=now,
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
        should_retry = retryable and job.attempt_count < job.max_attempts
        target = JobStatus.RETRY_WAIT if should_retry else JobStatus.FAILED
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.RUNNING,
            target_status=target,
            values={
                "available_at": retry_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": error_code,
            },
            event_type=target.value,
            event_payload={"error_code": error_code, "retryable": should_retry},
            now=now,
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
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.PUBLISHING,
            target_status=JobStatus.OUTCOME_UNKNOWN,
            values={
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "available_at": retry_at,
                "error_code": error_code,
            },
            event_type="outcome_unknown",
            event_payload={"error_code": error_code},
            now=now,
        )

    def claim_unknown(
        self, *, worker_id: str, lease_seconds: int, now: datetime
    ) -> InspectionJob | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                select(inspection_jobs)
                .where(
                    or_(
                        and_(
                            inspection_jobs.c.status
                            == JobStatus.OUTCOME_UNKNOWN.value,
                            inspection_jobs.c.available_at <= now,
                            or_(
                                inspection_jobs.c.lease_expires_at.is_(None),
                                inspection_jobs.c.lease_expires_at < now,
                            ),
                        ),
                        and_(
                            inspection_jobs.c.status == JobStatus.PUBLISHING.value,
                            inspection_jobs.c.lease_expires_at < now,
                        ),
                    ),
                )
                .order_by(inspection_jobs.c.updated_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).mappings().first()
            if row is None:
                return None
            lease_token = secrets.token_hex(16)
            updated = connection.execute(
                update(inspection_jobs)
                .where(
                    inspection_jobs.c.job_id == row["job_id"],
                    inspection_jobs.c.version == row["version"],
                )
                .values(
                    status=JobStatus.OUTCOME_UNKNOWN.value,
                    lease_owner=worker_id,
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    error_code=(
                        "publishing_lease_expired"
                        if row["status"] == JobStatus.PUBLISHING.value
                        else row["error_code"]
                    ),
                    updated_at=now,
                    version=row["version"] + 1,
                )
                .returning(*inspection_jobs.c)
            ).mappings().one()
            self._append_event(
                connection,
                job_id=row["job_id"],
                event_type="reconciliation_claimed",
                payload={"status": "outcome_unknown"},
                now=now,
            )
        return _row_to_job(updated)

    def reconcile_succeeded(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        stored: StoredObject,
        now: datetime,
    ) -> InspectionJob:
        if (
            job.expected_object_key != stored.object_key
            or job.expected_sha256 != stored.sha256
            or job.expected_bytes != stored.byte_size
        ):
            raise LeaseLost("Reconcile object 与 intent 不一致。")
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.OUTCOME_UNKNOWN,
            target_status=JobStatus.SUCCEEDED,
            values={
                "artifact_sha256": stored.sha256,
                "artifact_bytes": stored.byte_size,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": None,
            },
            event_type="reconciled_succeeded",
            event_payload={"sha256": stored.sha256, "byte_size": stored.byte_size},
            now=now,
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
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.OUTCOME_UNKNOWN,
            target_status=(JobStatus.RETRY_WAIT if should_retry else JobStatus.FAILED),
            values={
                "available_at": retry_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": (
                    "reconciled_object_absent"
                    if should_retry
                    else "attempts_exhausted_after_reconcile"
                ),
            },
            event_type=("reconciled_absent" if should_retry else "failed"),
            event_payload={
                "status": "retry_wait" if should_retry else "failed"
            },
            now=now,
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
        return self._transition_with_lease(
            job=job,
            worker_id=worker_id,
            expected_status=JobStatus.OUTCOME_UNKNOWN,
            target_status=JobStatus.OUTCOME_UNKNOWN,
            values={
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "available_at": retry_at,
                "error_code": error_code,
            },
            event_type="reconciliation_deferred",
            event_payload={"error_code": error_code},
            now=now,
        )

    def _transition_with_lease(
        self,
        *,
        job: InspectionJob,
        worker_id: str,
        expected_status: JobStatus,
        target_status: JobStatus,
        values: Mapping[str, Any],
        event_type: str,
        event_payload: Mapping[str, Any],
        now: datetime,
    ) -> InspectionJob:
        with self.engine.begin() as connection:
            updated = connection.execute(
                update(inspection_jobs)
                .where(
                    inspection_jobs.c.job_id == job.job_id,
                    inspection_jobs.c.status == expected_status.value,
                    inspection_jobs.c.lease_owner == worker_id,
                    inspection_jobs.c.lease_token == job.lease_token,
                    inspection_jobs.c.lease_expires_at.is_not(None),
                    inspection_jobs.c.lease_expires_at > now,
                    inspection_jobs.c.version == job.version,
                )
                .values(
                    **dict(values),
                    status=target_status.value,
                    updated_at=now,
                    version=job.version + 1,
                )
                .returning(*inspection_jobs.c)
            ).mappings().first()
            if updated is None:
                raise LeaseLost("Job lease/version 已失效。")
            self._append_event(
                connection,
                job_id=job.job_id,
                event_type=event_type,
                payload=dict(event_payload),
                now=now,
            )
        return _row_to_job(updated)

    def _append_event(
        self,
        connection: Connection,
        *,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        previous = connection.execute(
            select(job_events.c.sequence, job_events.c.event_hash)
            .where(job_events.c.job_id == job_id)
            .order_by(job_events.c.sequence.desc())
            .with_for_update()
            .limit(1)
        ).mappings().first()
        sequence = 0 if previous is None else int(previous["sequence"]) + 1
        previous_hash = "0" * 64 if previous is None else str(previous["event_hash"])
        event_hash = _event_hash(
            job_id=job_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            previous_hash=previous_hash,
        )
        connection.execute(
            job_events.insert().values(
                job_id=job_id,
                sequence=sequence,
                event_type=event_type,
                payload=dict(payload),
                previous_hash=previous_hash,
                event_hash=event_hash,
                created_at=now,
            )
        )


def _row_to_job(row: Mapping[str, Any]) -> InspectionJob:
    return InspectionJob(
        job_id=str(row["job_id"]),
        actor_hash=str(row["actor_hash"]),
        idempotency_digest=str(row["idempotency_digest"]),
        request_sha256=str(row["request_sha256"]),
        dataset_id=str(row["dataset_id"]),
        status=JobStatus(str(row["status"])),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        expected_object_key=row["expected_object_key"],
        expected_sha256=row["expected_sha256"],
        expected_bytes=row["expected_bytes"],
        artifact_sha256=row["artifact_sha256"],
        artifact_bytes=row["artifact_bytes"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=int(row["version"]),
        traceparent=row["traceparent"],
    )


def _event_hash(
    *,
    job_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    previous_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "event_type": event_type,
            "job_id": job_id,
            "payload": dict(payload),
            "previous_hash": previous_hash,
            "sequence": sequence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
