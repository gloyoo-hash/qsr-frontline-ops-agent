"""Pytest fixtures providing isolated SQLite persistence and clean HITL state."""

from __future__ import annotations

from pathlib import Path

import pytest

from shiftpilot_agent.memory.store_backend import (
    SQLiteStoreBackend,
    set_default_store_backend,
)
from shiftpilot_agent.tools.base import HITL_REGISTRY


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path) -> SQLiteStoreBackend:
    """Ensure each test uses a fresh SQLite DB and empty HITL token registry."""
    backend = SQLiteStoreBackend(db_path=tmp_path / "test_shiftpilot.db")
    set_default_store_backend(backend)
    HITL_REGISTRY.clear()
    return backend
