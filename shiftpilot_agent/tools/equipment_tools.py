"""Kitchen equipment diagnostics and maintenance vendor dispatch tools."""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import ValidationError

from shiftpilot_agent.config import CONFIG
from shiftpilot_agent.memory.store_backend import get_default_store_backend
from shiftpilot_agent.schemas.models import (
    EquipmentDiagnosisInput,
    EquipmentDiagnosisOutput,
    MaintenanceDispatchInput,
    MaintenanceDispatchOutput,
)
from shiftpilot_agent.tools.base import (
    HITL_REGISTRY,
    build_guided_error,
    execute_instrumented_tool,
    format_validation_error,
)

_EQUIPMENT_FAULTS: dict[tuple[str, str], dict[str, Any]] = {
    ("fryer", "HI-LIMIT"): {
        "severity": "high",
        "probable_cause": "High-limit thermal safety switch tripped due to oil overheat (>400°F) or low oil level.",
        "safe_crew_steps": [
            "Turn fryer power switch OFF immediately and do not add cold oil to a hot vat.",
            "Verify oil level is at the COLD/HOT fill line once cooled below 250°F.",
            "Press the red manual reset button underneath the control panel ONE time only.",
            "If HI-LIMIT trips a second time, lock out the vat and dispatch a certified technician.",
        ],
        "requires_vendor_dispatch": True,
        "estimated_vendor_cost_usd": 325.0,
        "food_safety_hold_required": False,
    },
    ("fryer", "E-42"): {
        "severity": "medium",
        "probable_cause": "Temperature probe open-circuit or loose harness connection.",
        "safe_crew_steps": [
            "Power cycle the fryer vat via the main toggle switch and wait 60 seconds.",
            "Shift frying production to Vat #2 and verify Vat #2 is at 350°F.",
            "If E-42 persists after one reset, submit a same-day vendor dispatch.",
        ],
        "requires_vendor_dispatch": True,
        "estimated_vendor_cost_usd": 195.0,
        "food_safety_hold_required": False,
    },
    ("walk_in_cooler", "TEMP-WARN"): {
        "severity": "critical_food_safety",
        "probable_cause": "Evaporator coil icing or compressor relay fault causing box temp > 41°F.",
        "safe_crew_steps": [
            "Keep the walk-in cooler door firmly closed and check door gasket seal.",
            "Probe internal temperature of dairy and raw poultry immediately.",
            "Quarantine TCS food items if above 41°F per SOP-FS-301.",
            "Dispatch emergency refrigeration vendor immediately.",
        ],
        "requires_vendor_dispatch": True,
        "estimated_vendor_cost_usd": 480.0,
        "food_safety_hold_required": True,
    },
    ("soft_serve_machine", "PAST-FAIL"): {
        "severity": "critical_food_safety",
        "probable_cause": "Overnight heat-treatment pasteurization cycle did not reach 151°F for 30 minutes.",
        "safe_crew_steps": [
            "DO NOT serve any soft-serve or shakes — product lock is mandatory for food safety.",
            "Drain, disassemble, wash, rinse, and sanitize hopper per cleaning SOP.",
            "If heat-treatment fails again, dispatch refrigeration/dessert vendor.",
        ],
        "requires_vendor_dispatch": True,
        "estimated_vendor_cost_usd": 290.0,
        "food_safety_hold_required": True,
    },
    ("clamshell_grill", "PLATEN-JAM"): {
        "severity": "medium",
        "probable_cause": "Carbon buildup or actuator sensor misalignment on upper platen.",
        "safe_crew_steps": [
            "Press STOP, let platen cool, and inspect for debris along rear hinge.",
            "Never force the upper platen downward by hand.",
            "Switch burger cooking to Lane 2 until technician inspects actuator.",
        ],
        "requires_vendor_dispatch": True,
        "estimated_vendor_cost_usd": 220.0,
        "food_safety_hold_required": False,
    },
    ("pos_terminal", "NET-OFFLINE"): {
        "severity": "low",
        "probable_cause": "Local Ethernet cable unplugged or store router DHCP lease drop.",
        "safe_crew_steps": [
            "Verify the blue Ethernet cable is seated firmly at the base of the POS terminal.",
            "Switch terminal to Store-and-Forward offline mode if drive-thru queue is active.",
            "Reboot terminal via Shift Manager menu; call IT helpdesk if still offline after 5 minutes.",
        ],
        "requires_vendor_dispatch": False,
        "estimated_vendor_cost_usd": 0.0,
        "food_safety_hold_required": False,
    },
}

_VALID_EQUIPMENT = [
    "fryer",
    "clamshell_grill",
    "soft_serve_machine",
    "walk_in_cooler",
    "espresso_machine",
    "pos_terminal",
]

_VENDOR_MAP = {
    "fryer": "Apex Commercial Kitchen Services",
    "clamshell_grill": "Apex Commercial Kitchen Services",
    "soft_serve_machine": "FrostLine Dessert Systems",
    "walk_in_cooler": "PolarGuard Refrigeration 24/7",
    "espresso_machine": "CremaTech Field Service",
    "pos_terminal": "ShiftPilot IT Hardware Desk",
}


def diagnose_kitchen_equipment(
    store_id: str,
    equipment_type: str,
    error_code: str,
    symptoms: str = "",
) -> dict[str, Any]:
    """Diagnose a ShiftPilot QSR kitchen equipment fault and record recurring incidents.

    Validates store ID and equipment category, looks up safe frontline
    troubleshooting steps, flags food-safety holds when cold/heat-treatment
    limits are breached, and increments the store's persistent recurring fault
    counter in the session/store memory backend.

    Args:
        store_id: ShiftPilot store ID matching ``SP-XXXX`` (e.g., ``"SP-1042"``).
        equipment_type: Standardized equipment category. One of ``"fryer"``,
            ``"clamshell_grill"``, ``"soft_serve_machine"``, ``"walk_in_cooler"``,
            ``"espresso_machine"``, or ``"pos_terminal"``.
        error_code: Fault code displayed on the control panel (e.g., ``"HI-LIMIT"``,
            ``"E-42"``, ``"TEMP-WARN"``, ``"PAST-FAIL"``, ``"PLATEN-JAM"``,
            ``"NET-OFFLINE"``).
        symptoms: Optional free-text crew observations. Defaults to ``""``.

    Returns:
        dict[str, Any]: On success, a validated ``EquipmentDiagnosisOutput``
        dictionary with ``status="ok"``, ``severity``, ``probable_cause``,
        ``safe_crew_steps``, ``requires_vendor_dispatch``,
        ``estimated_vendor_cost_usd``, ``food_safety_hold_required``, and
        ``recurring_fault_count``.

        Edge cases & guided errors:
        - Invalid ``store_id`` or unsupported ``equipment_type`` returns
          ``status="error"`` with ``error_code="INVALID_INPUT_SCHEMA"`` and
          ``valid_options``.
        - Unknown ``error_code`` returns ``status="error"`` with
          ``error_code="UNKNOWN_FAULT_CODE"`` and safe escalation instructions
          so crew never attempt unsafe repairs.
    """

    def _run() -> dict[str, Any]:
        try:
            req = EquipmentDiagnosisInput(
                store_id=store_id,
                equipment_type=equipment_type,  # type: ignore[arg-type]
                error_code=error_code.upper(),
                symptoms=symptoms,
            )
        except ValidationError as exc:
            return format_validation_error(
                exc,
                tool_name="diagnose_kitchen_equipment",
                recovery_hint=(
                    "Ensure `store_id` matches 'SP-XXXX' (e.g. 'SP-1042') and "
                    "`equipment_type` is one of the valid options."
                ),
                valid_options=_VALID_EQUIPMENT,
            )

        key = (req.equipment_type, req.error_code.upper())
        fault = _EQUIPMENT_FAULTS.get(key)
        if fault is None:
            known_codes = sorted(
                code for eq, code in _EQUIPMENT_FAULTS if eq == req.equipment_type
            )
            return build_guided_error(
                error_code="UNKNOWN_FAULT_CODE",
                message=(
                    f"Fault code '{req.error_code}' is not in the quick-reference catalog "
                    f"for '{req.equipment_type}'."
                ),
                recovery_instructions=(
                    "Confirm the exact error code on the display panel and retry with one "
                    "of the known codes in `valid_options`, or power down the unit safely "
                    "and call `dispatch_maintenance_vendor` with detailed symptoms."
                ),
                valid_options=known_codes,
            )

        # Track recurring fault count in persistent store backend
        backend = get_default_store_backend()
        fault_key = f"{req.equipment_type}:{req.error_code}"
        recurring_count = backend.record_equipment_fault(req.store_id, fault_key)

        output = EquipmentDiagnosisOutput(
            store_id=req.store_id,
            equipment_type=req.equipment_type,
            error_code=req.error_code,
            severity=fault["severity"],
            probable_cause=fault["probable_cause"],
            safe_crew_steps=fault["safe_crew_steps"],
            requires_vendor_dispatch=fault["requires_vendor_dispatch"],
            estimated_vendor_cost_usd=fault["estimated_vendor_cost_usd"],
            food_safety_hold_required=fault["food_safety_hold_required"],
            recurring_fault_count=recurring_count,
        )
        return output.model_dump()

    return execute_instrumented_tool(
        agent_name="kitchen_ops_agent",
        tool_name="diagnose_kitchen_equipment",
        intended_action=f"Diagnose {equipment_type} fault '{error_code}' at store {store_id}",
        raw_args={
            "store_id": store_id,
            "equipment_type": equipment_type,
            "error_code": error_code,
            "symptoms": symptoms,
        },
        handler=_run,
    )


def dispatch_maintenance_vendor(
    store_id: str,
    equipment_type: str,
    issue_summary: str,
    estimated_cost_usd: float,
    urgency: str = "same_day",
    manager_approval_token: str | None = None,
) -> dict[str, Any]:
    """Dispatch an approved third-party maintenance vendor for kitchen equipment repair.

    Enforces Human-in-the-Loop (HITL) financial governance: any dispatch with
    ``estimated_cost_usd`` exceeding ``$250.00`` MUST pause and return a
    ``PENDING_MANAGER_APPROVAL`` response with an ``approval_token``. Execution
    proceeds only after a Shift Manager explicitly approves that token and the
    tool is re-invoked with ``manager_approval_token``.

    Args:
        store_id: ShiftPilot store ID matching ``SP-XXXX`` (e.g., ``"SP-1042"``).
        equipment_type: Standardized equipment category (``"fryer"``,
            ``"clamshell_grill"``, ``"soft_serve_machine"``, ``"walk_in_cooler"``,
            ``"espresso_machine"``, ``"pos_terminal"``).
        issue_summary: Concise description of the malfunction (5-300 chars).
        estimated_cost_usd: Estimated vendor service cost in USD (> 0 and <= 25000).
        urgency: Dispatch SLA tier: ``"routine"``, ``"same_day"``, or ``"emergency"``.
            Defaults to ``"same_day"``.
        manager_approval_token: Optional HITL confirmation token previously issued
            when ``estimated_cost_usd > 250.0``.

    Returns:
        dict[str, Any]:
        - If ``estimated_cost_usd > 250.0`` and no valid approved token is provided:
          a ``HITLPendingResponse`` dict with ``status="PENDING_MANAGER_APPROVAL"``
          and ``approval_token``.
        - On authorized dispatch: a ``MaintenanceDispatchOutput`` dict with
          ``status="dispatched"``, ``ticket_id``, ``vendor_name``, and ``eta_window``.
        - On invalid arguments or unapproved/invalid token: a guided
          ``ToolErrorResponse`` dict with ``status="error"`` and
          ``recovery_instructions``.
    """

    def _run() -> dict[str, Any]:
        try:
            req = MaintenanceDispatchInput(
                store_id=store_id,
                equipment_type=equipment_type,  # type: ignore[arg-type]
                issue_summary=issue_summary,
                estimated_cost_usd=estimated_cost_usd,
                urgency=urgency,  # type: ignore[arg-type]
                manager_approval_token=manager_approval_token,
            )
        except ValidationError as exc:
            return format_validation_error(
                exc,
                tool_name="dispatch_maintenance_vendor",
                recovery_hint=(
                    "Provide a valid store_id ('SP-XXXX'), equipment_type, issue_summary "
                    "(>=5 chars), positive estimated_cost_usd, and urgency ('routine', "
                    "'same_day', or 'emergency')."
                ),
                valid_options=_VALID_EQUIPMENT,
            )

        threshold = CONFIG.hitl.maintenance_cost_usd
        approved_via_hitl = False

        if req.estimated_cost_usd > threshold:
            if req.manager_approval_token is None:
                return HITL_REGISTRY.create_pending_approval(
                    action_type="dispatch_maintenance_vendor",
                    reason=(
                        f"Estimated vendor cost (${req.estimated_cost_usd:.2f}) exceeds the "
                        f"${threshold:.2f} frontline auto-approval limit."
                    ),
                    proposed_details={
                        "store_id": req.store_id,
                        "equipment_type": req.equipment_type,
                        "estimated_cost_usd": req.estimated_cost_usd,
                        "urgency": req.urgency,
                        "issue_summary": req.issue_summary,
                    },
                    manager_instructions=(
                        "Pause execution and ask the Shift Manager to review and approve "
                        "the dispatch using the provided `approval_token`, then re-run "
                        "`dispatch_maintenance_vendor` with `manager_approval_token`."
                    ),
                )
            if not HITL_REGISTRY.consume_approval(
                req.manager_approval_token, "dispatch_maintenance_vendor"
            ):
                return build_guided_error(
                    error_code="INVALID_OR_UNAPPROVED_HITL_TOKEN",
                    message=(
                        f"Approval token '{req.manager_approval_token}' is invalid, "
                        "expired, or has not been approved by a Shift Manager."
                    ),
                    recovery_instructions=(
                        "Have the Shift Manager confirm approval for the pending token, or "
                        "re-invoke `dispatch_maintenance_vendor` without a token to generate "
                        "a fresh `PENDING_MANAGER_APPROVAL` request."
                    ),
                )
            approved_via_hitl = True

        eta_map = {
            "emergency": "Within 2 hours",
            "same_day": "Within 6 hours",
            "routine": "Next business day (08:00-12:00)",
        }
        ticket_id = f"MNT-{secrets.token_hex(3).upper()}"
        output = MaintenanceDispatchOutput(
            ticket_id=ticket_id,
            store_id=req.store_id,
            vendor_name=_VENDOR_MAP[req.equipment_type],
            equipment_type=req.equipment_type,
            estimated_cost_usd=req.estimated_cost_usd,
            urgency=req.urgency,
            eta_window=eta_map[req.urgency],
            approved_via_hitl=approved_via_hitl,
        )
        return output.model_dump()

    return execute_instrumented_tool(
        agent_name="kitchen_ops_agent",
        tool_name="dispatch_maintenance_vendor",
        intended_action=(
            f"Dispatch {urgency} vendor for {equipment_type} at {store_id} "
            f"(est. ${estimated_cost_usd})"
        ),
        raw_args={
            "store_id": store_id,
            "equipment_type": equipment_type,
            "issue_summary": issue_summary,
            "estimated_cost_usd": estimated_cost_usd,
            "urgency": urgency,
            "has_approval_token": bool(manager_approval_token),
        },
        handler=_run,
    )
