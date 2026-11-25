"""Shared tool infrastructure: guided error builder, HITL approval registry, and instrumentation.

Provides:
- `build_guided_error`: converts Pydantic validation failures or domain errors into
  structured `ToolErrorResponse` dicts so tools NEVER crash.
- `HITLApprovalRegistry`: generates and validates one-time `PENDING_MANAGER_APPROVAL`
  tokens for high-stakes actions.
- `instrument_tool`: wraps every tool execution with:
  1. OpenTelemetry span (`tool.<tool_name>`)
  2. Structured `event="tool_intent"` log BEFORE execution (PII-redacted)
  3. Latency measurement
  4. Structured `event="tool_outcome"` log AFTER execution (PII-redacted)
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from shiftpilot_agent.observability.logging_config import (
    log_tool_intent,
    log_tool_outcome,
)
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.observability.tracing import traced_span
from shiftpilot_agent.schemas.models import HITLPendingResponse, ToolErrorResponse

T = TypeVar("T")


class HITLApprovalRegistry:
    """In-memory + deterministic token registry for Human-in-the-Loop approvals."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._approved: set[str] = set()

    def create_pending_approval(
        self,
        *,
        action_type: str,
        reason: str,
        proposed_details: dict[str, Any],
        manager_instructions: str,
    ) -> dict[str, Any]:
        """Register a high-stakes action and return a `PENDING_MANAGER_APPROVAL` payload."""
        digest = hashlib.sha256(
            f"{action_type}:{sorted(proposed_details.items())}:{secrets.token_hex(4)}".encode()
        ).hexdigest()[:10].upper()
        token = f"MGR-APPROVE-{digest}"
        self._pending[token] = {
            "action_type": action_type,
            "proposed_details": redact_pii(proposed_details),
        }
        response = HITLPendingResponse(
            approval_token=token,
            action_type=action_type,
            reason=reason,
            proposed_details=redact_pii(proposed_details),
            manager_instructions=manager_instructions,
        )
        return response.model_dump()

    def approve_token(self, token: str) -> bool:
        """Explicitly mark a pending token as approved by a human shift manager."""
        if token in self._pending:
            self._approved.add(token)
            return True
        return False

    def consume_approval(self, token: str | None, action_type: str) -> bool:
        """Verify and consume an approved token (or explicitly confirmed token)."""
        if not token:
            return False
        entry = self._pending.get(token)
        if entry is None:
            return False
        if entry["action_type"] != action_type:
            return False
        if token not in self._approved:
            return False
        self._approved.discard(token)
        self._pending.pop(token, None)
        return True

    def is_pending(self, token: str) -> bool:
        return token in self._pending

    def clear(self) -> None:
        self._pending.clear()
        self._approved.clear()


HITL_REGISTRY = HITLApprovalRegistry()


def build_guided_error(
    *,
    error_code: str,
    message: str,
    recovery_instructions: str,
    valid_options: list[str] | None = None,
) -> dict[str, Any]:
    """Return a structured, LLM-actionable error dictionary (never raises)."""
    return ToolErrorResponse(
        error_code=error_code,
        message=message,
        recovery_instructions=recovery_instructions,
        valid_options=valid_options or [],
    ).model_dump()


def format_validation_error(
    exc: ValidationError,
    tool_name: str,
    recovery_hint: str,
    valid_options: list[str] | None = None,
) -> dict[str, Any]:
    """Convert a Pydantic v2 ValidationError into a guided `ToolErrorResponse`."""
    details = "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
        for err in exc.errors()
    )
    return build_guided_error(
        error_code="INVALID_INPUT_SCHEMA",
        message=f"Validation failed for `{tool_name}`: {details}",
        recovery_instructions=recovery_hint,
        valid_options=valid_options,
    )


def execute_instrumented_tool(
    *,
    agent_name: str,
    tool_name: str,
    intended_action: str,
    raw_args: dict[str, Any],
    handler: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Execute a tool with OTel tracing, intent/outcome logging, and crash protection."""
    start = time.perf_counter()
    safe_args = redact_pii(raw_args)

    with traced_span(
        f"tool.{tool_name}",
        attributes={
            "agent.name": agent_name,
            "tool.name": tool_name,
            "tool.intended_action": intended_action,
        },
    ) as span:
        log_tool_intent(
            agent_name=agent_name,
            tool_name=tool_name,
            intended_action=intended_action,
            args=safe_args,
        )
        try:
            result = handler()
        except ValidationError as exc:
            result = format_validation_error(
                exc,
                tool_name=tool_name,
                recovery_hint=(
                    f"Fix the invalid parameters for `{tool_name}` according to the schema "
                    "and retry the tool call."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            result = build_guided_error(
                error_code="UNEXPECTED_TOOL_ERROR",
                message=f"Tool `{tool_name}` encountered an internal error: {type(exc).__name__}",
                recovery_instructions=(
                    "Verify input arguments, retry once if transient, or escalate to the "
                    "Shift Manager if the issue persists."
                ),
            )

        latency_ms = (time.perf_counter() - start) * 1000.0
        status = str(result.get("status", "unknown"))
        summary = (
            str(result.get("message") or result.get("reason") or result.get("title") or status)
        )
        span.set_attribute("tool.status", status)
        span.set_attribute("tool.latency_ms", round(latency_ms, 2))

        log_tool_outcome(
            agent_name=agent_name,
            tool_name=tool_name,
            status=status,
            latency_ms=latency_ms,
            result_summary=summary,
        )
        return result
