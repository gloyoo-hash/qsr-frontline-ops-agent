"""Context & memory management: persistent backend, history compaction, async consolidation."""

from shiftpilot_agent.memory.async_consolidator import AsyncMemoryConsolidator
from shiftpilot_agent.memory.compaction import (
    HistoryCompactor,
    adk_history_compaction_callback,
)
from shiftpilot_agent.memory.store_backend import (
    FirestoreStoreBackend,
    SQLiteStoreBackend,
    StoreMemoryBackend,
    create_store_backend,
    get_default_store_backend,
    set_default_store_backend,
)

__all__ = [
    "AsyncMemoryConsolidator",
    "FirestoreStoreBackend",
    "HistoryCompactor",
    "SQLiteStoreBackend",
    "StoreMemoryBackend",
    "adk_history_compaction_callback",
    "create_store_backend",
    "get_default_store_backend",
    "set_default_store_backend",
]
