"""Central configuration for ShiftPilot QSR Frontline Operations Agent.

Encapsulates strategic model routing, HITL approval thresholds, persistence
backend configuration, and secret resolution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from shiftpilot_agent.security.secrets import get_secret


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Strategic model routing: fast lookups on Flash, complex reasoning on Pro."""

    fast_lookup_model: str = "gemini-2.5-flash"
    complex_reasoning_model: str = "gemini-2.5-pro"


@dataclass(frozen=True)
class HITLThresholds:
    """Human-in-the-Loop financial and operational approval thresholds."""

    maintenance_cost_usd: float = 250.0
    guest_voucher_usd: float = 25.0
    overtime_requires_approval: bool = True


@dataclass(frozen=True)
class CompactionConfig:
    """History compaction parameters for long 8-12 hour QSR shift sessions."""

    window_turns: int = 8
    max_tokens: int = 6000


@dataclass(frozen=True)
class AppConfig:
    """Runtime application settings loaded from Secret Manager / environment."""

    brand_name: str = "ShiftPilot QSR"
    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    memory_backend: str = "sqlite"
    sqlite_db_path: str = ".shiftpilot_data/shiftpilot_state.db"
    firestore_collection_prefix: str = "shiftpilot"
    enable_cloud_dlp: bool = False
    otel_service_name: str = "qsr-frontline-ops-agent"
    models: ModelRoutingConfig = ModelRoutingConfig()
    hitl: HITLThresholds = HITLThresholds()
    compaction: CompactionConfig = CompactionConfig()

    @property
    def google_api_key(self) -> str | None:
        """Resolve GOOGLE_API_KEY dynamically from Secret Manager or env."""
        secret_name = os.getenv("SECRET_NAME_GOOGLE_API_KEY", "shiftpilot-google-api-key")
        return get_secret(env_var="GOOGLE_API_KEY", secret_name=secret_name)


def load_config() -> AppConfig:
    """Construct an immutable `AppConfig` from environment variables."""
    return AppConfig(
        gcp_project_id=os.getenv("GCP_PROJECT_ID", ""),
        gcp_region=os.getenv("GCP_REGION", "us-central1"),
        memory_backend=os.getenv("MEMORY_BACKEND", "sqlite").lower(),
        sqlite_db_path=os.getenv("SQLITE_DB_PATH", ".shiftpilot_data/shiftpilot_state.db"),
        firestore_collection_prefix=os.getenv("FIRESTORE_COLLECTION_PREFIX", "shiftpilot"),
        enable_cloud_dlp=os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true",
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", "qsr-frontline-ops-agent"),
        models=ModelRoutingConfig(
            fast_lookup_model=os.getenv("MODEL_FAST_LOOKUP", "gemini-2.5-flash"),
            complex_reasoning_model=os.getenv("MODEL_COMPLEX_REASONING", "gemini-2.5-pro"),
        ),
        hitl=HITLThresholds(
            maintenance_cost_usd=float(os.getenv("HITL_MAINTENANCE_COST_THRESHOLD_USD", "250.0")),
            guest_voucher_usd=float(os.getenv("HITL_VOUCHER_AMOUNT_THRESHOLD_USD", "25.0")),
            overtime_requires_approval=True,
        ),
        compaction=CompactionConfig(
            window_turns=int(os.getenv("COMPACTION_WINDOW_TURNS", "8")),
            max_tokens=int(os.getenv("COMPACTION_MAX_TOKENS", "6000")),
        ),
    )


CONFIG = load_config()
