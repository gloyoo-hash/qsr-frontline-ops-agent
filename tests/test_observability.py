"""Unit & integration tests for Pillar 4 (Observability, Tracing, PII) & Security."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shiftpilot_agent.observability.logging_config import configure_logging
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.observability.tracing import configure_tracing, traced_span
from shiftpilot_agent.security.secrets import SecretProvider
from shiftpilot_agent.tools import diagnose_kitchen_equipment


def test_pii_redactor_masks_all_sensitive_types() -> None:
    sample = (
        "Employee EMP-90210 called guest at (415) 555-0199 / jane.doe@example.com "
        "regarding card 4532-1234-5678-9012 and SSN 123-45-6789."
    )
    scrubbed = redact_pii(sample)
    for leaked in ("EMP-90210", "415", "jane.doe@example.com", "4532-1234-5678-9012", "123-45-6789"):
        assert leaked not in scrubbed
    assert "[REDACTED_EMPLOYEE_ID]" in scrubbed
    assert "[REDACTED_PHONE]" in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_CARD]" in scrubbed
    assert "[REDACTED_SSN]" in scrubbed


def test_structured_json_logging_intent_vs_outcome_and_otel_spans(capsys) -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(exporter=exporter)
    configure_logging(level="INFO", force=True)

    with traced_span("agent.shift_coordinator", {"agent.name": "shift_coordinator"}):
        with traced_span("agent.kitchen_ops_agent", {"agent.name": "kitchen_ops_agent"}):
            result = diagnose_kitchen_equipment(
                store_id="SP-1042",
                equipment_type="fryer",
                error_code="E-42",
                symptoms="Reported by EMP-554433 at 555-321-9876",
            )

    assert result["status"] == "ok"

    # Verify structured JSON logs with tool_intent BEFORE tool_outcome + PII scrubbed
    captured = capsys.readouterr().out
    json_events = [
        json.loads(line)
        for line in captured.splitlines()
        if line.strip().startswith("{")
    ]

    intent_indices = [i for i, e in enumerate(json_events) if e.get("event") == "tool_intent"]
    outcome_indices = [i for i, e in enumerate(json_events) if e.get("event") == "tool_outcome"]
    assert len(intent_indices) >= 1
    assert len(outcome_indices) >= 1
    assert intent_indices[-1] < outcome_indices[-1]

    intent = json_events[intent_indices[-1]]
    outcome = json_events[outcome_indices[-1]]
    assert intent["tool_name"] == "diagnose_kitchen_equipment"
    assert intent["agent_name"] == "kitchen_ops_agent"
    assert "timestamp" in intent
    assert intent["trace_id"] != "0" * 32
    assert intent["trace_id"] == outcome["trace_id"]
    assert "EMP-554433" not in json.dumps(intent)
    assert "555-321-9876" not in json.dumps(intent)
    assert outcome["status"] == "ok"
    assert isinstance(outcome["latency_ms"], (int, float))

    # Verify OpenTelemetry Coordinator -> Sub-Agent -> Tool trace hierarchy
    spans = exporter.get_finished_spans()
    span_names = {s.name for s in spans}
    assert "agent.shift_coordinator" in span_names
    assert "agent.kitchen_ops_agent" in span_names
    assert "tool.diagnose_kitchen_equipment" in span_names
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1  # Shared distributed trace context


def test_secret_provider_env_fallback_and_zero_raw_prints(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-ephemeral-key-not-real")
    provider = SecretProvider(project_id="", use_gcp=False)
    assert provider.get_secret("GOOGLE_API_KEY", "shiftpilot-google-api-key") == "test-ephemeral-key-not-real"

    # Verify zero raw print() function calls anywhere in shiftpilot_agent/ via AST
    package_root = Path(__file__).resolve().parent.parent / "shiftpilot_agent"
    for py_file in package_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", f"Found raw print() call in {py_file}"
