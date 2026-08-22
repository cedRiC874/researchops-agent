from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from researchops_service.application import InspectionApplication, InspectionWorker

from fakes import FakeInspector, FixedClock, InMemoryJobStore, InMemoryObjectStore


def test_api_traceparent_is_linked_to_worker_consumer_span() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("api.submit") as producer:
        TraceContextTextMapPropagator().inject(carrier)
        producer_trace_id = producer.get_span_context().trace_id

    clock = FixedClock()
    jobs = InMemoryJobStore()
    objects = InMemoryObjectStore()
    application = InspectionApplication(
        repository=jobs,
        object_store=objects,
        hmac_key=b"h" * 32,
        clock=clock,
    )
    submitted = application.submit(
        actor="a" * 64,
        raw_idempotency_key="trace-request-0001",
        dataset_id="palmer_penguins_v0_1_0",
        traceparent=carrier["traceparent"],
    )
    assert submitted.job.traceparent == carrier["traceparent"]
    worker = InspectionWorker(
        queue=jobs,
        inspector=FakeInspector(),
        object_store=objects,
        worker_id="worker-trace-test",
        clock=clock,
    )
    worker.process_one()
    worker_span = next(
        span for span in exporter.get_finished_spans() if span.name == "inspection.execute"
    )
    assert worker_span.context.trace_id == producer_trace_id
    assert "authorization" not in worker_span.attributes
    assert "dataset_path" not in worker_span.attributes
