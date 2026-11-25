"""Unit & integration tests for Pillar 2 (Context & Memory)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from shiftpilot_agent.memory.async_consolidator import AsyncMemoryConsolidator
from shiftpilot_agent.memory.compaction import (
    HistoryCompactor,
    adk_history_compaction_callback,
)
from shiftpilot_agent.memory.store_backend import SQLiteStoreBackend
from shiftpilot_agent.schemas.models import ShiftHandoffNote


def test_history_compaction_reduces_turns_and_tokens() -> None:
    compactor = HistoryCompactor(window_turns=4, max_tokens=200)
    turns = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": (
                f"Shift update {i}: Store SP-1042 fryer Vat 1 HI-LIMIT triggered; "
                f"crew EMP-112233 checked brioche buns inventory and logged SOP-FS-204."
            ),
        }
        for i in range(14)
    ]

    result = compactor.compact(turns)
    assert result.was_compacted is True
    assert len(result.recent_turns) <= 4
    assert result.estimated_tokens_after < result.estimated_tokens_before
    assert "SHIFT HISTORY SUMMARY" in result.summary
    # PII must be redacted inside compacted summaries
    assert "EMP-112233" not in result.summary
    assert "[REDACTED_EMPLOYEE_ID]" in result.summary


def test_adk_history_compaction_callback_rewrites_llm_request() -> None:
    from google.genai import types

    contents = [
        types.Content(
            role="user" if i % 2 == 0 else "model",
            parts=[types.Part.from_text(text=f"Turn {i}: Store SP-1042 cooler temp check.")],
        )
        for i in range(12)
    ]
    fake_request = SimpleNamespace(contents=contents)
    adk_history_compaction_callback(callback_context=None, llm_request=fake_request)
    assert len(fake_request.contents) < 12
    assert "SHIFT HISTORY SUMMARY" in fake_request.contents[0].parts[0].text


def test_sqlite_persistent_session_and_store_memory_across_restarts(tmp_path: Path) -> None:
    db_file = tmp_path / "persistent.db"

    backend_a = SQLiteStoreBackend(db_path=db_file)
    assert backend_a.record_equipment_fault("SP-1042", "fryer:HI-LIMIT") == 1
    assert backend_a.record_equipment_fault("SP-1042", "fryer:HI-LIMIT") == 2
    backend_a.upsert_session_state(
        session_id="sess-001",
        store_id="SP-1042",
        state={"shift": "dinner", "crew_phone": "555-867-5309"},
    )
    backend_a.save_handoff_note(
        ShiftHandoffNote(
            store_id="SP-1042",
            session_id="sess-001",
            summary="Fryer Vat 1 locked out; call EMP-998877 for morning filter.",
            open_equipment_issues=["fryer:HI-LIMIT"],
            low_inventory_alerts=["SKU-BUN-01"],
        )
    )

    # Simulate process restart with a brand new backend instance pointing at same DB
    backend_b = SQLiteStoreBackend(db_path=db_file)
    memory = backend_b.get_store_memory("SP-1042")
    assert memory.recurring_faults["fryer:HI-LIMIT"] == 2
    assert len(memory.handoff_notes) == 1
    assert "EMP-998877" not in memory.handoff_notes[0].summary
    assert "[REDACTED_EMPLOYEE_ID]" in memory.handoff_notes[0].summary

    session = backend_b.get_session_state("sess-001")
    assert session is not None
    assert session["store_id"] == "SP-1042"
    assert "555-867-5309" not in str(session["state"])


@pytest.mark.asyncio
async def test_async_memory_consolidation_runs_via_create_task(tmp_path: Path) -> None:
    backend = SQLiteStoreBackend(db_path=tmp_path / "async_mem.db")
    consolidator = AsyncMemoryConsolidator(backend=backend)

    transcript = [
        {
            "role": "user",
            "content": "Store SP-1042 walk-in cooler showing TEMP-WARN. Crew EMP-774411 checked.",
        },
        {
            "role": "assistant",
            "content": "Quarantine TCS items per SOP-FS-301 and check brioche buns low stock.",
        },
    ]

    task = consolidator.schedule_consolidation(
        store_id="SP-1042",
        session_id="sess-async-1",
        transcript=transcript,
    )
    assert not task.done()  # Non-blocking background task scheduled via asyncio.create_task
    results = await consolidator.flush()
    assert len(results) == 1

    memory = backend.get_store_memory("SP-1042")
    assert len(memory.handoff_notes) == 1
    saved = memory.handoff_notes[0]
    assert "EMP-774411" not in saved.summary
    assert len(saved.open_equipment_issues) >= 1
    assert len(saved.low_inventory_alerts) >= 1
