"""Unit & integration tests for Pillar 1 (Tool & Interface Design) and HITL hooks."""

from __future__ import annotations

import inspect

from shiftpilot_agent.tools import (
    ALL_TOOLS,
    HITL_REGISTRY,
    check_store_inventory,
    diagnose_kitchen_equipment,
    dispatch_maintenance_vendor,
    issue_guest_recovery_voucher,
    lookup_food_safety_sop,
    request_shift_coverage,
)


def test_all_six_required_tools_exist_with_google_docstrings() -> None:
    expected_names = {
        "lookup_food_safety_sop",
        "diagnose_kitchen_equipment",
        "check_store_inventory",
        "dispatch_maintenance_vendor",
        "request_shift_coverage",
        "issue_guest_recovery_voucher",
    }
    actual_names = {fn.__name__ for fn in ALL_TOOLS}
    assert actual_names == expected_names

    for fn in ALL_TOOLS:
        doc = inspect.getdoc(fn)
        assert doc is not None, f"{fn.__name__} missing docstring"
        assert "Args:" in doc, f"{fn.__name__} docstring missing Args section"
        assert "Returns:" in doc, f"{fn.__name__} docstring missing Returns section"


def test_lookup_food_safety_sop_success_and_guided_error() -> None:
    ok = lookup_food_safety_sop(topic="peanut_allergen", store_region="US-CA")
    assert ok["status"] == "ok"
    assert ok["sop_id"] == "SOP-ALG-101"
    assert ok["escalation_required"] is True
    assert len(ok["allergens_present"]) >= 1
    assert len(ok["cross_contact_warnings"]) >= 1

    err = lookup_food_safety_sop(topic="mystery_sauce_999")
    assert err["status"] == "error"
    assert err["error_code"] == "SOP_NOT_FOUND"
    assert "DO NOT guess" in err["recovery_instructions"]
    assert len(err["valid_options"]) >= 3

    schema_err = lookup_food_safety_sop(topic="a")
    assert schema_err["status"] == "error"
    assert schema_err["error_code"] == "INVALID_INPUT_SCHEMA"


def test_diagnose_kitchen_equipment_tracks_recurring_faults() -> None:
    first = diagnose_kitchen_equipment(
        store_id="SP-1042",
        equipment_type="fryer",
        error_code="HI-LIMIT",
        symptoms="Vat 1 shut off during lunch rush",
    )
    assert first["status"] == "ok"
    assert first["recurring_fault_count"] == 1
    assert first["requires_vendor_dispatch"] is True

    second = diagnose_kitchen_equipment(
        store_id="SP-1042",
        equipment_type="fryer",
        error_code="HI-LIMIT",
    )
    assert second["recurring_fault_count"] == 2

    invalid = diagnose_kitchen_equipment(
        store_id="BAD-ID",
        equipment_type="teleport_oven",
        error_code="X-1",
    )
    assert invalid["status"] == "error"
    assert invalid["error_code"] == "INVALID_INPUT_SCHEMA"
    assert invalid["recovery_instructions"]


def test_check_store_inventory_classifies_stock_and_handles_missing_items() -> None:
    low = check_store_inventory(store_id="SP-1042", item_sku_or_name="brioche buns")
    assert low["status"] == "ok"
    assert low["items"][0]["status"] == "critical_86_risk"

    oos = check_store_inventory(store_id="SP-1042", item_sku_or_name="SKU-CUP-16")
    assert oos["status"] == "ok"
    assert oos["items"][0]["status"] == "out_of_stock"
    assert "86" in oos["recommended_action"]

    missing_item = check_store_inventory(store_id="SP-1042", item_sku_or_name="truffle_oil")
    assert missing_item["status"] == "error"
    assert missing_item["error_code"] == "ITEM_NOT_FOUND"
    assert missing_item["valid_options"]


def test_hitl_maintenance_dispatch_pause_and_approved_execution() -> None:
    # Under $250 executes directly
    direct = dispatch_maintenance_vendor(
        store_id="SP-1042",
        equipment_type="fryer",
        issue_summary="Probe harness replacement on Vat 1",
        estimated_cost_usd=195.0,
    )
    assert direct["status"] == "dispatched"
    assert direct["approved_via_hitl"] is False

    # Over $250 MUST pause with PENDING_MANAGER_APPROVAL
    paused = dispatch_maintenance_vendor(
        store_id="SP-1042",
        equipment_type="walk_in_cooler",
        issue_summary="Compressor fault causing 46F ambient temp",
        estimated_cost_usd=480.0,
        urgency="emergency",
    )
    assert paused["status"] == "PENDING_MANAGER_APPROVAL"
    token = paused["approval_token"]
    assert token.startswith("MGR-APPROVE-")

    # Unapproved token is rejected gracefully
    rejected = dispatch_maintenance_vendor(
        store_id="SP-1042",
        equipment_type="walk_in_cooler",
        issue_summary="Compressor fault causing 46F ambient temp",
        estimated_cost_usd=480.0,
        urgency="emergency",
        manager_approval_token=token,
    )
    assert rejected["status"] == "error"
    assert rejected["error_code"] == "INVALID_OR_UNAPPROVED_HITL_TOKEN"

    # Human manager approves token -> execution succeeds
    assert HITL_REGISTRY.approve_token(token) is True
    dispatched = dispatch_maintenance_vendor(
        store_id="SP-1042",
        equipment_type="walk_in_cooler",
        issue_summary="Compressor fault causing 46F ambient temp",
        estimated_cost_usd=480.0,
        urgency="emergency",
        manager_approval_token=token,
    )
    assert dispatched["status"] == "dispatched"
    assert dispatched["approved_via_hitl"] is True


def test_hitl_guest_voucher_and_overtime_shift_coverage() -> None:
    # Voucher <= $25 auto-issues
    small_voucher = issue_guest_recovery_voucher(
        store_id="SP-1042",
        order_reference="ORD-1001",
        reason="Missing fries in drive-thru bag",
        amount_usd=15.0,
    )
    assert small_voucher["status"] == "issued"
    assert small_voucher["approved_via_hitl"] is False

    # Voucher > $25 pauses for manager approval
    big_voucher_pause = issue_guest_recovery_voucher(
        store_id="SP-1042",
        order_reference="ORD-1002",
        reason="Large catering order delayed 30 minutes",
        amount_usd=40.0,
    )
    assert big_voucher_pause["status"] == "PENDING_MANAGER_APPROVAL"
    v_token = big_voucher_pause["approval_token"]
    assert HITL_REGISTRY.approve_token(v_token) is True
    big_voucher_issued = issue_guest_recovery_voucher(
        store_id="SP-1042",
        order_reference="ORD-1002",
        reason="Large catering order delayed 30 minutes",
        amount_usd=40.0,
        manager_approval_token=v_token,
    )
    assert big_voucher_issued["status"] == "issued"
    assert big_voucher_issued["approved_via_hitl"] is True

    # Non-overtime shift coverage posts directly
    reg_shift = request_shift_coverage(
        store_id="SP-1042",
        role="cashier",
        shift_window="Today 12:00-16:00",
        reason="Unexpected lunch surge",
        is_overtime=False,
    )
    assert reg_shift["status"] == "posted"

    # Overtime shift coverage pauses for HITL approval
    ot_pause = request_shift_coverage(
        store_id="SP-1042",
        role="line_cook",
        shift_window="Today 17:00-23:00",
        reason="Dinner call-out requiring overtime",
        is_overtime=True,
    )
    assert ot_pause["status"] == "PENDING_MANAGER_APPROVAL"
    ot_token = ot_pause["approval_token"]
    assert HITL_REGISTRY.approve_token(ot_token) is True
    ot_posted = request_shift_coverage(
        store_id="SP-1042",
        role="line_cook",
        shift_window="Today 17:00-23:00",
        reason="Dinner call-out requiring overtime",
        is_overtime=True,
        manager_approval_token=ot_token,
    )
    assert ot_posted["status"] == "posted"
    assert ot_posted["approved_via_hitl"] is True
