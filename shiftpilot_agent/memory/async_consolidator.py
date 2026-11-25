"""Async background memory consolidation using `asyncio.create_task`.

Summarizes shift transcripts into structured `ShiftHandoffNote` records and
persists them to SQLite/Firestore asynchronously so frontline responses are
never blocked by memory consolidation I/O.
"""

from __future__ import annotations

import asyncio
from typing import Any

from shiftpilot_agent.memory.store_backend import (
    StoreMemoryBackend,
    get_default_store_backend,
)
from shiftpilot_agent.observability.logging_config import get_logger
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.observability.tracing import traced_span
from shiftpilot_agent.schemas.models import ShiftHandoffNote

logger = get_logger(__name__)


class AsyncMemoryConsolidator:
    """Schedules non-blocking background consolidation of shift handoff notes."""

    def __init__(self, backend: StoreMemoryBackend | None = None) -> None:
        self.backend = backend or get_default_store_backend()
        self._tasks: set[asyncio.Task[ShiftHandoffNote]] = set()

    def _extract_handoff_note(
        self,
        store_id: str,
        session_id: str,
        transcript: list[dict[str, str]],
    ) -> ShiftHandoffNote:
        """Synthesize a PII-redacted shift handoff summary from conversation turns."""
        equipment_issues: list[str] = []
        inventory_alerts: list[str] = []
        key_lines: list[str] = []

        for turn in transcript:
            text = redact_pii(turn.get("content", "")).strip()
            if not text:
                continue
            lower = text.lower()
            if any(w in lower for w in ("fryer", "cooler", "grill", "soft_serve", "hi-limit", "e-42", "temp-warn")):
                equipment_issues.append(text[:160])
            if any(w in lower for w in ("86", "out of stock", "low stock", "inventory", "buns", "cups", "oil")):
                inventory_alerts.append(text[:160])
            if turn.get("role") in ("user", "assistant", "model"):
                key_lines.append(text[:120])

        summary = (
            f"Shift handoff for {store_id} ({len(transcript)} turns): "
            + ("; ".join(key_lines[-3:]) if key_lines else "Routine shift operations.")
        )
        return ShiftHandoffNote(
            store_id=store_id,
            session_id=session_id,
            summary=redact_pii(summary),
            open_equipment_issues=equipment_issues[:5],
            low_inventory_alerts=inventory_alerts[:5],
        )

    async def _consolidate_worker(
        self,
        store_id: str,
        session_id: str,
        transcript: list[dict[str, str]],
    ) -> ShiftHandoffNote:
        with traced_span(
            "memory.async_consolidate",
            attributes={"store.id": store_id, "session.id": session_id},
        ):
            # Yield control to the event loop to keep user-facing latency unblocked
            await asyncio.sleep(0)
            note = self._extract_handoff_note(store_id, session_id, transcript)
            await asyncio.to_thread(self.backend.save_handoff_note, note)
            logger.info(
                "async_memory_consolidated",
                store_id=store_id,
                session_id=session_id,
                equipment_issue_count=len(note.open_equipment_issues),
                inventory_alert_count=len(note.low_inventory_alerts),
            )
            return note

    def schedule_consolidation(
        self,
        store_id: str,
        session_id: str,
        transcript: list[dict[str, str]],
    ) -> asyncio.Task[ShiftHandoffNote]:
        """Fire-and-forget background task via `asyncio.create_task`."""
        task: asyncio.Task[ShiftHandoffNote] = asyncio.create_task(
            self._consolidate_worker(store_id, session_id, transcript)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def flush(self) -> list[Any]:
        """Await all in-flight background consolidation tasks (for graceful shutdown/tests)."""
        if not self._tasks:
            return []
        return list(await asyncio.gather(*list(self._tasks), return_exceptions=False))
