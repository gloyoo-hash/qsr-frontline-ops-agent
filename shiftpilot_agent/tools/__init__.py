"""ShiftPilot QSR domain tools with Pydantic v2 validation, guided errors, and HITL."""

from shiftpilot_agent.tools.base import HITL_REGISTRY
from shiftpilot_agent.tools.equipment_tools import (
    diagnose_kitchen_equipment,
    dispatch_maintenance_vendor,
)
from shiftpilot_agent.tools.inventory_tools import check_store_inventory
from shiftpilot_agent.tools.shift_tools import (
    issue_guest_recovery_voucher,
    request_shift_coverage,
)
from shiftpilot_agent.tools.sop_tools import lookup_food_safety_sop

ALL_TOOLS = [
    lookup_food_safety_sop,
    diagnose_kitchen_equipment,
    check_store_inventory,
    dispatch_maintenance_vendor,
    request_shift_coverage,
    issue_guest_recovery_voucher,
]

__all__ = [
    "ALL_TOOLS",
    "HITL_REGISTRY",
    "check_store_inventory",
    "diagnose_kitchen_equipment",
    "dispatch_maintenance_vendor",
    "issue_guest_recovery_voucher",
    "lookup_food_safety_sop",
    "request_shift_coverage",
]
