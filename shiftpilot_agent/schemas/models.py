"""Strict Pydantic v2 input, output, guided error, HITL, and memory models.

All tool arguments and responses are validated through these explicit JSON schemas.
`extra="forbid"` is enforced on input models so malformed LLM arguments are caught
and converted into structured `ToolErrorResponse` guidance rather than crashing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictBaseModel(BaseModel):
    """Base model forbidding unexpected fields and trimming whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Guided Error & HITL Models
# ---------------------------------------------------------------------------


class ToolErrorResponse(BaseModel):
    """Structured guided error returned to the LLM instead of raising exceptions."""

    status: Literal["error"] = "error"
    error_code: str = Field(..., description="Machine-readable error category.")
    message: str = Field(..., description="Human-readable summary of what failed.")
    recovery_instructions: str = Field(
        ...,
        description="Concrete, actionable next steps for the LLM to recover cleanly.",
    )
    valid_options: list[str] = Field(
        default_factory=list,
        description="Optional list of valid parameter values the LLM can choose from.",
    )


class HITLPendingResponse(BaseModel):
    """Returned when a high-stakes action pauses for explicit manager approval."""

    status: Literal["PENDING_MANAGER_APPROVAL"] = "PENDING_MANAGER_APPROVAL"
    approval_token: str = Field(
        ...,
        description="One-time confirmation token that a shift manager must approve.",
    )
    action_type: str = Field(..., description="High-stakes tool action requiring approval.")
    reason: str = Field(..., description="Policy rule that triggered the HITL pause.")
    proposed_details: dict[str, Any] = Field(
        default_factory=dict,
        description="Sanitized summary of the proposed action for manager review.",
    )
    manager_instructions: str = Field(
        ...,
        description="Clear instructions for the frontline worker to obtain manager sign-off.",
    )


# ---------------------------------------------------------------------------
# 1. Food Safety & Allergen SOP Lookup
# ---------------------------------------------------------------------------


class FoodSafetySOPInput(StrictBaseModel):
    """Input schema for `lookup_food_safety_sop`."""

    topic: str = Field(
        ...,
        min_length=2,
        max_length=120,
        description="Food safety topic, menu item, or allergen query (e.g. 'peanut_allergen', 'fryer_oil_temp', 'chicken_hold_time').",
    )
    store_region: str = Field(
        default="US-DEFAULT",
        min_length=2,
        max_length=32,
        description="Regulatory region code (e.g. 'US-DEFAULT', 'US-CA', 'EU-UK').",
    )


class FoodSafetySOPOutput(BaseModel):
    """Output schema for `lookup_food_safety_sop`."""

    status: Literal["ok"] = "ok"
    sop_id: str
    title: str
    store_region: str
    critical_limits: list[str]
    allergens_present: list[str]
    cross_contact_warnings: list[str]
    steps: list[str]
    escalation_required: bool = False
    disclaimer: str = (
        "Verified against official ShiftPilot QSR SOP Manual. "
        "Never guess or improvise allergen information."
    )


# ---------------------------------------------------------------------------
# 2. Kitchen Equipment Diagnostics
# ---------------------------------------------------------------------------


class EquipmentDiagnosisInput(StrictBaseModel):
    """Input schema for `diagnose_kitchen_equipment`."""

    store_id: str = Field(
        ...,
        pattern=r"^SP-\d{3,5}$",
        description="ShiftPilot store identifier in format 'SP-XXXX' (e.g. 'SP-1042').",
    )
    equipment_type: Literal[
        "fryer",
        "clamshell_grill",
        "soft_serve_machine",
        "walk_in_cooler",
        "espresso_machine",
        "pos_terminal",
    ] = Field(..., description="Standardized ShiftPilot kitchen equipment category.")
    error_code: str = Field(
        ...,
        min_length=2,
        max_length=32,
        description="Alphanumeric fault code shown on the equipment panel (e.g. 'E-42', 'HI-LIMIT', 'TEMP-WARN').",
    )
    symptoms: str = Field(
        default="",
        max_length=400,
        description="Optional crew observations (e.g. 'oil not heating', 'beeping continuously').",
    )


class EquipmentDiagnosisOutput(BaseModel):
    """Output schema for `diagnose_kitchen_equipment`."""

    status: Literal["ok"] = "ok"
    store_id: str
    equipment_type: str
    error_code: str
    severity: Literal["low", "medium", "high", "critical_food_safety"]
    probable_cause: str
    safe_crew_steps: list[str]
    requires_vendor_dispatch: bool
    estimated_vendor_cost_usd: float
    food_safety_hold_required: bool
    recurring_fault_count: int = 1


# ---------------------------------------------------------------------------
# 3. Store Inventory Check
# ---------------------------------------------------------------------------


class InventoryCheckInput(StrictBaseModel):
    """Input schema for `check_store_inventory`."""

    store_id: str = Field(
        ...,
        pattern=r"^SP-\d{3,5}$",
        description="ShiftPilot store identifier in format 'SP-XXXX'.",
    )
    item_sku_or_name: str = Field(
        ...,
        min_length=2,
        max_length=80,
        description="SKU code (e.g. 'SKU-BUN-01') or item name (e.g. 'brioche buns').",
    )


class InventoryItemStatus(BaseModel):
    """Single inventory item snapshot."""

    sku: str
    item_name: str
    on_hand_units: int = Field(..., ge=0)
    unit_type: str
    par_level: int = Field(..., ge=0)
    status: Literal["healthy", "low_stock", "critical_86_risk", "out_of_stock"]
    next_delivery_eta: str


class InventoryCheckOutput(BaseModel):
    """Output schema for `check_store_inventory`."""

    status: Literal["ok"] = "ok"
    store_id: str
    items: list[InventoryItemStatus]
    recommended_action: str


# ---------------------------------------------------------------------------
# 4. Maintenance Vendor Dispatch (HITL > $250)
# ---------------------------------------------------------------------------


class MaintenanceDispatchInput(StrictBaseModel):
    """Input schema for `dispatch_maintenance_vendor`."""

    store_id: str = Field(..., pattern=r"^SP-\d{3,5}$")
    equipment_type: Literal[
        "fryer",
        "clamshell_grill",
        "soft_serve_machine",
        "walk_in_cooler",
        "espresso_machine",
        "pos_terminal",
    ]
    issue_summary: str = Field(..., min_length=5, max_length=300)
    estimated_cost_usd: float = Field(..., gt=0, le=25000.0)
    urgency: Literal["routine", "same_day", "emergency"] = "same_day"
    manager_approval_token: str | None = Field(
        default=None,
        description="Required HITL approval token when estimated_cost_usd exceeds $250.",
    )


class MaintenanceDispatchOutput(BaseModel):
    """Output schema for `dispatch_maintenance_vendor`."""

    status: Literal["dispatched"] = "dispatched"
    ticket_id: str
    store_id: str
    vendor_name: str
    equipment_type: str
    estimated_cost_usd: float
    urgency: str
    eta_window: str
    approved_via_hitl: bool


# ---------------------------------------------------------------------------
# 5. Shift Coverage Request (HITL if overtime)
# ---------------------------------------------------------------------------


class ShiftCoverageInput(StrictBaseModel):
    """Input schema for `request_shift_coverage`."""

    store_id: str = Field(..., pattern=r"^SP-\d{3,5}$")
    role: Literal["cashier", "line_cook", "drive_thru", "shift_lead", "prep_crew"]
    shift_window: str = Field(
        ...,
        min_length=4,
        max_length=60,
        description="Time window needing coverage (e.g. 'Today 17:00-22:00').",
    )
    reason: str = Field(..., min_length=3, max_length=200)
    is_overtime: bool = Field(
        default=False,
        description="Whether filling this shift incurs overtime pay (>40 hrs/week).",
    )
    manager_approval_token: str | None = Field(
        default=None,
        description="Required HITL approval token when is_overtime is True.",
    )


class ShiftCoverageOutput(BaseModel):
    """Output schema for `request_shift_coverage`."""

    status: Literal["posted"] = "posted"
    coverage_request_id: str
    store_id: str
    role: str
    shift_window: str
    is_overtime: bool
    notified_candidates_count: int
    approved_via_hitl: bool


# ---------------------------------------------------------------------------
# 6. Guest Recovery Voucher (HITL > $25)
# ---------------------------------------------------------------------------


class GuestVoucherInput(StrictBaseModel):
    """Input schema for `issue_guest_recovery_voucher`."""

    store_id: str = Field(..., pattern=r"^SP-\d{3,5}$")
    order_reference: str = Field(
        ...,
        min_length=3,
        max_length=40,
        description="Order or receipt ID (e.g. 'ORD-8821').",
    )
    reason: str = Field(..., min_length=5, max_length=200)
    amount_usd: float = Field(..., gt=0, le=500.0)
    manager_approval_token: str | None = Field(
        default=None,
        description="Required HITL approval token when amount_usd exceeds $25.",
    )

    @field_validator("amount_usd")
    @classmethod
    def round_currency(cls, value: float) -> float:
        return round(value, 2)


class GuestVoucherOutput(BaseModel):
    """Output schema for `issue_guest_recovery_voucher`."""

    status: Literal["issued"] = "issued"
    voucher_code: str
    store_id: str
    order_reference: str
    amount_usd: float
    expires_in_days: int = 30
    approved_via_hitl: bool


# ---------------------------------------------------------------------------
# Memory & Compaction Models
# ---------------------------------------------------------------------------


class ShiftHandoffNote(BaseModel):
    """Consolidated shift handoff record persisted asynchronously."""

    store_id: str
    session_id: str
    summary: str
    open_equipment_issues: list[str] = Field(default_factory=list)
    low_inventory_alerts: list[str] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class StoreMemoryRecord(BaseModel):
    """Persistent store state tracked across turns and sessions."""

    store_id: str
    recurring_faults: dict[str, int] = Field(default_factory=dict)
    handoff_notes: list[ShiftHandoffNote] = Field(default_factory=list)
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class CompactedHistory(BaseModel):
    """Result of sliding-window + summarization history compaction."""

    summary: str
    recent_turns: list[dict[str, str]]
    original_turn_count: int
    compacted_turn_count: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    was_compacted: bool
