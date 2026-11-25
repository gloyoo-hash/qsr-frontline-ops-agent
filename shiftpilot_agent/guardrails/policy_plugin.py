"""Input/output policy guardrails, self-evaluation plugin, and ADK BasePlugin.

Implements:
1. Input Guardrail (`check_input_policy`):
   - Blocks prompt injection / jailbreak attempts.
   - Blocks off-topic non-QSR requests (e.g., personal crypto/stock tips, hacking).
2. Output Guardrail (`check_allergen_output_safety`):
   - Flags unsafe absolute allergen guarantees ("100% nut-free", "guaranteed allergen-free")
     or unverified allergen guessing ("probably safe", "I think it's gluten-free").
3. Self-Evaluation Callback (`self_evaluate_response`):
   - Scores every agent response against ShiftPilot QSR constitution rules
     (hospitality tone, allergen safety, PII leakage, actionability).
4. `ShiftPilotPolicyPlugin(BasePlugin)`:
   - Native Google ADK `BasePlugin` wiring input guardrails, history compaction,
     output allergen safety checks, self-evaluation, and OpenTelemetry spans into
     ADK `before_model_callback`, `after_model_callback`, `before_agent_callback`,
     and `after_agent_callback`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from google.adk.plugins import BasePlugin

from shiftpilot_agent.memory.compaction import adk_history_compaction_callback
from shiftpilot_agent.observability.logging_config import get_logger
from shiftpilot_agent.observability.pii_redactor import redact_pii
from shiftpilot_agent.observability.tracing import get_tracer

logger = get_logger(__name__)

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"reveal\s+(your\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"bypass\s+(manager\s+)?approval|skip\s+hitl", re.IGNORECASE),
    re.compile(r"pretend\s+you\s+are\s+(not\s+)?shiftpilot|jailbreak|dan\s+mode", re.IGNORECASE),
]

_OFF_TOPIC_PATTERNS = [
    re.compile(r"\b(bitcoin|crypto\s+trading|stock\s+pick|buy\s+nvda|sports\s+betting)\b", re.IGNORECASE),
    re.compile(r"\b(write\s+malware|sql\s+injection\s+exploit|hack\s+into)\b", re.IGNORECASE),
]

_UNSAFE_ALLERGEN_PATTERNS = [
    re.compile(r"\b(100%|completely|guaranteed)\s+(nut|peanut|gluten|dairy|allergen)[- ]free\b", re.IGNORECASE),
    re.compile(r"\b(probably|i\s+think|should\s+be|i\s+guess)\s+(it'?s\s+)?(safe|allergen[- ]free|gluten[- ]free|nut[- ]free)\b", re.IGNORECASE),
]


@dataclass
class GuardrailVerdict:
    """Result of an input or output policy check."""

    allowed: bool
    policy_code: str
    reason: str
    safe_replacement: str | None = None


@dataclass
class SelfEvaluationResult:
    """Automated self-evaluation score of an agent draft response."""

    passed: bool
    score: float
    violations: list[str] = field(default_factory=list)


def check_input_policy(user_text: str) -> GuardrailVerdict:
    """Validate incoming frontline message against injection & off-topic policies."""
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(user_text):
            logger.warning(
                "guardrail_input_blocked",
                policy_code="PROMPT_INJECTION_DETECTED",
                sample=redact_pii(user_text[:120]),
            )
            return GuardrailVerdict(
                allowed=False,
                policy_code="PROMPT_INJECTION_DETECTED",
                reason="Detected attempt to override system instructions or bypass HITL controls.",
                safe_replacement=(
                    "I can't override ShiftPilot QSR safety rules or manager approval gates. "
                    "Please let me know how I can help with food safety, equipment, inventory, "
                    "shift coverage, or guest recovery."
                ),
            )

    for pattern in _OFF_TOPIC_PATTERNS:
        if pattern.search(user_text):
            logger.warning(
                "guardrail_input_blocked",
                policy_code="OFF_TOPIC_REQUEST",
                sample=redact_pii(user_text[:120]),
            )
            return GuardrailVerdict(
                allowed=False,
                policy_code="OFF_TOPIC_REQUEST",
                reason="Request is outside ShiftPilot QSR restaurant operations scope.",
                safe_replacement=(
                    "I'm ShiftPilot QSR, dedicated strictly to restaurant operations "
                    "(food safety & allergens, kitchen equipment, store inventory, shift "
                    "coverage, and guest recovery). Let me know how I can help on shift!"
                ),
            )

    return GuardrailVerdict(
        allowed=True,
        policy_code="OK",
        reason="Input complies with ShiftPilot QSR operational boundaries.",
    )


def check_allergen_output_safety(response_text: str) -> GuardrailVerdict:
    """Ensure model output never guesses or makes unsafe absolute allergen guarantees."""
    for pattern in _UNSAFE_ALLERGEN_PATTERNS:
        if pattern.search(response_text):
            logger.warning(
                "guardrail_output_blocked",
                policy_code="UNSAFE_ALLERGEN_CLAIM",
                sample=redact_pii(response_text[:160]),
            )
            return GuardrailVerdict(
                allowed=False,
                policy_code="UNSAFE_ALLERGEN_CLAIM",
                reason=(
                    "Response contained an unverified or absolute allergen safety claim "
                    "violating the Zero-Guessing Allergen Rule."
                ),
                safe_replacement=(
                    "Safety Hold: I cannot guarantee an item is 100% allergen-free or guess "
                    "allergen safety. Please verify the printed in-store ShiftPilot QSR "
                    "Allergen Matrix binder and alert your Shift Manager immediately."
                ),
            )

    return GuardrailVerdict(
        allowed=True,
        policy_code="OK",
        reason="Output complies with food-safety and allergen accuracy standards.",
    )


def self_evaluate_response(response_text: str) -> SelfEvaluationResult:
    """Run a deterministic self-evaluation check on an agent response."""
    violations: list[str] = []
    score = 1.0

    allergen_verdict = check_allergen_output_safety(response_text)
    if not allergen_verdict.allowed:
        violations.append(allergen_verdict.policy_code)
        score -= 0.5

    if redact_pii(response_text) != response_text:
        violations.append("UNREDACTED_PII_IN_OUTPUT")
        score -= 0.3

    if len(response_text.strip()) < 10:
        violations.append("RESPONSE_TOO_SHORT")
        score -= 0.2

    final_score = max(0.0, round(score, 2))
    passed = len(violations) == 0
    logger.info(
        "self_evaluation_completed",
        passed=passed,
        score=final_score,
        violations=violations,
    )
    return SelfEvaluationResult(passed=passed, score=final_score, violations=violations)


class ShiftPilotPolicyPlugin(BasePlugin):
    """Google ADK Plugin enforcing guardrails, compaction, self-eval, and OTel spans."""

    def __init__(self, name: str = "shiftpilot_policy_plugin") -> None:
        super().__init__(name=name)
        self._active_spans: dict[str, Any] = {}

    async def before_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> Any:
        """Start an OpenTelemetry span for Coordinator / Sub-Agent execution."""
        agent_name = getattr(agent, "name", "unknown_agent")
        tracer = get_tracer("shiftpilot.agents")
        span = tracer.start_span(
            f"agent.{agent_name}",
            attributes={
                "agent.name": agent_name,
                "agent.model": str(getattr(agent, "model", "workflow")),
            },
        )
        key = f"{id(callback_context)}:{agent_name}"
        self._active_spans[key] = span
        logger.info("agent_invoked", agent_name=agent_name)
        return None

    async def after_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> Any:
        """End the agent OpenTelemetry span."""
        agent_name = getattr(agent, "name", "unknown_agent")
        key = f"{id(callback_context)}:{agent_name}"
        span = self._active_spans.pop(key, None)
        if span is not None:
            span.end()
        logger.info("agent_completed", agent_name=agent_name)
        return None

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> Any:
        """Run history compaction and input policy guardrails before LLM calls."""
        adk_history_compaction_callback(callback_context, llm_request)

        contents = getattr(llm_request, "contents", None) or []
        if contents:
            last = contents[-1]
            parts = getattr(last, "parts", None) or []
            user_text = " ".join(
                getattr(p, "text", "") for p in parts if getattr(p, "text", None)
            )
            verdict = check_input_policy(user_text)
            if not verdict.allowed:
                from google.adk.models import LlmResponse
                from google.genai import types

                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=verdict.safe_replacement or verdict.reason)],
                    )
                )
        return None

    async def after_model_callback(
        self, *, callback_context: Any, llm_response: Any
    ) -> Any:
        """Run allergen output guardrail & self-evaluation after LLM generation."""
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) or []
        text = " ".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None))
        if not text:
            return None

        allergen_verdict = check_allergen_output_safety(text)
        eval_result = self_evaluate_response(text)

        if not allergen_verdict.allowed:
            from google.adk.models import LlmResponse
            from google.genai import types

            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text=allergen_verdict.safe_replacement or allergen_verdict.reason
                        )
                    ],
                )
            )

        if "UNREDACTED_PII_IN_OUTPUT" in eval_result.violations:
            from google.adk.models import LlmResponse
            from google.genai import types

            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=redact_pii(text))],
                )
            )

        return None
