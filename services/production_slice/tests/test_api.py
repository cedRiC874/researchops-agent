from __future__ import annotations

from fastapi.testclient import TestClient

from researchops_service.api import ServiceContainer, create_app
from researchops_service.application import InspectionApplication, InspectionWorker

from fakes import FakeInspector, FixedClock, InMemoryJobStore, InMemoryObjectStore


TOKEN = "test-api-token-with-at-least-24-characters"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DATASET = "palmer_penguins_v0_1_0"


def _client():
    clock = FixedClock()
    jobs = InMemoryJobStore()
    objects = InMemoryObjectStore()
    application = InspectionApplication(
        repository=jobs,
        object_store=objects,
        hmac_key=b"h" * 32,
        clock=clock,
    )
    worker = InspectionWorker(
        queue=jobs,
        inspector=FakeInspector(),
        object_store=objects,
        worker_id="worker-api-test",
        clock=clock,
    )
    app = create_app(
        ServiceContainer(
            application=application,
            api_token=TOKEN,
            ready_checks=(jobs.healthcheck, objects.healthcheck),
        )
    )
    return TestClient(app), worker, jobs, objects


def test_api_requires_auth_and_strict_body() -> None:
    client, _, _, _ = _client()
    assert client.post(
        "/v1/inspection-jobs",
        headers={"Idempotency-Key": "request-1001"},
        json={"dataset_id": DATASET},
    ).status_code == 401
    assert client.post(
        "/v1/inspection-jobs",
        headers={**AUTH, "Idempotency-Key": "request-1001"},
        json={"dataset_id": DATASET, "path": "C:/secret.csv"},
    ).status_code == 422


def test_api_job_lifecycle_and_result_proxy() -> None:
    client, worker, _, _ = _client()
    response = client.post(
        "/v1/inspection-jobs",
        headers={**AUTH, "Idempotency-Key": "request-1002"},
        json={"dataset_id": DATASET},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["reused"] is False
    job_id = body["job_id"]
    assert client.get(
        f"/v1/inspection-jobs/{job_id}/result", headers=AUTH
    ).status_code == 409

    worker.process_one()
    status_response = client.get(f"/v1/inspection-jobs/{job_id}", headers=AUTH)
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "succeeded"
    assert "expected_object_key" not in status_response.text

    result = client.get(f"/v1/inspection-jobs/{job_id}/result", headers=AUTH)
    assert result.status_code == 200
    assert result.json()["result_type"] == "aggregate_dataset_profile"
    assert "jobs/" not in result.text


def test_api_idempotency_and_readiness() -> None:
    client, _, jobs, _ = _client()
    headers = {**AUTH, "Idempotency-Key": "request-1003"}
    first = client.post(
        "/v1/inspection-jobs", headers=headers, json={"dataset_id": DATASET}
    )
    second = client.post(
        "/v1/inspection-jobs", headers=headers, json={"dataset_id": DATASET}
    )
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["reused"] is True
    assert client.get("/health/ready").status_code == 200
    jobs.healthy = False
    assert client.get("/health/ready").status_code == 503


def test_api_fails_closed_when_stored_result_is_tampered() -> None:
    client, worker, _, objects = _client()
    response = client.post(
        "/v1/inspection-jobs",
        headers={**AUTH, "Idempotency-Key": "request-1004"},
        json={"dataset_id": DATASET},
    )
    job_id = response.json()["job_id"]
    completed = worker.process_one()
    assert completed is not None
    objects.objects[completed.expected_object_key] = b"tampered"
    result = client.get(f"/v1/inspection-jobs/{job_id}/result", headers=AUTH)
    assert result.status_code == 502
    assert result.json()["error_code"] == "object_outcome_unknown"
