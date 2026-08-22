from __future__ import annotations

from fastapi import FastAPI

from .adapters.inspect_backend import CoreAggregateInspector
from .adapters.postgres import PostgresJobStore
from .adapters.s3 import S3ObjectStore
from .api import ServiceContainer, create_app as create_http_app
from .application import InspectionApplication, InspectionWorker
from .config import Settings
from .telemetry import configure_telemetry, instrument_app, instrument_engine


def build_components(settings: Settings):
    repository = PostgresJobStore(settings.database_url())
    access_key, secret_key = settings.object_credentials()
    object_store = S3ObjectStore(
        endpoint_url=settings.object_endpoint,
        region_name=settings.object_region,
        bucket=settings.object_bucket,
        access_key=access_key,
        secret_key=secret_key,
        server_side_encryption=settings.object_server_side_encryption,
    )
    inspector = CoreAggregateInspector(settings.registry_path)
    application = InspectionApplication(
        repository=repository,
        object_store=object_store,
        hmac_key=settings.idempotency_hmac_key(),
        max_attempts=settings.worker_max_attempts,
    )
    return repository, object_store, inspector, application


def create_app() -> FastAPI:
    settings = Settings()
    repository, object_store, _, application = build_components(settings)
    configure_telemetry(
        service_name="researchops-api",
        environment=settings.environment,
        otlp_http_endpoint=settings.otlp_http_endpoint,
    )
    instrument_engine(repository.engine)
    app = create_http_app(
        ServiceContainer(
            application=application,
            api_token=settings.api_token(),
            ready_checks=(repository.healthcheck, object_store.healthcheck),
            initialize=object_store.initialize_bucket,
            close=repository.close,
        )
    )
    instrument_app(app)
    return app


def create_worker(settings: Settings, worker_id: str) -> InspectionWorker:
    repository, object_store, inspector, _ = build_components(settings)
    configure_telemetry(
        service_name="researchops-worker",
        environment=settings.environment,
        otlp_http_endpoint=settings.otlp_http_endpoint,
    )
    instrument_engine(repository.engine)
    object_store.initialize_bucket()
    return InspectionWorker(
        queue=repository,
        inspector=inspector,
        object_store=object_store,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
    )
