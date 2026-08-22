from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.engine import Engine


def configure_telemetry(
    *, service_name: str, environment: str, otlp_http_endpoint: str | None
):
    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "deployment.environment.name": environment,
                }
            )
        )
        if otlp_http_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_http_endpoint))
            )
        trace.set_tracer_provider(provider)
    return trace.get_tracer("researchops.production_slice", "0.1.0")


def instrument_app(app: FastAPI) -> None:
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/health/live,/health/ready",
    )


def instrument_engine(engine: Engine) -> None:
    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        enable_commenter=False,
    )
