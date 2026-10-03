"""
AI analysis step (Phase 2): OpenAI via Microsoft Agent Framework.

The model receives the assembled clinical context (no internal IDs) and
returns a JSON recommendation. Its output is UNTRUSTED: it is parsed and
validated by backend/proposals.py before anything else happens, and it
never reaches the database or the governance decision directly.

The model's output is a workflow recommendation for a synthetic
demonstration, not a clinical diagnosis.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from backend.proposals import SUPPORTED_DEPARTMENTS


class AnalysisUnavailableError(RuntimeError):
    """The analysis model could not be reached or did not respond."""


class ClinicalAnalyzer(Protocol):
    async def analyze(
        self,
        context: dict[str, Any],
        *,
        department: str | None,
        question: str | None,
    ) -> Any:
        """Return the model's raw output (text or mapping)."""


ANALYSIS_INSTRUCTIONS = f"""
You are a clinical workflow assistant in a SYNTHETIC-DATA demonstration.
You review a patient's record and recommend whether a specialist referral
is appropriate. You do not diagnose and you do not perform actions: the
application validates your recommendation and a governance layer decides
whether any action may run.

Respond with ONE JSON object only, with exactly these keys:

  "recommendation": short plain-language summary of your assessment
  "action": "CREATE_REFERRAL" or "NO_ACTION"
  "department": one of {list(SUPPORTED_DEPARTMENTS)} (or null for NO_ACTION)
  "reason": the clinical reason for the referral (or null for NO_ACTION)
  "evidence": list of items copied EXACTLY from the patient record
              (condition, medication, allergy, observation type or
              encounter type) that support the recommendation

Rules:
- Use only information present in the record. Never invent findings.
- Never include identifiers of any kind.
- Never claim that a referral has been created.
"""


def build_analysis_prompt(
    context: dict[str, Any],
    *,
    department: str | None,
    question: str | None,
) -> str:
    ask = question or (
        f"Is a {department} referral appropriate for this patient?"
        if department
        else "Is a specialist referral appropriate for this patient?"
    )
    return (
        f"Clinician request: {ask}\n\n"
        f"Patient record (synthetic):\n{json.dumps(context, indent=2, default=str)}\n\n"
        "Return the JSON object now."
    )


class OpenAIClinicalAnalyzer:
    """Default analyzer: a tool-less Agent Framework agent on OpenAI."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model or os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini")
        self._agent: Any = None

    def _get_agent(self) -> Any:
        if self._agent is None:
            from agent_framework import Agent
            from agent_framework.openai import OpenAIChatClient
            from backend.agent import validate_openai_configuration

            validate_openai_configuration()

            self._agent = Agent(
                client=OpenAIChatClient(model=self._model),
                name="XVerbaClinicalAnalyst",
                instructions=ANALYSIS_INSTRUCTIONS,
            )
        return self._agent

    async def analyze(
        self,
        context: dict[str, Any],
        *,
        department: str | None,
        question: str | None,
    ) -> Any:
        prompt = build_analysis_prompt(context, department=department, question=question)
        try:
            response = await self._get_agent().run(
                prompt,
                options={"response_format": "json", "temperature": 0},
            )
        except Exception as exc:  # network, authentication, timeout, ...
            raise AnalysisUnavailableError("Clinical analysis model unavailable.") from exc

        return getattr(response, "text", None) or ""
