import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)
_telemetry_initialized = False


def _is_enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "true").lower() != "false"


def setup_telemetry() -> None:
    global _telemetry_initialized

    if _telemetry_initialized:
        return

    if not _is_enabled():
        log.info("OpenTelemetry desabilitado via OTEL_ENABLED=false")
        return

    resource = Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "analytics-service"),
        "service.version": os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
    })
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    BotocoreInstrumentor().instrument()
    _telemetry_initialized = True


def instrument_flask(app) -> None:
    if _telemetry_initialized:
        FlaskInstrumentor().instrument_app(app)


def get_tracer():
    return trace.get_tracer(
        os.getenv("OTEL_SERVICE_NAME", "analytics-service"),
    )
