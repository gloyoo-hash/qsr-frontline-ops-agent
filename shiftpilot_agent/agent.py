"""Google ADK Multi-Agent Architecture for ShiftPilot QSR Frontline Operations.

Orchestration Topology:
- Root Coordinator (`shift_coordinator`, model=`gemini-2.5-pro`):
  Routes incoming frontline worker requests to specialized sub-agents:
  1. `sop_allergen_agent` (model=`gemini-2.5-flash`):
     Fast, deterministic food-safety & allergen SOP lookups.
  2. `kitchen_ops_agent` (model=`gemini-2.5-pro`):
     Complex equipment troubleshooting, inventory checks, and vendor dispatch (HITL > $250).
  3. `shift_recovery_agent` (model=`gemini-2.5-pro`):
     Shift coverage planning (HITL for overtime) and guest recovery vouchers (HITL > $25).
  4. `equipment_incident_pipeline` (`SequentialAgent`):
     Two-stage sequential workflow (`incident_triage_agent` -> `incident_mitigation_agent`)
     for multi-step kitchen equipment breakdowns affecting food safety and inventory.

Guardrails, history compaction, self-evaluation, and OpenTelemetry tracing are
wired via ADK callbacks and `ShiftPilotPolicyPlugin`.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent, SequentialAgent

from shiftpilot_agent.config import CONFIG
from shiftpilot_agent.guardrails.policy_plugin import (
    ShiftPilotPolicyPlugin,
    check_allergen_output_safety,
    check_input_policy,
    self_evaluate_response,
)
from shiftpilot_agent.memory.compaction import adk_history_compaction_callback
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.prompts import (
    COORDINATOR_INSTRUCTION,
    INCIDENT_MITIGATION_STEP_INSTRUCTION,
    INCIDENT_TRIAGE_STEP_INSTRUCTION,
    KITCHEN_OPS_INSTRUCTION,
    SHIFT_RECOVERY_INSTRUCTION,
    SOP_ALLERGEN_INSTRUCTION,
)
from shiftpilot_agent.tools import (
    check_store_inventory,
    diagnose_kitchen_equipment,
    dispatch_maintenance_vendor,
    issue_guest_recovery_voucher,
    lookup_food_safety_sop,
    request_shift_coverage,
)


def _before_model_guardrail_and_compaction(callback_context: Any, llm_request: Any) -> Any:
    """Agent-level `before_model_callback` combining compaction and input policy checks."""
    adk_history_compaction_callback(callback_context, llm_request)
    contents = getattr(llm_request, "contents", None) or []
    if contents:
        parts = getattr(contents[-1], "parts", None) or []
        text = " ".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None))
        verdict = check_input_policy(text)
        if not verdict.allowed:
            from google.adk.models import LlmResponse
            from google.genai import types

            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=verdict.safe_replacement or verdict.reason)],
                )
            )
    return None


def _after_model_safety_and_self_eval(callback_context: Any, llm_response: Any) -> Any:
    """Agent-level `after_model_callback` enforcing allergen safety & self-evaluation."""
    content = getattr(llm_response, "content", None)
    parts = getattr(content, "parts", None) or []
    text = " ".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None))
    if not text:
        return None

    verdict = check_allergen_output_safety(text)
    eval_result = self_evaluate_response(text)
    if not verdict.allowed:
        from google.adk.models import LlmResponse
        from google.genai import types

        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=verdict.safe_replacement or verdict.reason)],
            )
        )
    if "UNREDACTED_PII_IN_OUTPUT" in eval_result.violations:
        from google.adk.models import LlmResponse
        from google.genai import types

        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=redact_pii(text))],
            )
        )
    return None


# 1. Fast Lookup Specialist -> gemini-2.5-flash
sop_allergen_agent = Agent(
    name="sop_allergen_agent",
    model=CONFIG.models.fast_lookup_model,
    description=(
        "Fast specialist for ShiftPilot QSR food safety SOPs, allergen verification, "
        "cooking/holding temperatures, and cross-contact rules."
    ),
    instruction=SOP_ALLERGEN_INSTRUCTION,
    tools=[lookup_food_safety_sop],
    before_model_callback=_before_model_guardrail_and_compaction,
    after_model_callback=_after_model_safety_and_self_eval,
)

# 2. Complex Kitchen Equipment & Inventory Specialist -> gemini-2.5-pro
kitchen_ops_agent = Agent(
    name="kitchen_ops_agent",
    model=CONFIG.models.complex_reasoning_model,
    description=(
        "Specialist for diagnosing kitchen equipment error codes, checking store "
        "inventory & 86 risks, and dispatching maintenance vendors (with HITL > $250)."
    ),
    instruction=KITCHEN_OPS_INSTRUCTION,
    tools=[
        diagnose_kitchen_equipment,
        check_store_inventory,
        dispatch_maintenance_vendor,
    ],
    before_model_callback=_before_model_guardrail_and_compaction,
    after_model_callback=_after_model_safety_and_self_eval,
)

# 3. Shift Coverage & Guest Recovery Specialist -> gemini-2.5-pro
shift_recovery_agent = Agent(
    name="shift_recovery_agent",
    model=CONFIG.models.complex_reasoning_model,
    description=(
        "Specialist for frontline shift coverage requests (HITL for overtime) and "
        "hospitality guest recovery vouchers (HITL > $25)."
    ),
    instruction=SHIFT_RECOVERY_INSTRUCTION,
    tools=[
        request_shift_coverage,
        issue_guest_recovery_voucher,
    ],
    before_model_callback=_before_model_guardrail_and_compaction,
    after_model_callback=_after_model_safety_and_self_eval,
)

# 4. Sequential Workflow for End-to-End Equipment Incident Response
incident_triage_agent = Agent(
    name="incident_triage_agent",
    model=CONFIG.models.fast_lookup_model,
    description="Step 1: Diagnose equipment fault and verify related food-safety SOP.",
    instruction=INCIDENT_TRIAGE_STEP_INSTRUCTION,
    tools=[diagnose_kitchen_equipment, lookup_food_safety_sop],
)

incident_mitigation_agent = Agent(
    name="incident_mitigation_agent",
    model=CONFIG.models.complex_reasoning_model,
    description="Step 2: Check impacted store inventory and plan/dispatch maintenance.",
    instruction=INCIDENT_MITIGATION_STEP_INSTRUCTION,
    tools=[check_store_inventory, dispatch_maintenance_vendor],
)

equipment_incident_pipeline = SequentialAgent(
    name="equipment_incident_pipeline",
    description=(
        "Sequential two-step workflow for major kitchen equipment incidents: "
        "triage & food-safety hold check followed by inventory impact & vendor dispatch."
    ),
    sub_agents=[incident_triage_agent, incident_mitigation_agent],
)

# 5. Root Coordinator -> gemini-2.5-pro
shift_coordinator = Agent(
    name="shift_coordinator",
    model=CONFIG.models.complex_reasoning_model,
    description=(
        "ShiftPilot QSR Root Frontline Operations Coordinator delegating to food safety, "
        "kitchen ops, shift/guest recovery, and sequential equipment incident sub-agents."
    ),
    instruction=COORDINATOR_INSTRUCTION,
    sub_agents=[
        sop_allergen_agent,
        kitchen_ops_agent,
        shift_recovery_agent,
        equipment_incident_pipeline,
    ],
    before_model_callback=_before_model_guardrail_and_compaction,
    after_model_callback=_after_model_safety_and_self_eval,
)

# Canonical ADK root agent export & policy plugin instance
root_agent = shift_coordinator
policy_plugin = ShiftPilotPolicyPlugin()
