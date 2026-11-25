"""Secure Secret Management with Google Cloud Secret Manager and environment fallback.

Zero hardcoded API keys or credentials exist in this codebase. In production
(Cloud Run / GKE), secrets are fetched from Google Cloud Secret Manager using
Workload Identity / IAM service accounts. For local development and CI tests,
environment variables (`GOOGLE_API_KEY`, `GCP_PROJECT_ID`, etc.) are used as a
safe fallback.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from shiftpilot_agent.observability.logging_config import get_logger

logger = get_logger(__name__)


class SecretProvider:
    """Resolves secrets from Google Cloud Secret Manager with env-var fallback."""

    def __init__(self, project_id: Optional[str] = None, use_gcp: Optional[bool] = None) -> None:
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "")
        if use_gcp is None:
            # Automatically attempt Secret Manager only when running on GCP or explicitly enabled
            self.use_gcp = bool(
                os.getenv("K_SERVICE") or os.getenv("USE_GCP_SECRET_MANAGER", "").lower() == "true"
            )
        else:
            self.use_gcp = use_gcp

    def _fetch_from_secret_manager(self, secret_name: str, version: str = "latest") -> Optional[str]:
        """Fetch a secret payload from Google Cloud Secret Manager."""
        if not self.project_id:
            return None
        try:
            from google.cloud import secretmanager  # type: ignore[import-untyped]

            client = secretmanager.SecretManagerServiceClient()
            resource_name = f"projects/{self.project_id}/secrets/{secret_name}/versions/{version}"
            response = client.access_secret_version(request={"name": resource_name})
            return response.payload.data.decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "secret_manager_fallback",
                secret_name=secret_name,
                reason=type(exc).__name__,
            )
            return None

    def get_secret(
        self,
        env_var: str,
        secret_name: Optional[str] = None,
        default: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve a secret value without ever logging its plaintext content.

        Resolution order:
        1. Google Cloud Secret Manager (when running in GCP or explicitly enabled).
        2. Local environment variable fallback (`env_var`).
        3. Optional default value.
        """
        if self.use_gcp and secret_name:
            gcp_val = self._fetch_from_secret_manager(secret_name)
            if gcp_val:
                return gcp_val

        env_val = os.getenv(env_var)
        if env_val is not None and env_val != "":
            return env_val

        return default


@lru_cache(maxsize=1)
def _default_provider() -> SecretProvider:
    return SecretProvider()


def get_secret(
    env_var: str,
    secret_name: Optional[str] = None,
    default: Optional[str] = None,
) -> Optional[str]:
    """Module-level convenience wrapper around `SecretProvider.get_secret`."""
    return _default_provider().get_secret(
        env_var=env_var,
        secret_name=secret_name,
        default=default,
    )
