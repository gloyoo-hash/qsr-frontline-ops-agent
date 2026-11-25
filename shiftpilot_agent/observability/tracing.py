"""Distributed tracing via OpenTelemetry for Coordinator -> Sub-Agent -> Tool spans.

Provides context propagation, span helpers, and trace ID correlation for
structured JSON logging. Supports in-memory span exporters for unit tests and
OTLP/Cloud Trace exporters in production.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, Tracer

_TRACING_CONFIGURED = False


def configure_tracing(
    service_name: str | None = None,
    exporter: SpanExporter | None = None,
    force: bool = False,
) -> TracerProvider:
    """Initialize OpenTelemetry `TracerProvider` with optional custom exporter."""
    global _TRACING_CONFIGURED
    svc_name = service_name or os.getenv("OTEL_SERVICE_NAME", "qsr-frontline-ops-agent")

    current_provider = trace.get_tracer_provider()
    if isinstance(current_provider, TracerProvider):
        if exporter is not None:
            current_provider.add_span_processor(SimpleSpanProcessor(exporter))
        _TRACING_CONFIGURED = True
        return current_provider

    resource = Resource.create({"service.name": svc_name})
    provider = TracerProvider(resource=resource)
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _TRACING_CONFIGURED = True
    return provider


def get_tracer(name: str = "shiftpilot_agent") -> Tracer:
    """Return an OpenTelemetry tracer for the given component."""
    if not _TRACING_CONFIGURED:
        configure_tracing()
    return trace.get_tracer(name)


def get_current_trace_id() -> str:
    """Return the 32-hex-char trace ID of the active span, or a zero trace ID."""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return "0" * 32


@contextmanager
def traced_span(
    span_name: str,
    attributes: dict[str, Any] | None = None,
    tracer_name: str = "shiftpilot_agent",
) -> Iterator[Span]:
    """Context manager creating an OpenTelemetry span with safe attribute coercion."""
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(span_name) as span:
        if attributes:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, str(value) if not isinstance(value, (bool, int, float, str)) else value)
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
