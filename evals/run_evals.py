"""Automated evaluation runner for ShiftPilot QSR golden dataset.

Executes all golden evaluation cases covering:
- Allergen safety & zero-guessing guardrails
- Guided error recovery (no unhandled crashes)
- Human-in-the-Loop (HITL) pauses for high-stakes actions
- Prompt injection & off-topic input guardrails
- Active PII redaction
- History compaction for long shift sessions

Run via:
    python -m evals.run_evals --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shiftpilot_agent.guardrails.policy_plugin import (
    check_allergen_output_safety,
    check_input_policy,
)
from shiftpilot_agent.memory.compaction import HistoryCompactor
from shiftpilot_agent.observability.logging_config import get_logger
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.tools import (
    check_store_inventory,
    diagnose_kitchen_equipment,
    dispatch_maintenance_vendor,
    issue_guest_recovery_voucher,
    lookup_food_safety_sop,
    request_shift_coverage,
)

logger = get_logger("shiftpilot.evals")

_TOOL_REGISTRY = {
    "lookup_food_safety_sop": lookup_food_safety_sop,
    "diagnose_kitchen_equipment": diagnose_kitchen_equipment,
    "check_store_inventory": check_store_inventory,
    "dispatch_maintenance_vendor": dispatch_maintenance_vendor,
    "request_shift_coverage": request_shift_coverage,
    "issue_guest_recovery_voucher": issue_guest_recovery_voucher,
}


@dataclass
class EvalCaseResult:
    case_id: str
    category: str
    passed: bool
    detail: str


def _evaluate_case(case: dict[str, Any]) -> EvalCaseResult:
    case_id = case["id"]
    category = case["category"]
    action = case["action"]
    expected = case["expected"]

    if action == "tool_call":
        tool_fn = _TOOL_REGISTRY[case["tool"]]
        result = tool_fn(**case["args"])
        if result.get("status") != expected["status"]:
            return EvalCaseResult(
                case_id,
                category,
                False,
                f"Expected status={expected['status']}, got {result.get('status')}",
            )
        if "error_code" in expected and result.get("error_code") != expected["error_code"]:
            return EvalCaseResult(
                case_id,
                category,
                False,
                f"Expected error_code={expected['error_code']}, got {result.get('error_code')}",
            )
        if expected.get("must_have_recovery_instructions") and not result.get("recovery_instructions"):
            return EvalCaseResult(case_id, category, False, "Missing recovery_instructions")
        if expected.get("must_have_approval_token") and not str(result.get("approval_token", "")).startswith("MGR-APPROVE-"):
            return EvalCaseResult(case_id, category, False, "Missing MGR-APPROVE-* token")
        if "escalation_required" in expected and result.get("escalation_required") != expected["escalation_required"]:
            return EvalCaseResult(case_id, category, False, "Mismatch on escalation_required")
        for key in expected.get("must_contain_keys", []):
            if key not in result:
                return EvalCaseResult(case_id, category, False, f"Missing expected key '{key}'")
        return EvalCaseResult(case_id, category, True, "Tool contract verified")

    if action == "allergen_output_guardrail":
        verdict = check_allergen_output_safety(case["input_text"])
        ok = verdict.allowed == expected["allowed"] and verdict.policy_code == expected["policy_code"]
        return EvalCaseResult(
            case_id,
            category,
            ok,
            f"allowed={verdict.allowed}, policy_code={verdict.policy_code}",
        )

    if action == "input_guardrail":
        verdict = check_input_policy(case["input_text"])
        ok = verdict.allowed == expected["allowed"] and verdict.policy_code == expected["policy_code"]
        return EvalCaseResult(
            case_id,
            category,
            ok,
            f"allowed={verdict.allowed}, policy_code={verdict.policy_code}",
        )

    if action == "pii_redaction":
        redacted = redact_pii(case["input_text"])
        for forbidden in expected["forbidden_substrings"]:
            if forbidden in redacted:
                return EvalCaseResult(case_id, category, False, f"Leaked PII substring: {forbidden}")
        for marker in expected["required_markers"]:
            if marker not in redacted:
                return EvalCaseResult(case_id, category, False, f"Missing redaction marker: {marker}")
        return EvalCaseResult(case_id, category, True, "All PII patterns scrubbed")

    if action == "history_compaction":
        compactor = HistoryCompactor(window_turns=case["window_turns"], max_tokens=500)
        turns = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": (
                    f"Turn {i}: Store SP-1042 fryer Vat 1 reported HI-LIMIT during dinner rush; "
                    f"checked inventory for brioche buns and updated shift handoff log #{i}."
                ),
            }
            for i in range(case["turn_count"])
        ]
        compacted = compactor.compact(turns)
        ok = (
            compacted.was_compacted == expected["was_compacted"]
            and len(compacted.recent_turns) <= expected["max_recent_turns"]
            and (
                not expected.get("tokens_reduced")
                or compacted.estimated_tokens_after < compacted.estimated_tokens_before
            )
        )
        return EvalCaseResult(
            case_id,
            category,
            ok,
            (
                f"compacted={compacted.was_compacted}, "
                f"tokens={compacted.estimated_tokens_before}->{compacted.estimated_tokens_after}"
            ),
        )

    return EvalCaseResult(case_id, category, False, f"Unknown eval action: {action}")


def run_golden_evals(dataset_path: Path | None = None) -> dict[str, Any]:
    """Execute the full golden dataset and return a structured summary report."""
    path = dataset_path or Path(__file__).parent / "golden_dataset.json"
    dataset = json.loads(path.read_text(encoding="utf-8"))
    results = [_evaluate_case(case) for case in dataset["cases"]]

    passed_count = sum(1 for r in results if r.passed)
    total_count = len(results)
    pass_rate = round((passed_count / max(total_count, 1)) * 100.0, 2)

    report = {
        "suite_name": dataset["suite_name"],
        "version": dataset["version"],
        "passed": passed_count,
        "total": total_count,
        "pass_rate_pct": pass_rate,
        "results": [
            {
                "case_id": r.case_id,
                "category": r.category,
                "passed": r.passed,
                "detail": r.detail,
            }
            for r in results
        ],
    }
    logger.info(
        "golden_eval_suite_completed",
        passed=passed_count,
        total=total_count,
        pass_rate_pct=pass_rate,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ShiftPilot QSR Golden Evaluation Suite.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any eval fails.")
    args = parser.parse_args()

    report = run_golden_evals()
    if args.strict and report["passed"] != report["total"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
