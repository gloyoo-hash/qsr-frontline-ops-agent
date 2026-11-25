"""Store inventory lookup tool for ShiftPilot QSR."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from shiftpilot_agent.schemas.models import (
    InventoryCheckInput,
    InventoryCheckOutput,
    InventoryItemStatus,
)
from shiftpilot_agent.tools.base import (
    build_guided_error,
    execute_instrumented_tool,
    format_validation_error,
)

# Simulated real-time store inventory ledger
_STORE_INVENTORY: dict[str, list[dict[str, Any]]] = {
    "SP-1042": [
        {
            "sku": "SKU-BUN-01",
            "item_name": "Brioche Buns (Case of 48)",
            "on_hand_units": 3,
            "unit_type": "cases",
            "par_level": 12,
            "next_delivery_eta": "Tomorrow 05:30 AM",
        },
        {
            "sku": "SKU-CHK-02",
            "item_name": "Crispy Chicken Filets (Case of 60)",
            "on_hand_units": 9,
            "unit_type": "cases",
            "par_level": 10,
            "next_delivery_eta": "Tomorrow 05:30 AM",
        },
        {
            "sku": "SKU-OIL-05",
            "item_name": "Fryer Shortening Jug (35 lb)",
            "on_hand_units": 1,
            "unit_type": "jugs",
            "par_level": 6,
            "next_delivery_eta": "Tomorrow 05:30 AM",
        },
        {
            "sku": "SKU-CUP-16",
            "item_name": "16oz Cold Beverage Cups (Sleeve of 50)",
            "on_hand_units": 0,
            "unit_type": "sleeves",
            "par_level": 20,
            "next_delivery_eta": "Today 16:00 (Emergency Transfer)",
        },
    ],
    "SP-2088": [
        {
            "sku": "SKU-BUN-01",
            "item_name": "Brioche Buns (Case of 48)",
            "on_hand_units": 14,
            "unit_type": "cases",
            "par_level": 12,
            "next_delivery_eta": "Tomorrow 06:00 AM",
        },
        {
            "sku": "SKU-OIL-05",
            "item_name": "Fryer Shortening Jug (35 lb)",
            "on_hand_units": 7,
            "unit_type": "jugs",
            "par_level": 6,
            "next_delivery_eta": "Tomorrow 06:00 AM",
        },
    ],
}


def _classify_stock(on_hand: int, par: int) -> str:
    if on_hand == 0:
        return "out_of_stock"
    ratio = on_hand / max(par, 1)
    if ratio <= 0.25:
        return "critical_86_risk"
    if ratio < 0.7:
        return "low_stock"
    return "healthy"


def check_store_inventory(
    store_id: str,
    item_sku_or_name: str,
) -> dict[str, Any]:
    """Check real-time on-hand stock, par level, and delivery ETA for a store item.

    Searches the specified ShiftPilot QSR store's inventory ledger by exact SKU
    or partial item name and classifies 86 (out-of-stock) risk so shift leads can
    arrange inter-store transfers before dinner rush.

    Args:
        store_id: ShiftPilot store ID matching ``SP-XXXX`` (e.g., ``"SP-1042"``).
        item_sku_or_name: SKU code (e.g., ``"SKU-BUN-01"``) or descriptive name
            (e.g., ``"brioche buns"``, ``"fryer oil"``, ``"cups"``).

    Returns:
        dict[str, Any]: On success, a validated ``InventoryCheckOutput`` dict with
        ``status="ok"``, ``store_id``, matching ``items`` (each including
        ``on_hand_units``, ``par_level``, ``status``, and ``next_delivery_eta``),
        and ``recommended_action``.

        Edge cases & guided errors:
        - Invalid ``store_id`` format returns ``status="error"`` with
          ``error_code="INVALID_INPUT_SCHEMA"``.
        - Unknown ``store_id`` returns ``status="error"`` with
          ``error_code="STORE_NOT_FOUND"`` and ``valid_options``.
        - Unmatched item query returns ``status="error"`` with
          ``error_code="ITEM_NOT_FOUND"`` and the store's available SKUs.
    """

    def _run() -> dict[str, Any]:
        try:
            req = InventoryCheckInput(
                store_id=store_id,
                item_sku_or_name=item_sku_or_name,
            )
        except ValidationError as exc:
            return format_validation_error(
                exc,
                tool_name="check_store_inventory",
                recovery_hint=(
                    "Provide a valid store_id matching 'SP-XXXX' (e.g. 'SP-1042') "
                    "and a non-empty SKU or item name."
                ),
                valid_options=sorted(_STORE_INVENTORY.keys()),
            )

        ledger = _STORE_INVENTORY.get(req.store_id)
        if ledger is None:
            return build_guided_error(
                error_code="STORE_NOT_FOUND",
                message=f"Store '{req.store_id}' was not found in the inventory service.",
                recovery_instructions=(
                    "Confirm the store ID with the frontline worker and retry using one "
                    "of the active store IDs in `valid_options`."
                ),
                valid_options=sorted(_STORE_INVENTORY.keys()),
            )

        query = req.item_sku_or_name.lower().strip()
        matches = [
            row
            for row in ledger
            if query in row["sku"].lower() or query in row["item_name"].lower()
        ]
        if not matches:
            return build_guided_error(
                error_code="ITEM_NOT_FOUND",
                message=(
                    f"No inventory item matching '{req.item_sku_or_name}' in store "
                    f"{req.store_id}."
                ),
                recovery_instructions=(
                    "Retry with a broader keyword or one of the SKUs listed in "
                    "`valid_options`."
                ),
                valid_options=[f"{r['sku']} ({r['item_name']})" for r in ledger],
            )

        items = [
            InventoryItemStatus(
                sku=row["sku"],
                item_name=row["item_name"],
                on_hand_units=row["on_hand_units"],
                unit_type=row["unit_type"],
                par_level=row["par_level"],
                status=_classify_stock(row["on_hand_units"], row["par_level"]),  # type: ignore[arg-type]
                next_delivery_eta=row["next_delivery_eta"],
            )
            for row in matches
        ]

        statuses = {item.status for item in items}
        if "out_of_stock" in statuses:
            recommended = (
                "86 item on POS/kiosk immediately and request an inter-store transfer "
                "from nearest sister store (SP-2088)."
            )
        elif "critical_86_risk" in statuses:
            recommended = (
                "Alert Shift Manager: item is at <=25% of par. Arrange sister-store "
                "transfer before peak rush."
            )
        elif "low_stock" in statuses:
            recommended = "Monitor consumption during peak hour and confirm next truck order."
        else:
            recommended = "Stock levels are healthy; no immediate action required."

        output = InventoryCheckOutput(
            store_id=req.store_id,
            items=items,
            recommended_action=recommended,
        )
        return output.model_dump()

    return execute_instrumented_tool(
        agent_name="kitchen_ops_agent",
        tool_name="check_store_inventory",
        intended_action=f"Check inventory for '{item_sku_or_name}' at store {store_id}",
        raw_args={"store_id": store_id, "item_sku_or_name": item_sku_or_name},
        handler=_run,
    )
