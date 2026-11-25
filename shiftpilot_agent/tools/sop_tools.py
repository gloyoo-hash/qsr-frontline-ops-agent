"""Food safety and allergen SOP lookup tool for ShiftPilot QSR."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from shiftpilot_agent.schemas.models import FoodSafetySOPInput, FoodSafetySOPOutput
from shiftpilot_agent.tools.base import (
    build_guided_error,
    execute_instrumented_tool,
    format_validation_error,
)

# Verified ShiftPilot QSR SOP Knowledge Base (fictional brand data)
_SOP_DATABASE: dict[str, dict[str, Any]] = {
    "peanut_allergen": {
        "sop_id": "SOP-ALG-101",
        "title": "Peanut & Tree Nut Allergen Handling Protocol",
        "critical_limits": [
            "Zero nut ingredients are permitted in the main fryer vat.",
            "Allergy-flagged orders require fresh gloves, sanitized prep board, and clean tongs.",
        ],
        "allergens_present": ["Peanuts (pre-packaged trail mix & sundae topping packets only)"],
        "cross_contact_warnings": [
            "Soft-serve topping station handles sealed peanut packets — wash hands and swap scoop before allergen orders.",
            "Bakery buns are produced in a facility that also processes tree nuts.",
        ],
        "steps": [
            "Inform the Shift Manager immediately when a guest declares a severe nut allergy.",
            "Verify the printed in-store Allergen Matrix binder for the exact item.",
            "Wash hands, don fresh blue allergen gloves, and use a dedicated sanitized prep tray.",
            "Serve the allergen order sealed and hand-carried separately from other trays.",
        ],
        "escalation_required": True,
    },
    "dairy_gluten_allergen": {
        "sop_id": "SOP-ALG-102",
        "title": "Dairy & Gluten Allergen Verification Protocol",
        "critical_limits": [
            "All crispy chicken breading contains wheat (gluten) and buttermilk (dairy).",
            "Brioche buns contain wheat, dairy, and egg; lettuce wraps are gluten-free when prepped on a clean surface.",
        ],
        "allergens_present": ["Wheat/Gluten", "Milk/Dairy", "Egg"],
        "cross_contact_warnings": [
            "Shared fryers are used for breaded chicken and fries — fries are NOT celiac-safe.",
            "Clamshell grill surfaces receive butter-oil seasoning unless cleaned and scraped.",
        ],
        "steps": [
            "Disclose shared-fryer cross-contact clearly to any guest with gluten or dairy allergies.",
            "Offer lettuce-wrapped grilled chicken prepared on a sanitized grill lane.",
            "Never assure a guest that an item is '100% allergen-free' without manager verification.",
        ],
        "escalation_required": False,
    },
    "fryer_oil_temp": {
        "sop_id": "SOP-FS-204",
        "title": "Fryer Oil Temperature & Filtering Standard",
        "critical_limits": [
            "Standard operating fryer oil temperature: 350°F (177°C) +/- 5°F.",
            "Maximum safe oil temperature before thermal cut-off: 400°F (204°C).",
            "Minimum internal cooked chicken temperature: 165°F (74°C) for 15 seconds.",
        ],
        "allergens_present": ["Soy (refined soybean oil)", "Wheat (cross-contact from breaded items)"],
        "cross_contact_warnings": [
            "Do not fry gluten-free items in vats shared with breaded poultry.",
        ],
        "steps": [
            "Verify digital vat readout with a calibrated stem thermometer at shift open and every 4 hours.",
            "If temperature drops below 335°F, hold frying and allow vat recovery to avoid undercooked poultry.",
            "If temperature exceeds 390°F or smokes, power off vat immediately and run `diagnose_kitchen_equipment`.",
        ],
        "escalation_required": False,
    },
    "chicken_hold_time": {
        "sop_id": "SOP-FS-210",
        "title": "Cooked Crispy & Grilled Chicken Holding Limits",
        "critical_limits": [
            "Minimum hot-holding cabinet temperature: 140°F (60°C).",
            "Maximum quality & safety hold time for crispy filets: 30 minutes.",
            "Maximum hold time for grilled filets: 45 minutes.",
        ],
        "allergens_present": ["Wheat (crispy filet)", "Milk (crispy filet)", "Soy"],
        "cross_contact_warnings": [
            "Store grilled and crispy filets in separate labeled UHC trays with dedicated tongs.",
        ],
        "steps": [
            "Start the UHC timer immediately upon transferring cooked filets.",
            "Discard any tray automatically when the timer expires — never reset timers.",
            "Log waste count in the shift waste sheet.",
        ],
        "escalation_required": False,
    },
    "walk_in_cooler_temp": {
        "sop_id": "SOP-FS-301",
        "title": "Walk-In Cooler Temperature Excursion & Food Hold SOP",
        "critical_limits": [
            "Safe cold-holding range: 34°F to 41°F (1°C to 5°C).",
            "Excursion threshold: > 41°F for more than 2 hours requires mandatory product quarantine.",
            "Any TCS food above 45°F for > 4 hours MUST be discarded.",
        ],
        "allergens_present": [],
        "cross_contact_warnings": [
            "Store raw poultry on the lowest shelf below ready-to-eat produce and sauces.",
        ],
        "steps": [
            "Verify ambient and internal product temperature using a calibrated probe thermometer.",
            "Keep the walk-in door closed to preserve thermal mass.",
            "Tag and quarantine TCS items if temperature exceeds 41°F and alert the Shift Manager.",
            "Diagnose cooler fault immediately and dispatch emergency refrigeration vendor if needed.",
        ],
        "escalation_required": True,
    },
}

_TOPIC_ALIASES: dict[str, str] = {
    "peanut": "peanut_allergen",
    "nut": "peanut_allergen",
    "tree nut": "peanut_allergen",
    "allergen": "peanut_allergen",
    "dairy": "dairy_gluten_allergen",
    "gluten": "dairy_gluten_allergen",
    "celiac": "dairy_gluten_allergen",
    "wheat": "dairy_gluten_allergen",
    "fryer": "fryer_oil_temp",
    "oil": "fryer_oil_temp",
    "chicken": "chicken_hold_time",
    "hold time": "chicken_hold_time",
    "cooler": "walk_in_cooler_temp",
    "walk-in": "walk_in_cooler_temp",
    "fridge": "walk_in_cooler_temp",
}


def lookup_food_safety_sop(
    topic: str,
    store_region: str = "US-DEFAULT",
) -> dict[str, Any]:
    """Look up verified ShiftPilot QSR food safety, allergen, or temperature SOPs.

    Always invoke this tool before answering any allergen, cross-contact, cooking
    temperature, or food-holding question. Never guess or improvise food-safety
    or allergen guidance.

    Args:
        topic: Food safety topic, allergen, or menu item keyword to look up
            (e.g., ``"peanut_allergen"``, ``"dairy_gluten_allergen"``,
            ``"fryer_oil_temp"``, ``"chicken_hold_time"``, ``"walk_in_cooler_temp"``).
        store_region: Regulatory jurisdiction code such as ``"US-DEFAULT"``,
            ``"US-CA"``, or ``"EU-UK"``. Defaults to ``"US-DEFAULT"``.

    Returns:
        dict[str, Any]: On success, a validated ``FoodSafetySOPOutput`` dictionary
        containing ``status="ok"``, ``sop_id``, ``title``, ``critical_limits``,
        ``allergens_present``, ``cross_contact_warnings``, ``steps``, and
        ``escalation_required``.

        Edge cases & guided errors:
        - If ``topic`` is empty or invalid, returns ``status="error"`` with
          ``error_code="INVALID_INPUT_SCHEMA"`` and ``recovery_instructions``.
        - If no verified SOP matches ``topic``, returns ``status="error"`` with
          ``error_code="SOP_NOT_FOUND"``, ``valid_options`` listing known SOP
          keys, and strict instructions to escalate to the Shift Manager rather
          than guessing allergen or safety details.
    """

    def _run() -> dict[str, Any]:
        try:
            req = FoodSafetySOPInput(topic=topic, store_region=store_region)
        except ValidationError as exc:
            return format_validation_error(
                exc,
                tool_name="lookup_food_safety_sop",
                recovery_hint=(
                    "Provide a valid non-empty food safety topic (2-120 chars) and "
                    "region code (e.g. 'US-DEFAULT'), then retry."
                ),
                valid_options=sorted(_SOP_DATABASE.keys()),
            )

        normalized = req.topic.lower().strip().replace(" ", "_")
        sop_key = normalized if normalized in _SOP_DATABASE else None

        if sop_key is None:
            raw_lower = req.topic.lower().strip()
            for alias, mapped_key in _TOPIC_ALIASES.items():
                if alias in raw_lower:
                    sop_key = mapped_key
                    break

        if sop_key is None:
            return build_guided_error(
                error_code="SOP_NOT_FOUND",
                message=f"No verified ShiftPilot SOP found for topic '{req.topic}'.",
                recovery_instructions=(
                    "DO NOT guess allergen or food safety rules. Either retry with one of "
                    "the verified SOP keys in `valid_options`, or instruct the frontline "
                    "worker to check the physical in-store Allergen Matrix binder and "
                    "notify the Shift Manager immediately."
                ),
                valid_options=sorted(_SOP_DATABASE.keys()),
            )

        record = _SOP_DATABASE[sop_key]
        output = FoodSafetySOPOutput(
            sop_id=record["sop_id"],
            title=record["title"],
            store_region=req.store_region,
            critical_limits=record["critical_limits"],
            allergens_present=record["allergens_present"],
            cross_contact_warnings=record["cross_contact_warnings"],
            steps=record["steps"],
            escalation_required=record["escalation_required"],
        )
        return output.model_dump()

    return execute_instrumented_tool(
        agent_name="sop_allergen_agent",
        tool_name="lookup_food_safety_sop",
        intended_action=f"Verify food safety / allergen SOP for topic='{topic}'",
        raw_args={"topic": topic, "store_region": store_region},
        handler=_run,
    )
