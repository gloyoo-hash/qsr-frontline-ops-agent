"""Observability package: PII redaction, structured JSON logging, and OTel tracing."""

from shiftpilot_agent.observability.logging_config import (
    configure_logging,
    get_logger,
    log_tool_intent,
    log_tool_outcome,
)
from shiftpilot_agent.observability.pii_redactor import PIIRedactor, redact_pii
from shiftpilot_agent.observability.tracing import (
    configure_tracing,
    get_current_trace_id,
    get_tracer,
    traced_span,
)

__all__ = [
    "PIIRedactor",
    "configure_logging",
    "configure_tracing",
    "get_current_trace_id",
    "get_logger",
    "get_tracer",
    "log_tool_intent",
    "log_tool_outcome",
    "redact_pii",
    "traced_span",
]
