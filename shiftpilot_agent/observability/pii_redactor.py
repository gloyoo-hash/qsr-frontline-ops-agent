"""Active PII redaction scrubber applied BEFORE logging or persisting memory.

Masks:
- Employee IDs (e.g., EMP-123456, E12345, employee_id=98765)
- Phone numbers (US/international formats)
- Email addresses
- Payment / credit / debit card numbers (13-19 digits, spaced or hyphenated)
- Social Security Numbers (SSN)

Supports an optional Google Cloud DLP (`google-cloud-dlp`) de-identification hook
when `ENABLE_CLOUD_DLP=true` and `GCP_PROJECT_ID` is configured.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Compiled regex patterns ordered from most specific to most general
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\w)"
)
_EMPLOYEE_ID_RE = re.compile(
    r"\b(?:EMP[-_]?\d{4,8}|SP[-_]EMP[-_]?\d{3,8}|E\d{5,8})\b",
    re.IGNORECASE,
)

_SENSITIVE_KEYS = frozenset(
    {
        "employee_id",
        "emp_id",
        "phone",
        "phone_number",
        "guest_phone",
        "email",
        "guest_email",
        "card_number",
        "credit_card",
        "payment_card",
        "ssn",
        "api_key",
        "authorization",
    }
)


class PIIRedactor:
    """Scrubs PII from strings and nested dicts/lists with optional Cloud DLP hook."""

    def __init__(self, enable_cloud_dlp: bool | None = None, project_id: str | None = None) -> None:
        self.enable_cloud_dlp = (
            enable_cloud_dlp
            if enable_cloud_dlp is not None
            else os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true"
        )
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "")

    def _redact_with_cloud_dlp(self, text: str) -> str:
        """Optional Google Cloud DLP de-identification hook."""
        if not (self.enable_cloud_dlp and self.project_id):
            return text
        try:
            from google.cloud import dlp_v2  # type: ignore[import-untyped]

            dlp = dlp_v2.DlpServiceClient()
            parent = f"projects/{self.project_id}"
            inspect_config = {
                "info_types": [
                    {"name": "EMAIL_ADDRESS"},
                    {"name": "PHONE_NUMBER"},
                    {"name": "CREDIT_CARD_NUMBER"},
                    {"name": "US_SOCIAL_SECURITY_NUMBER"},
                ]
            }
            deidentify_config = {
                "info_type_transformations": {
                    "transformations": [
                        {
                            "primitive_transformation": {
                                "replace_with_info_type_config": {}
                            }
                        }
                    ]
                }
            }
            response = dlp.deidentify_content(
                request={
                    "parent": parent,
                    "deidentify_config": deidentify_config,
                    "inspect_config": inspect_config,
                    "item": {"value": text},
                }
            )
            return response.item.value
        except Exception:  # noqa: BLE001
            # Local regex scrubber always runs as guaranteed baseline defense
            return text

    def redact_text(self, text: str) -> str:
        """Mask PII patterns in a string."""
        if not text:
            return text
        scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        scrubbed = _SSN_RE.sub("[REDACTED_SSN]", scrubbed)
        scrubbed = _CARD_RE.sub("[REDACTED_CARD]", scrubbed)
        scrubbed = _PHONE_RE.sub("[REDACTED_PHONE]", scrubbed)
        scrubbed = _EMPLOYEE_ID_RE.sub("[REDACTED_EMPLOYEE_ID]", scrubbed)
        if self.enable_cloud_dlp:
            scrubbed = self._redact_with_cloud_dlp(scrubbed)
        return scrubbed

    def redact(self, payload: Any) -> Any:
        """Recursively redact PII across strings, mappings, lists, and tuples."""
        if isinstance(payload, str):
            return self.redact_text(payload)
        if isinstance(payload, dict):
            redacted_dict: dict[Any, Any] = {}
            for key, value in payload.items():
                key_lower = str(key).lower()
                if key_lower in _SENSITIVE_KEYS and value is not None:
                    redacted_dict[key] = f"[REDACTED_{key_lower.upper()}]"
                else:
                    redacted_dict[key] = self.redact(value)
            return redacted_dict
        if isinstance(payload, list):
            return [self.redact(item) for item in payload]
        if isinstance(payload, tuple):
            return tuple(self.redact(item) for item in payload)
        return payload


_DEFAULT_REDACTOR = PIIRedactor()


def redact_pii(payload: Any) -> Any:
    """Scrub PII from any string or nested structure using the default redactor."""
    return _DEFAULT_REDACTOR.redact(payload)
