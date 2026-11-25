"""Structured JSON logging with automatic PII redaction and trace correlation.

Uses `structlog` to emit machine-parseable JSON log lines containing:
- `timestamp` (ISO-8601 UTC)
- `level`
- `event` (including explicit `tool_intent` and `tool_outcome` events)
- `trace_id` (from active OpenTelemetry span)
- `agent_name`
- `tool_name`
- `metadata` (scrubbed via `PIIRedactor` BEFORE emission)

No raw `print()` debugging is used anywhere in the codebase.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.observability.tracing import get_current_trace_id

_CONFIGURED = False


def _inject_trace_id(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that attaches the active OpenTelemetry trace_id."""
    if "trace_id" not in event_dict or not event_dict["trace_id"]:
        event_dict["trace_id"] = get_current_trace_id()
    return event_dict


def _redact_event_dict(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that scrubs all PII before any log line is serialized."""
    return redact_pii(event_dict)


def configure_logging(level: str | None = None, force: bool = False) -> None:
    """Configure structlog for strict JSON output with PII redaction."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    log_level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.stdlib.add_log_level,
            _inject_trace_id,
            _redact_event_dict,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def get_logger(name: str = "shiftpilot") -> structlog.stdlib.BoundLogger:
    """Return a configured structured JSON logger."""
    configure_logging()
    return structlog.get_logger(name)


def log_tool_intent(
    *,
    agent_name: str,
    tool_name: str,
    intended_action: str,
    args: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit structured `event="tool_intent"` BEFORE tool execution."""
    logger = get_logger("shiftpilot.tools")
    logger.info(
        "tool_intent",
        agent_name=agent_name,
        tool_name=tool_name,
        intended_action=intended_action,
        redacted_args=redact_pii(args),
        metadata=redact_pii(metadata or {}),
    )


def log_tool_outcome(
    *,
    agent_name: str,
    tool_name: str,
    status: str,
    latency_ms: float,
    result_summary: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit structured `event="tool_outcome"` AFTER tool execution."""
    logger = get_logger("shiftpilot.tools")
    logger.info(
        "tool_outcome",
        agent_name=agent_name,
        tool_name=tool_name,
        status=status,
        latency_ms=round(latency_ms, 2),
        result_summary=redact_pii(result_summary),
        metadata=redact_pii(metadata or {}),
    )
