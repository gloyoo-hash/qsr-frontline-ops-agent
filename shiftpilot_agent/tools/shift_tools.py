"""Shift coverage and guest recovery voucher tools with HITL governance."""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import ValidationError

from shiftpilot_agent.config import CONFIG
from shiftpilot_agent.schemas.models import (
    GuestVoucherInput,
    GuestVoucherOutput,
    ShiftCoverageInput,
    ShiftCoverageOutput,
)
from shiftpilot_agent.tools.base import (
    HITL_REGISTRY,
    build_guided_error,
    execute_instrumented_tool,
    format_validation_error,
)

_VALID_ROLES = ["cashier", "line_cook", "drive_thru", "shift_lead", "prep_crew"]


def request_shift_coverage(
    store_id: str,
    role: str,
    shift_window: str,
    reason: str,
    is_overtime: bool = False,
    manager_approval_token: str | None = None,
) -> dict[str, Any]:
    """Post an open-shift coverage alert to eligible ShiftPilot QSR crew members.

    Enforces Human-in-the-Loop (HITL) labor-cost governance: if ``is_overtime``
    is ``True``, execution MUST pause and return ``status="PENDING_MANAGER_APPROVAL"``
    with a one-time ``approval_token``. The shift is posted only after a Shift
    Manager approves the token and this tool is re-invoked with
    ``manager_approval_token``.

    Args:
        store_id: ShiftPilot store ID matching ``SP-XXXX`` (e.g., ``"SP-1042"``).
        role: Crew position requiring coverage. One of ``"cashier"``,
            ``"line_cook"``, ``"drive_thru"``, ``"shift_lead"``, or ``"prep_crew"``.
        shift_window: Human-readable shift time range (e.g., ``"Today 17:00-22:00"``).
        reason: Brief operational reason (e.g., ``"Crew illness call-out"``).
        is_overtime: Set ``True`` if covering the shift triggers overtime pay
            (>40 hours/week). Defaults to ``False``.
        manager_approval_token: Optional HITL confirmation token required when
            ``is_overtime`` is ``True``.

    Returns:
        dict[str, Any]:
        - If ``is_overtime=True`` and no approved token is supplied: a
          ``HITLPendingResponse`` dict with ``status="PENDING_MANAGER_APPROVAL"``.
        - On authorized posting: a ``ShiftCoverageOutput`` dict with
          ``status="posted"``, ``coverage_request_id``, and
          ``notified_candidates_count``.
        - On validation or token errors: a guided ``ToolErrorResponse`` dict
          with ``status="error"`` and ``recovery_instructions``.
    """

    def _run() -> dict[str, Any]:
        try:
            req = ShiftCoverageInput(
                store_id=store_id,
                role=role,  # type: ignore[arg-type]
                shift_window=shift_window,
                reason=reason,
                is_overtime=is_overtime,
                manager_approval_token=manager_approval_token,
            )
        except ValidationError as exc:
            return format_validation_error(
                exc,
                tool_name="request_shift_coverage",
                recovery_hint=(
                    "Provide a valid store_id ('SP-XXXX'), role from valid_options, "
                    "shift_window, and reason."
                ),
                valid_options=_VALID_ROLES,
            )

        approved_via_hitl = False
        if req.is_overtime and CONFIG.hitl.overtime_requires_approval:
            if req.manager_approval_token is None:
                return HITL_REGISTRY.create_pending_approval(
                    action_type="request_shift_coverage",
                    reason=(
                        "Overtime shift coverage incurs 1.5x labor cost and requires "
                        "explicit Shift Manager approval before posting."
                    ),
                    proposed_details={
                        "store_id": req.store_id,
                        "role": req.role,
                        "shift_window": req.shift_window,
                        "is_overtime": req.is_overtime,
                        "reason": req.reason,
                    },
                    manager_instructions=(
                        "Pause and ask the Shift Manager to approve overtime coverage using "
                        "`approval_token`, then re-invoke `request_shift_coverage` with "
                        "`manager_approval_token`."
                    ),
                )
            if not HITL_REGISTRY.consume_approval(
                req.manager_approval_token, "request_shift_coverage"
            ):
                return build_guided_error(
                    error_code="INVALID_OR_UNAPPROVED_HITL_TOKEN",
                    message=(
                        f"Approval token '{req.manager_approval_token}' is invalid or "
                        "not yet approved by a Shift Manager."
                    ),
                    recovery_instructions=(
                        "Obtain Shift Manager confirmation for the pending overtime token "
                        "or call `request_shift_coverage` without a token to issue a new "
                        "approval request."
                    ),
                )
            approved_via_hitl = True

        req_id = f"COV-{secrets.token_hex(3).upper()}"
        output = ShiftCoverageOutput(
            coverage_request_id=req_id,
            store_id=req.store_id,
            role=req.role,
            shift_window=req.shift_window,
            is_overtime=req.is_overtime,
            notified_candidates_count=5 if not req.is_overtime else 8,
            approved_via_hitl=approved_via_hitl,
        )
        return output.model_dump()

    return execute_instrumented_tool(
        agent_name="shift_recovery_agent",
        tool_name="request_shift_coverage",
        intended_action=(
            f"Request {'overtime ' if is_overtime else ''}shift coverage for {role} "
            f"at {store_id} ({shift_window})"
        ),
        raw_args={
            "store_id": store_id,
            "role": role,
            "shift_window": shift_window,
            "reason": reason,
            "is_overtime": is_overtime,
            "has_approval_token": bool(manager_approval_token),
        },
        handler=_run,
    )


def issue_guest_recovery_voucher(
    store_id: str,
    order_reference: str,
    reason: str,
    amount_usd: float,
    manager_approval_token: str | None = None,
) -> dict[str, Any]:
    """Issue a digital guest recovery voucher for a delayed or incorrect QSR order.

    Enforces Human-in-the-Loop (HITL) financial governance: any voucher with
    ``amount_usd`` exceeding ``$25.00`` MUST pause and return
    ``status="PENDING_MANAGER_APPROVAL"`` with a one-time ``approval_token``.
    The voucher is generated only after explicit Shift Manager confirmation.

    Args:
        store_id: ShiftPilot store ID matching ``SP-XXXX`` (e.g., ``"SP-1042"``).
        order_reference: Receipt or order identifier (e.g., ``"ORD-8821"``).
        reason: Concise hospitality recovery reason (5-200 chars).
        amount_usd: Voucher face value in USD (> 0 and <= 500).
        manager_approval_token: Optional HITL confirmation token required when
            ``amount_usd > 25.0``.

    Returns:
        dict[str, Any]:
        - If ``amount_usd > 25.0`` and no approved token is supplied: a
          ``HITLPendingResponse`` dict with ``status="PENDING_MANAGER_APPROVAL"``.
        - On authorized issuance: a ``GuestVoucherOutput`` dict with
          ``status="issued"``, ``voucher_code``, ``amount_usd``, and
          ``expires_in_days``.
        - On invalid arguments or unapproved token: a guided
          ``ToolErrorResponse`` dict with ``status="error"`` and
          ``recovery_instructions``.
    """

    def _run() -> dict[str, Any]:
        try:
            req = GuestVoucherInput(
                store_id=store_id,
                order_reference=order_reference,
                reason=reason,
                amount_usd=amount_usd,
                manager_approval_token=manager_approval_token,
            )
        except ValidationError as exc:
            return format_validation_error(
                exc,
                tool_name="issue_guest_recovery_voucher",
                recovery_hint=(
                    "Provide a valid store_id ('SP-XXXX'), order_reference (>=3 chars), "
                    "reason (>=5 chars), and amount_usd between 0.01 and 500.00."
                ),
            )

        threshold = CONFIG.hitl.guest_voucher_usd
        approved_via_hitl = False

        if req.amount_usd > threshold:
            if req.manager_approval_token is None:
                return HITL_REGISTRY.create_pending_approval(
                    action_type="issue_guest_recovery_voucher",
                    reason=(
                        f"Guest recovery voucher (${req.amount_usd:.2f}) exceeds the "
                        f"${threshold:.2f} crew empowerment limit."
                    ),
                    proposed_details={
                        "store_id": req.store_id,
                        "order_reference": req.order_reference,
                        "amount_usd": req.amount_usd,
                        "reason": req.reason,
                    },
                    manager_instructions=(
                        "Pause and request Shift Manager approval using `approval_token`, "
                        "then re-invoke `issue_guest_recovery_voucher` with "
                        "`manager_approval_token`."
                    ),
                )
            if not HITL_REGISTRY.consume_approval(
                req.manager_approval_token, "issue_guest_recovery_voucher"
            ):
                return build_guided_error(
                    error_code="INVALID_OR_UNAPPROVED_HITL_TOKEN",
                    message=(
                        f"Approval token '{req.manager_approval_token}' is invalid or "
                        "not yet approved by a Shift Manager."
                    ),
                    recovery_instructions=(
                        "Have the Shift Manager approve the pending token before retrying, "
                        "or re-invoke without a token to request a new approval token."
                    ),
                )
            approved_via_hitl = True

        voucher_code = f"SP-CARE-{secrets.token_hex(3).upper()}"
        output = GuestVoucherOutput(
            voucher_code=voucher_code,
            store_id=req.store_id,
            order_reference=req.order_reference,
            amount_usd=req.amount_usd,
            approved_via_hitl=approved_via_hitl,
        )
        return output.model_dump()

    return execute_instrumented_tool(
        agent_name="shift_recovery_agent",
        tool_name="issue_guest_recovery_voucher",
        intended_action=(
            f"Issue ${amount_usd} guest recovery voucher for order {order_reference} at {store_id}"
        ),
        raw_args={
            "store_id": store_id,
            "order_reference": order_reference,
            "reason": reason,
            "amount_usd": amount_usd,
            "has_approval_token": bool(manager_approval_token),
        },
        handler=_run,
    )
