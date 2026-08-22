from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ConfigDict, Field

from .application import InspectionApplication
from .auth import BearerTokenAuth
from .domain import (
    IdempotencyConflict,
    InspectionJob,
    InvalidIdempotencyKey,
    JobNotFound,
    ObjectOutcomeUnknown,
    ProductionSliceError,
    ResultNotReady,
    TransientDependencyError,
    UnsafeAggregatePayload,
)


class InspectionJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class InspectionJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    dataset_id: str
    status: str
    attempt_count: int
    max_attempts: int
    artifact_sha256: str | None
    artifact_bytes: int | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    reused: bool | None = None


@dataclass(slots=True)
class ServiceContainer:
    application: InspectionApplication
    api_token: str
    ready_checks: tuple[Callable[[], bool], ...]
    initialize: Callable[[], None] = lambda: None
    close: Callable[[], None] = lambda: None

    def ready(self) -> bool:
        return all(check() for check in self.ready_checks)


def create_app(container: ServiceContainer) -> FastAPI:
    auth = BearerTokenAuth(container.api_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        container.initialize()
        try:
            yield
        finally:
            container.close()

    app = FastAPI(
        title="ResearchOps Production Slice",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(ProductionSliceError)
    async def production_error_handler(_, exc: ProductionSliceError):
        status_code = status.HTTP_400_BAD_REQUEST
        if isinstance(exc, JobNotFound):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ResultNotReady):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, IdempotencyConflict):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, InvalidIdempotencyKey):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif isinstance(exc, TransientDependencyError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif isinstance(exc, (ObjectOutcomeUnknown, UnsafeAggregatePayload)):
            status_code = status.HTTP_502_BAD_GATEWAY
        return JSONResponse(
            status_code=status_code,
            content={"error_code": exc.code},
        )

    @app.get("/health/live", include_in_schema=False)
    def live() -> Mapping[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready(response: Response) -> Mapping[str, str]:
        if not container.ready():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        return {"status": "ready"}

    @app.post(
        "/v1/inspection-jobs",
        response_model=InspectionJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_job(
        request: InspectionJobRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        actor: str = Depends(auth),
    ) -> InspectionJobResponse:
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        result = container.application.submit(
            actor=actor,
            raw_idempotency_key=idempotency_key,
            dataset_id=request.dataset_id,
            traceparent=carrier.get("traceparent"),
        )
        return _job_response(result.job, reused=not result.created)

    @app.get(
        "/v1/inspection-jobs/{job_id}", response_model=InspectionJobResponse
    )
    def get_job(job_id: str, actor: str = Depends(auth)) -> InspectionJobResponse:
        job = container.application.get(job_id)
        _enforce_owner(job, actor)
        return _job_response(job)

    @app.get("/v1/inspection-jobs/{job_id}/result")
    def get_result(job_id: str, actor: str = Depends(auth)) -> Mapping[str, Any]:
        job = container.application.get(job_id)
        _enforce_owner(job, actor)
        return container.application.get_result(job_id)

    return app


def _enforce_owner(job: InspectionJob, actor: str) -> None:
    if job.actor_hash != actor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "job_not_found"},
        )


def _job_response(job: InspectionJob, *, reused: bool | None = None) -> InspectionJobResponse:
    return InspectionJobResponse(
        job_id=job.job_id,
        dataset_id=job.dataset_id,
        status=job.status.value,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        artifact_sha256=job.artifact_sha256,
        artifact_bytes=job.artifact_bytes,
        error_code=job.error_code,
        created_at=job.created_at,
        updated_at=job.updated_at,
        reused=reused,
    )
