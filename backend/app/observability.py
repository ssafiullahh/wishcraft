"""Trace workflow stages and emit structured, privacy-conscious events (concept 8)."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("wishcraft.observability")
_tracer = None


def configure_observability(app: Any) -> None:
    """Enable OpenTelemetry when installed; use OTLP or an opt-in console exporter."""
    global _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError:
        log.warning("observability=otel_unavailable; using structured logs only")
        return

    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "wishcraft")})
    provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        except ImportError:
            log.warning("observability=otlp_exporter_unavailable")
    elif os.getenv("OTEL_CONSOLE_EXPORTER", "true").lower() == "true":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("wishcraft.workflows")
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        log.warning("observability=fastapi_instrumentor_unavailable")


@contextmanager
def workflow_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Use the same workflow code with or without an exporter configured."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None and isinstance(value, (str, int, float, bool)):
                span.set_attribute(key, value)
        yield span


def record_event(event: str, **fields: Any) -> None:
    """Write operational metadata only; do not pass prompts, names, or context here."""
    log.info(json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":")))
