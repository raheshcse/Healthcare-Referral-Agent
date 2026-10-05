"""
Chat service: multi-turn conversations through Microsoft Agent Framework.

Per conversation (backend.conversation.Conversation):
  - an AgentSession holds the message history (user, assistant, tool calls,
    tool results), so the configured OpenAI model sees the whole conversation every turn;
  - a ReferralContext holds the validated facts actions depend on
    (patient, department, reason, stage), injected into the instructions.

Turn routing:
  - small talk            -> tool-less conversation agent (same history)
  - an answer to the pending referral question, or a new referral request
    that is still missing details
                          -> deterministic next question (no model call; the
                             exchange is added to the history on the next run)
  - everything else       -> tool-enabled agent with application validation

Replies are derived from the authoritative workflow / action outcomes
whenever a workflow ran, never from the model's claims. If a
side-effecting tool was attempted but produced no outcome (validation
denied it before it ran, or it failed), the reply says so deterministically:
the model's text is never allowed to claim a result that did not happen.
"""


from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agent_tools import (
    capture_action_outcomes,
    capture_clinical_reviews,
    capture_referral_outcomes,
)
from backend.clinical_workflow import ClinicalWorkflowResult
from backend.conversation import (
    ConversationStore,
    PatientReference,
    PendingLookup,
    apply_staff_message,
    conversation_turn,
    has_action_intent,
    next_question,
)
from backend.domain import ActionOutcome, ReferralOutcome, WorkflowStatus
from backend.patient_resolution import ResolutionStatus, format_date_of_birth, parse_date_of_birth


logger = logging.getLogger(__name__)

# Tools whose purpose is a side effect (or a workflow).
SIDE_EFFECT_TOOLS = frozenset(
    {"create_referral", "update_referral", "cancel_referral", "request_clinical_review",
     "review_patient_for_referral"}
)

DECLINED_REPLY = "Understood. I won't submit that referral. Nothing was created."

CLINICIAN_GUIDANCE = {
    "patient_named_by_staff": "Please confirm the patient name exactly as it appears in the record.",
    "patient_resolved": "Please identify the patient using a full name and date of birth if needed.",
    "search_term_from_staff": "Please provide a clear patient name or name fragment to search.",
    "search_term_valid": "Please give at least two characters of the patient name.",
    "referral_confirmed": "Please confirm the referral details before submitting.",
    "matches_confirmed_details": "Please confirm the patient and department match the referral you want to create.",
}

CAPABILITY_REPLY = (
    "I can look up patients and their clinical records, create referrals, update or "
    "cancel existing referrals, and request a clinical review. Nothing has been "
    "submitted. Which patient would you like to work with?"
)


class LLMUnavailableError(RuntimeError):
    """The language model could not be reached or failed to respond."""


@dataclass
class ChatResult:
    reply: str
    agent_reply: str | None
    workflow_results: list[ReferralOutcome] = field(default_factory=list)
    clinical_reviews: list[ClinicalWorkflowResult] = field(default_factory=list)
    action_results: list[ActionOutcome] = field(default_factory=list)
    conversation_id: str | None = None
    context: dict[str, Any] | None = None


def _describe(outcome: ReferralOutcome) -> str:
    text = outcome.message

    if outcome.status is WorkflowStatus.MULTIPLE_PATIENT_MATCHES and outcome.candidates:
        options = "; ".join(
            f"{c.name} (DOB {c.date_of_birth or 'unknown'})"
            for c in outcome.candidates
        )
        text = f"{text} Candidates: {options}."

    return text


_SMALL_TALK_PHRASE = re.compile(
    r"^(?:(?:hi+|hey+|hello+|hiya|yo|howdy|greetings|kia ora|good (?:morning|afternoon|evening|day))"
    r"(?: there| team| everyone| all| doc| doctor)?"
    r"|thanks?(?: you)?(?: (?:very|so) much)?|thank you(?: (?:very|so) much)?|cheers|ta"
    r"|ok(?:ay)?|cool|great|bye|goodbye|see you|how are you(?: doing| today)?"
    r"|how(?:'s| is) it going|what'?s up|sup)$",
    re.IGNORECASE,
)


def is_small_talk(message: str) -> bool:
    """
    Deterministic check for pure greetings / thanks / small talk.

    Every clause of the message must be a small-talk phrase, so
    "hi, refer Aisha Wiegand to cardiology for chest pain" is NOT small talk.
    """

    clauses = [c.strip() for c in re.split(r"[,.!?;:\n]+", message or "") if c.strip()]
    return bool(clauses) and all(_SMALL_TALK_PHRASE.match(" ".join(c.split())) for c in clauses)


def _user_message(text: str) -> Any:
    from agent_framework import Message

    return Message(role="user", contents=[text])


def _assistant_message(text: str) -> Any:
    from agent_framework import Message

    return Message(role="assistant", contents=[text])


class ChatService:
    def __init__(
        self,
        agent_factory: Callable[[], Any] | None = None,
        conversation_agent_factory: Callable[[], Any] | None = None,
        store: ConversationStore | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._agent: Any = None
        self._conversation_agent_factory = conversation_agent_factory
        self._conversation_agent: Any = None
        self._store = store or ConversationStore()
        self._locks: dict[str, asyncio.Lock] = {}
        # Deterministic exchanges not yet seen by the model, per conversation.
        self._pending: dict[str, list[Any]] = {}

    @property
    def store(self) -> ConversationStore:
        return self._store

    def _get_agent(self) -> Any:
        if self._agent is None:
            if self._agent_factory is None:
                from backend.agent import create_agent

                self._agent_factory = create_agent
            self._agent = self._agent_factory()
        return self._agent

    def _get_conversation_agent(self) -> Any:
        if self._conversation_agent is None:
            if self._conversation_agent_factory is None:
                from backend.agent import create_conversation_agent

                self._conversation_agent_factory = create_conversation_agent
            self._conversation_agent = self._conversation_agent_factory()
        return self._conversation_agent

    def _new_session(self, session_id: str) -> Any:
        """One MAF AgentSession per conversation (message history)."""

        try:
            from agent_framework import AgentSession

            return AgentSession(session_id=session_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _inputs(self, conversation_id: str, message: str) -> list[Any]:
        return [*self._pending.pop(conversation_id, []), _user_message(message)]

    # ------------------------------------------------------------------

    async def chat(self, message: str, conversation_id: str | None = None) -> ChatResult:
        conversation = self._store.get_or_create(conversation_id, self._new_session)
        lock = self._locks.setdefault(conversation.conversation_id, asyncio.Lock())

        async with lock:
            with conversation_turn(conversation):
                result = await self._turn(conversation, message)

        result.conversation_id = conversation.conversation_id
        result.context = conversation.context.for_client()
        return result

    async def _turn(self, conversation, message: str) -> ChatResult:
        cid = conversation.conversation_id

        # 1. General conversation: tool-less agent, same history.
        if is_small_talk(message):
            conversation.staff_messages.append(message)
            try:
                response = await self._get_conversation_agent().run(
                    self._inputs(cid, message), session=conversation.session
                )
            except Exception as exc:
                from backend.agent import OpenAIConfigurationError

                if isinstance(exc, OpenAIConfigurationError):
                    raise
                logger.exception("Conversation agent run failed.")
                raise LLMUnavailableError("The language model is unavailable.") from exc
            text = getattr(response, "text", None) or ""
            return ChatResult(reply=text, agent_reply=text)

        # 2. Capture clearly supplied referral facts (deterministic).
        update = apply_staff_message(conversation, message)
        context = conversation.context
        question = next_question(context)

        if update.declined:
            self._pending.setdefault(cid, []).extend([_user_message(message), _assistant_message(DECLINED_REPLY)])
            return ChatResult(reply=DECLINED_REPLY, agent_reply=None)

        # The staff member identified the patient for a pending lookup but it
        # still did not resolve: say so clearly (no model call).
        lookup = update.lookup_resolution
        if lookup is not None and lookup.status is not ResolutionStatus.RESOLVED:
            pending = context.pending_lookup
            reply = patient_lookup_reply(
                pending.query if pending else PatientReference(),
                lookup.status.value,
                lookup.message,
                [{"name": c.name, "date_of_birth": c.date_of_birth} for c in lookup.candidates],
            )
            self._pending.setdefault(cid, []).extend([_user_message(message), _assistant_message(reply)])
            return ChatResult(reply=reply, agent_reply=None)

        if update.unknown_department and context.department is None:
            reply = unsupported_department_reply(update.unknown_department)
            self._pending.setdefault(cid, []).extend([_user_message(message), _assistant_message(reply)])
            return ChatResult(reply=reply, agent_reply=None)

        # 3. Still collecting required details: ask exactly the next missing
        #    one. No model call, so it can never re-ask for known facts.
        if question and not update.unresolved_name and not _is_question(message) and (
            update.changed or update.consumed or _starts_referral(message)
        ):
            self._pending.setdefault(cid, []).extend([_user_message(message), _assistant_message(question)])
            return ChatResult(reply=question, agent_reply=None)

        # 4. Tool-enabled agent with application validation.
        attempts: list[dict[str, Any]] = []
        with (
            capture_referral_outcomes() as outcomes,
            capture_clinical_reviews() as reviews,
            capture_action_outcomes() as actions,
        ):
            try:
                response = await self._get_agent().run(
                    self._inputs(cid, message), session=conversation.session
                )
                agent_reply: str | None = getattr(response, "text", None) or ""
            except Exception as exc:
                from backend.agent import OpenAIConfigurationError

                if isinstance(exc, OpenAIConfigurationError):
                    raise
                logger.exception("Agent run failed.")
                if not outcomes and not reviews and not actions:
                    raise LLMUnavailableError(
                        "The language model is unavailable."
                    ) from exc
                agent_reply = None

        # Authoritative reply: built from actual workflow results whenever a
        # workflow ran, never from the model's own claims.
        parts = (
            [r.message for r in reviews]
            + [_describe(o) for o in outcomes]
            + [a.message for a in actions]
        )
        # A resolved clarification has been handed to the model once; it is
        # not repeated on later turns.
        if lookup is not None and context.pending_lookup is not None and context.pending_lookup.resolved:
            context.pending_lookup = None

        read_problem = None if parts else _read_tool_problem(attempts)

        if parts:
            reply = " ".join(parts)
        elif read_problem is not None:
            reply = read_problem.reply
            if read_problem.pending is not None and context.intent is None:
                context.pending_lookup = PendingLookup(request=message, **read_problem.pending)
        elif any(a["tool"] in SIDE_EFFECT_TOOLS for a in attempts):
            # Never pass the model's text through here: it may claim a result
            # that did not happen.
            if has_action_intent(message) or context.intent is not None or any(a["executed"] for a in attempts):
                reply = _unfinished_action_reply(attempts)
            else:
                # The model reached for an action the staff member never asked for.
                reply = CAPABILITY_REPLY
        else:
            reply = agent_reply or next_question(context) or ""

        return ChatResult(
            reply=reply,
            agent_reply=agent_reply,
            workflow_results=list(outcomes),
            clinical_reviews=list(reviews),
            action_results=list(actions),
        )


READ_TOOLS = frozenset(
    {"search_patient", "get_patient_information", "get_patient_conditions", "get_patient_medications",
     "get_patient_allergies", "get_patient_observations", "get_patient_encounters"}
)

_UNRESOLVED = ("NOT_FOUND", "MULTIPLE_MATCHES", "INVALID_INPUT")


@dataclass
class _ReadProblem:
    reply: str
    pending: dict[str, Any] | None = None  # PendingLookup fields (query, status, candidates)


def _read_tool_problem(attempts: list[dict]) -> _ReadProblem | None:
    """
    A read-only lookup the staff member asked for did not return data:
    the named patient was not found / not unique, or the tool failed.
    Returns a clear, deterministic reply (never the model's guess).
    """

    if any(a["tool"] in SIDE_EFFECT_TOOLS for a in attempts):
        return None
    reads = [a for a in attempts if a["tool"] in READ_TOOLS]
    if not reads:
        return None
    if any(a["executed"] and a["error"] for a in reads):
        return _ReadProblem(
            "I couldn't retrieve that patient information because of a system error. "
            "Nothing was changed. Please try again, or contact support if it keeps happening."
        )
    if any(a["executed"] and not a["error"] for a in reads):
        return None  # data was returned; the model reports it
    for attempt in reads:
        patient = attempt.get("patient") or {}
        failed = attempt.get("failed") or []
        if patient.get("status") in _UNRESOLVED and "patient_named_by_staff" not in failed:
            query = PatientReference(**(patient.get("query") or {}))
            candidates = patient.get("candidates") or []
            return _ReadProblem(
                patient_lookup_reply(query, patient["status"], patient.get("message", ""), candidates),
                {"query": query, "status": patient["status"], "candidates": candidates},
            )
    return None


def _born(value: str | None) -> str:
    return f"born {format_date_of_birth(value)}" if value else "date of birth not recorded"


def _typed_date(value: str | None) -> str:
    """DD/MM/YYYY example for the clarification hint."""

    dates = parse_date_of_birth(value)
    return dates[0].strftime("%d/%m/%Y") if dates else "DD/MM/YYYY"


def patient_lookup_reply(query: PatientReference, status: str, message: str,
                         candidates: list[dict[str, Any]]) -> str:
    """Clear clinician-facing text for a patient that could not be identified."""

    name = query.patient_name or "that name"
    if status == "NOT_FOUND":
        if query.date_of_birth:
            return (
                f"I couldn't find a patient named {name} with date of birth "
                f"{format_date_of_birth(query.date_of_birth)}. Please check the name and date of birth. "
                "No patient information was retrieved."
            )
        return (
            f"I couldn't find a patient named {name}. Please check the spelling, or give the "
            "patient's full name and date of birth (DD/MM/YYYY). No patient information was retrieved."
        )
    if status == "MULTIPLE_MATCHES":
        lines = [f"- {c.get('name')}, {_born(c.get('date_of_birth'))}" for c in candidates[:10]]
        example = candidates[0] if candidates else {}
        hint = (
            f"for example \u201c{example.get('name')}, {_typed_date(example.get('date_of_birth'))}\u201d"
            if example.get("date_of_birth") else "for example the full name and date of birth (DD/MM/YYYY)"
        )
        return (
            f"More than one patient matches {name}:\n" + "\n".join(lines)
            + f"\nWhich patient do you mean? Reply with the full name and date of birth, {hint}."
        )
    return message or "The patient could not be identified. Please give the full name and date of birth."


def unsupported_department_reply(department: str) -> str:
    from backend.proposals import SUPPORTED_DEPARTMENTS

    return (
        f"\u201c{department}\u201d is not a department I can refer to. Supported departments are: "
        f"{', '.join(SUPPORTED_DEPARTMENTS)}. Which department should the referral go to?"
    )


def _unfinished_action_reply(attempts: list[dict]) -> str:
    """An action was attempted but produced no outcome."""

    relevant = [a for a in attempts if a["tool"] in SIDE_EFFECT_TOOLS]
    if any(a["executed"] and a["error"] for a in relevant):
        return ("No referral or change was made because of a system error. "
                "Please try again, or contact support if it keeps happening.")
    guidance: list[str] = []
    for attempt in relevant:
        failed = list(attempt.get("failed") or [])
        if "patient_named_by_staff" in failed and "patient_resolved" in failed:
            failed.remove("patient_resolved")
        for fact in failed:
            text = CLINICIAN_GUIDANCE.get(fact)
            if text and text not in guidance:
                guidance.append(text)
    detail = " ".join(guidance) or "Some required details were missing or could not be verified."
    return f"No referral or change was made. {detail}"


def _is_question(message: str) -> bool:
    return bool(re.search(r"\?\s*$|^\s*(what|which|who|when|where|why|how|is|are|does|do|can|could)\b", message, re.I))


def _starts_referral(message: str) -> bool:
    return bool(re.search(r"\b(refer|referral)\b", message, re.I))
