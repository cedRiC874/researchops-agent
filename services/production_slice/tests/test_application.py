from __future__ import annotations

import json

import pytest

from researchops_service.application import InspectionApplication, InspectionWorker
from researchops_service.domain import (
    IdempotencyConflict,
    JobStatus,
    LeaseLost,
    UnsafeAggregatePayload,
)

from fakes import FakeInspector, FixedClock, InMemoryJobStore, InMemoryObjectStore


ACTOR = "a" * 64
DATASET = "palmer_penguins_v0_1_0"


def _components():
    clock = FixedClock()
    jobs = InMemoryJobStore()
    objects = InMemoryObjectStore()
    inspector = FakeInspector()
    application = InspectionApplication(
        repository=jobs,
        object_store=objects,
        hmac_key=b"h" * 32,
        clock=clock,
    )
    worker = InspectionWorker(
        queue=jobs,
        inspector=inspector,
        object_store=objects,
        worker_id="worker-test",
        clock=clock,
    )
    return clock, jobs, objects, inspector, application, worker


def test_submit_is_idempotent_and_conflict_is_fail_closed() -> None:
    _, _, _, _, application, _ = _components()
    first = application.submit(
        actor=ACTOR, raw_idempotency_key="request-0001", dataset_id=DATASET
    )
    second = application.submit(
        actor=ACTOR, raw_idempotency_key="request-0001", dataset_id=DATASET
    )
    assert first.created is True
    assert second.created is False
    assert second.job.job_id == first.job.job_id

    with pytest.raises(IdempotencyConflict):
        application.submit(
            actor=ACTOR,
            raw_idempotency_key="request-0001",
            dataset_id="uci_heart_disease_cleveland_45",
        )


def test_worker_completes_hash_bound_aggregate_result() -> None:
    _, jobs, objects, _, application, worker = _components()
    submitted = application.submit(
        actor=ACTOR, raw_idempotency_key="request-0002", dataset_id=DATASET
    )
    completed = worker.process_one()
    assert completed is not None
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.expected_object_key == (
        f"jobs/{submitted.job.job_id}/results/{completed.artifact_sha256}.json"
    )
    assert completed.artifact_bytes is not None
    result = application.get_result(submitted.job.job_id)
    assert result["dataset_id"] == DATASET
    assert result["privacy"]["row_level_data_exposed"] is False
    serialized = json.dumps(result)
    assert "file_path" not in serialized
    assert completed.expected_object_key not in serialized
    assert jobs.events[-1][1] == "succeeded"
    assert len(objects.objects) == 1


def test_second_worker_cannot_claim_an_active_lease() -> None:
    clock, jobs, _, _, application, _ = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0003", dataset_id=DATASET
    )
    claimed = jobs.claim_next(worker_id="worker-a", lease_seconds=60, now=clock())
    assert claimed is not None
    assert jobs.claim_next(worker_id="worker-b", lease_seconds=60, now=clock()) is None


def test_transient_inspection_failure_enters_retry_wait() -> None:
    _, _, _, inspector, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0004", dataset_id=DATASET
    )
    inspector.transient_failure = True
    result = worker.process_one()
    assert result is not None
    assert result.status is JobStatus.RETRY_WAIT
    assert result.error_code == "transient_dependency_error"


def test_object_write_uncertainty_is_not_blindly_retried() -> None:
    clock, jobs, objects, _, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0005", dataset_id=DATASET
    )
    objects.fail_put = True
    result = worker.process_one()
    assert result is not None
    assert result.status is JobStatus.OUTCOME_UNKNOWN
    assert result.error_code == "object_write_outcome_unknown"
    assert jobs.claim_next(worker_id="worker-b", lease_seconds=60, now=result.updated_at) is None
    clock.advance(5)
    reconciled = worker.reconcile_one()
    assert reconciled is not None
    assert reconciled.status is JobStatus.RETRY_WAIT
    assert reconciled.error_code == "reconciled_object_absent"
    clock.advance(5)
    objects.fail_put = False
    assert worker.process_one().status is JobStatus.SUCCEEDED


def test_reconcile_confirms_object_written_before_uncertain_response() -> None:
    clock, _, objects, _, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0007", dataset_id=DATASET
    )
    objects.fail_put = True
    objects.store_then_fail = True
    uncertain = worker.process_one()
    assert uncertain is not None
    assert uncertain.status is JobStatus.OUTCOME_UNKNOWN
    clock.advance(5)
    reconciled = worker.reconcile_one()
    assert reconciled is not None
    assert reconciled.status is JobStatus.SUCCEEDED
    assert reconciled.artifact_sha256 == uncertain.expected_sha256


def test_reconcile_keeps_mismatched_object_outcome_unknown() -> None:
    clock, _, objects, _, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0008", dataset_id=DATASET
    )
    objects.fail_put = True
    objects.store_then_fail = True
    uncertain = worker.process_one()
    assert uncertain is not None
    objects.objects[uncertain.expected_object_key] = b"different"
    clock.advance(5)
    reconciled = worker.reconcile_one()
    assert reconciled is not None
    assert reconciled.status is JobStatus.OUTCOME_UNKNOWN
    assert reconciled.error_code == "reconcile_object_mismatch"


def test_expired_lease_is_fenced_and_attempts_are_bounded() -> None:
    clock, jobs, _, _, application, _ = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0009", dataset_id=DATASET
    )
    first = jobs.claim_next(worker_id="worker-a", lease_seconds=60, now=clock())
    assert first is not None
    clock.advance(61)
    with pytest.raises(LeaseLost):
        jobs.fail_or_retry(
            job=first,
            worker_id="worker-a",
            error_code="late",
            retryable=True,
            retry_at=clock(),
            now=clock(),
        )
    second = jobs.claim_next(worker_id="worker-b", lease_seconds=60, now=clock())
    assert second.attempt_count == 2
    clock.advance(61)
    third = jobs.claim_next(worker_id="worker-c", lease_seconds=60, now=clock())
    assert third.attempt_count == 3
    clock.advance(61)
    assert jobs.claim_next(worker_id="worker-d", lease_seconds=60, now=clock()) is None
    assert jobs.get(first.job_id).status is JobStatus.FAILED


def test_expired_publishing_job_enters_reconciliation() -> None:
    clock, jobs, _, _, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0010", dataset_id=DATASET
    )
    claimed = jobs.claim_next(worker_id="worker-test", lease_seconds=60, now=clock())
    publishing = jobs.begin_publishing(
        job=claimed,
        worker_id="worker-test",
        object_key=f"jobs/{claimed.job_id}/results/{'a' * 64}.json",
        sha256="a" * 64,
        byte_size=10,
        now=clock(),
    )
    assert publishing.status is JobStatus.PUBLISHING
    clock.advance(61)
    reconciled = worker.reconcile_one()
    assert reconciled is not None
    assert reconciled.status is JobStatus.RETRY_WAIT
    assert reconciled.error_code == "reconciled_object_absent"


def test_unsafe_aggregate_payload_fails_without_object_write() -> None:
    _, _, objects, inspector, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0006", dataset_id=DATASET
    )
    inspector.payload = {"file_path": "C:/secret.csv"}
    result = worker.process_one()
    assert result is not None
    assert result.status is JobStatus.FAILED
    assert result.error_code == "inspection_failed"
    assert objects.objects == {}

    clock, _, objects, inspector, application, worker = _components()
    application.submit(
        actor=ACTOR, raw_idempotency_key="request-0011", dataset_id=DATASET
    )
    wrong = FakeInspector().payload
    wrong["dataset"]["dataset_id"] = "uci_heart_disease_cleveland_45"
    inspector.inspect_dataset = lambda _: wrong
    result = worker.process_one()
    assert result is not None
    assert result.status is JobStatus.FAILED
    assert objects.objects == {}
