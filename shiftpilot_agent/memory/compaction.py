"""History compaction for long QSR shift conversations (sliding window + summarization).

Prevents context bloat and latency degradation during 8-12 hour restaurant shifts:
- Estimates token usage across conversation turns.
- When turn count exceeds `window_turns` OR estimated tokens exceed `max_tokens`,
  older turns are compressed into a concise operational shift summary while the
  most recent `window_turns` turns are preserved verbatim.
- Exposes `adk_history_compaction_callback`, an ADK `before_model_callback` hook
  that automatically compacts `LlmRequest.contents` in place.
"""

from __future__ import annotations

from typing import Any

from shiftpilot_agent.config import CONFIG
from shiftpilot_agent.observability.logging_config import get_logger
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.schemas.models import CompactedHistory

logger = get_logger(__name__)


def estimate_tokens(text: str) -> int:
    """Fast deterministic token estimator (~4 chars per token)."""
    return max(1, (len(text) + 3) // 4) if text else 0


class HistoryCompactor:
    """Sliding-window + extractive summarization compactor for shift conversations."""

    def __init__(
        self,
        window_turns: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.window_turns = window_turns or CONFIG.compaction.window_turns
        self.max_tokens = max_tokens or CONFIG.compaction.max_tokens

    def _estimate_history_tokens(self, turns: list[dict[str, str]]) -> int:
        return sum(estimate_tokens(t.get("content", "")) + 6 for t in turns)

    def _summarize_older_turns(self, older_turns: list[dict[str, str]]) -> str:
        """Produce a structured, PII-redacted shift summary from archived turns."""
        highlights: list[str] = []
        keywords = (
            "allergen",
            "sop",
            "fryer",
            "cooler",
            "grill",
            "error",
            "hi-limit",
            "temp",
            "inventory",
            "86",
            "vendor",
            "voucher",
            "overtime",
            "approval",
            "sp-",
        )
        for turn in older_turns:
            content = redact_pii(turn.get("content", "")).strip()
            role = turn.get("role", "user")
            if not content:
                continue
            lower = content.lower()
            if any(k in lower for k in keywords) or len(highlights) < 5:
                snippet = content[:140] + ("..." if len(content) > 140 else "")
                highlights.append(f"- [{role}] {snippet}")

        if not highlights:
            return f"Compacted {len(older_turns)} earlier shift turns with no open incidents."

        # Cap summary length to keep compacted context tight
        capped = highlights[:10]
        return (
            f"SHIFT HISTORY SUMMARY ({len(older_turns)} earlier turns compacted):\n"
            + "\n".join(capped)
        )

    def compact(self, turns: list[dict[str, str]]) -> CompactedHistory:
        """Compact conversation history if it exceeds turn or token thresholds."""
        tokens_before = self._estimate_history_tokens(turns)
        needs_compaction = (
            len(turns) > self.window_turns or tokens_before > self.max_tokens
        )

        if not needs_compaction:
            return CompactedHistory(
                summary="",
                recent_turns=turns,
                original_turn_count=len(turns),
                compacted_turn_count=len(turns),
                estimated_tokens_before=tokens_before,
                estimated_tokens_after=tokens_before,
                was_compacted=False,
            )

        keep_count = min(self.window_turns, max(2, len(turns) // 2))
        older = turns[:-keep_count]
        recent = turns[-keep_count:]
        summary = self._summarize_older_turns(older)

        # Ensure token budget is also respected if individual recent messages are huge
        compacted_recent = recent
        summary_tokens = estimate_tokens(summary)
        recent_tokens = self._estimate_history_tokens(compacted_recent)
        while len(compacted_recent) > 2 and (summary_tokens + recent_tokens) > self.max_tokens:
            dropped = compacted_recent.pop(0)
            older.append(dropped)
            summary = self._summarize_older_turns(older)
            summary_tokens = estimate_tokens(summary)
            recent_tokens = self._estimate_history_tokens(compacted_recent)

        tokens_after = summary_tokens + recent_tokens
        logger.info(
            "history_compacted",
            original_turns=len(turns),
            kept_turns=len(compacted_recent),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )
        return CompactedHistory(
            summary=summary,
            recent_turns=compacted_recent,
            original_turn_count=len(turns),
            compacted_turn_count=len(compacted_recent) + 1,
            estimated_tokens_before=tokens_before,
            estimated_tokens_after=tokens_after,
            was_compacted=True,
        )


_DEFAULT_COMPACTOR = HistoryCompactor()


def adk_history_compaction_callback(callback_context: Any, llm_request: Any) -> None:
    """ADK `before_model_callback` hook that compacts long `LlmRequest.contents`.

    Extracts text turns from `llm_request.contents`, runs `HistoryCompactor`, and
    replaces older turns with a single compact summary Content object when needed.
    """
    contents = getattr(llm_request, "contents", None)
    if not contents or len(contents) <= _DEFAULT_COMPACTOR.window_turns:
        return None

    turns: list[dict[str, str]] = []
    for content in contents:
        role = getattr(content, "role", "user") or "user"
        parts = getattr(content, "parts", None) or []
        text_chunks = [getattr(p, "text", "") for p in parts if getattr(p, "text", None)]
        turns.append({"role": role, "content": " ".join(text_chunks)})

    result = _DEFAULT_COMPACTOR.compact(turns)
    if not result.was_compacted:
        return None

    try:
        from google.genai import types

        summary_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=result.summary)],
        )
        kept_count = len(result.recent_turns)
        llm_request.contents = [summary_content, *contents[-kept_count:]]
    except Exception:  # noqa: BLE001
        pass

    return None
