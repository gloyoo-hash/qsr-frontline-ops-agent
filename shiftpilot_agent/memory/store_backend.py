"""Persistent SQLite (local) and Firestore (GCP) session & store-memory backend.

Tracks per-store operational state across turns and shifts:
- Active `store_id` & session metadata
- Recurring equipment fault counters (`equipment_type:error_code -> count`)
- Consolidated shift handoff notes (PII-scrubbed BEFORE persistence)
"""

from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shiftpilot_agent.config import CONFIG
from shiftpilot_agent.observability.logging_config import get_logger
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.schemas.models import ShiftHandoffNote, StoreMemoryRecord

logger = get_logger(__name__)


class StoreMemoryBackend(ABC):
    """Abstract interface for persistent store & session state."""

    @abstractmethod
    def record_equipment_fault(self, store_id: str, fault_key: str) -> int:
        """Increment and return the recurring fault count for a store."""

    @abstractmethod
    def save_handoff_note(self, note: ShiftHandoffNote) -> None:
        """Persist a PII-redacted shift handoff note."""

    @abstractmethod
    def get_store_memory(self, store_id: str) -> StoreMemoryRecord:
        """Load full recurring fault history and shift handoff notes for a store."""

    @abstractmethod
    def upsert_session_state(self, session_id: str, store_id: str, state: dict[str, Any]) -> None:
        """Persist session state keyed by session_id."""

    @abstractmethod
    def get_session_state(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve persisted session state if present."""


class SQLiteStoreBackend(StoreMemoryBackend):
    """Local persistent SQLite backend for development, testing, and edge deployments."""

    def __init__(self, db_path: str | Path = ".shiftpilot_data/shiftpilot_state.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS equipment_faults (
                    store_id TEXT NOT NULL,
                    fault_key TEXT NOT NULL,
                    fault_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (store_id, fault_key)
                );

                CREATE TABLE IF NOT EXISTS handoff_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    open_equipment_issues TEXT NOT NULL,
                    low_inventory_alerts TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    store_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def record_equipment_fault(self, store_id: str, fault_key: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        clean_store = redact_pii(store_id)
        clean_key = redact_pii(fault_key)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO equipment_faults (store_id, fault_key, fault_count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(store_id, fault_key)
                DO UPDATE SET fault_count = fault_count + 1, updated_at = excluded.updated_at
                """,
                (clean_store, clean_key, now),
            )
            row = conn.execute(
                "SELECT fault_count FROM equipment_faults WHERE store_id = ? AND fault_key = ?",
                (clean_store, clean_key),
            ).fetchone()
            return int(row["fault_count"]) if row else 1

    def save_handoff_note(self, note: ShiftHandoffNote) -> None:
        safe_summary = redact_pii(note.summary)
        safe_issues = redact_pii(note.open_equipment_issues)
        safe_alerts = redact_pii(note.low_inventory_alerts)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO handoff_notes
                    (store_id, session_id, summary, open_equipment_issues, low_inventory_alerts, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    note.store_id,
                    note.session_id,
                    safe_summary,
                    json.dumps(safe_issues),
                    json.dumps(safe_alerts),
                    note.created_at,
                ),
            )
        logger.info(
            "memory_handoff_persisted",
            backend="sqlite",
            store_id=note.store_id,
            session_id=note.session_id,
        )

    def get_store_memory(self, store_id: str) -> StoreMemoryRecord:
        with self._connect() as conn:
            fault_rows = conn.execute(
                "SELECT fault_key, fault_count FROM equipment_faults WHERE store_id = ?",
                (store_id,),
            ).fetchall()
            note_rows = conn.execute(
                """
                SELECT store_id, session_id, summary, open_equipment_issues,
                       low_inventory_alerts, created_at
                FROM handoff_notes
                WHERE store_id = ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (store_id,),
            ).fetchall()

        faults = {str(r["fault_key"]): int(r["fault_count"]) for r in fault_rows}
        notes = [
            ShiftHandoffNote(
                store_id=str(r["store_id"]),
                session_id=str(r["session_id"]),
                summary=str(r["summary"]),
                open_equipment_issues=json.loads(r["open_equipment_issues"]),
                low_inventory_alerts=json.loads(r["low_inventory_alerts"]),
                created_at=str(r["created_at"]),
            )
            for r in note_rows
        ]
        return StoreMemoryRecord(
            store_id=store_id,
            recurring_faults=faults,
            handoff_notes=notes,
        )

    def upsert_session_state(self, session_id: str, store_id: str, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        safe_state = redact_pii(state)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_state (session_id, store_id, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id)
                DO UPDATE SET
                    store_id = excluded.store_id,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, store_id, json.dumps(safe_state), now),
            )

    def get_session_state(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT store_id, state_json, updated_at FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": session_id,
            "store_id": str(row["store_id"]),
            "state": json.loads(row["state_json"]),
            "updated_at": str(row["updated_at"]),
        }


class FirestoreStoreBackend(StoreMemoryBackend):
    """Google Cloud Firestore backend for multi-instance Cloud Run deployments."""

    def __init__(
        self,
        project_id: str | None = None,
        collection_prefix: str = "shiftpilot",
    ) -> None:
        from google.cloud import firestore  # type: ignore[import-untyped]

        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "")
        self.prefix = collection_prefix
        self.client = firestore.Client(project=self.project_id or None)

    def _stores_col(self) -> Any:
        return self.client.collection(f"{self.prefix}_stores")

    def _sessions_col(self) -> Any:
        return self.client.collection(f"{self.prefix}_sessions")

    def record_equipment_fault(self, store_id: str, fault_key: str) -> int:
        from google.cloud import firestore  # type: ignore[import-untyped]

        doc_ref = self._stores_col().document(store_id)
        doc_ref.set(
            {
                "store_id": store_id,
                "recurring_faults": {fault_key: firestore.Increment(1)},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            merge=True,
        )
        snapshot = doc_ref.get()
        data = snapshot.to_dict() or {}
        return int((data.get("recurring_faults") or {}).get(fault_key, 1))

    def save_handoff_note(self, note: ShiftHandoffNote) -> None:
        safe_payload = redact_pii(note.model_dump())
        (
            self._stores_col()
            .document(note.store_id)
            .collection("handoff_notes")
            .add(safe_payload)
        )
        logger.info(
            "memory_handoff_persisted",
            backend="firestore",
            store_id=note.store_id,
            session_id=note.session_id,
        )

    def get_store_memory(self, store_id: str) -> StoreMemoryRecord:
        doc = self._stores_col().document(store_id).get()
        data = doc.to_dict() or {}
        faults = {k: int(v) for k, v in (data.get("recurring_faults") or {}).items()}
        note_docs = (
            self._stores_col()
            .document(store_id)
            .collection("handoff_notes")
            .order_by("created_at", direction="DESCENDING")
            .limit(20)
            .stream()
        )
        notes = [ShiftHandoffNote(**d.to_dict()) for d in note_docs]
        return StoreMemoryRecord(
            store_id=store_id,
            recurring_faults=faults,
            handoff_notes=notes,
        )

    def upsert_session_state(self, session_id: str, store_id: str, state: dict[str, Any]) -> None:
        self._sessions_col().document(session_id).set(
            {
                "session_id": session_id,
                "store_id": store_id,
                "state": redact_pii(state),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            merge=True,
        )

    def get_session_state(self, session_id: str) -> dict[str, Any] | None:
        doc = self._sessions_col().document(session_id).get()
        return doc.to_dict() if doc.exists else None


def create_store_backend(
    backend_type: str | None = None,
    sqlite_path: str | Path | None = None,
) -> StoreMemoryBackend:
    """Factory creating either SQLite (local) or Firestore (cloud) backend."""
    selected = (backend_type or CONFIG.memory_backend).lower()
    if selected == "firestore":
        return FirestoreStoreBackend(
            project_id=CONFIG.gcp_project_id,
            collection_prefix=CONFIG.firestore_collection_prefix,
        )
    return SQLiteStoreBackend(db_path=sqlite_path or CONFIG.sqlite_db_path)


_DEFAULT_BACKEND: StoreMemoryBackend | None = None


def get_default_store_backend() -> StoreMemoryBackend:
    """Return process-wide persistent store backend (lazily initialized)."""
    global _DEFAULT_BACKEND
    if _DEFAULT_BACKEND is None:
        _DEFAULT_BACKEND = create_store_backend()
    return _DEFAULT_BACKEND


def set_default_store_backend(backend: StoreMemoryBackend) -> None:
    """Override the default store backend (useful for isolated tests)."""
    global _DEFAULT_BACKEND
    _DEFAULT_BACKEND = backend
