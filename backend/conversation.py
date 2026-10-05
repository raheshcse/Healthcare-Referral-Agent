"""
Conversation context and state (Phase 3).

Two kinds of memory, deliberately separate:

1. Message history - owned by Microsoft Agent Framework. Each conversation
   has one AgentSession; MAF's InMemoryHistoryProvider stores the user
   messages, assistant messages, tool calls and tool results in it, so the
   model sees the whole conversation on every turn.

2. Structured workflow state - owned by the application (ReferralContext).
   The facts an action depends on (which patient, which department,
   what reason, what stage) are kept as validated fields, NOT inferred from
   free text each time. The resolved patient's internal UUID lives only
   here; the model only ever sees the name.

The state is injected into the model's instructions every turn by
ReferralContextProvider to decide whether a tool input is grounded in what
the staff member actually said.
"""

from __future__ import annotations

import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from backend.database.connection import SessionLocal
from backend.domain import PatientRecord
from backend.patient_resolution import (
    DATE_PATTERN,
    PatientResolution,
    ResolutionStatus,
    format_date_of_birth,
    parse_date_of_birth,
    resolve_patient_by_name,
    split_name_and_date_of_birth,
)
from backend.proposals import SUPPORTED_DEPARTMENTS, normalize_department


# ============================================================
# Text helpers (shared with the pre-tool gates)
# ============================================================

PRONOUNS = {
    "", "she", "he", "her", "him", "they", "them", "hers", "his",
    "the patient", "that patient", "this patient", "patient", "same patient",
    "the same patient", "her patient", "this person", "that person",
}

NAME_FILLER = {"mr", "mrs", "ms", "miss", "dr", "patient", "the"}

PLACEHOLDER_TEXT = {
    "unknown", "none", "n/a", "na", "n.a", "null", "nil", "tbd", "tba", "-", "?",
    "not specified", "unspecified", "not provided", "not given", "not stated",
    "no reason", "no reason given", "no reason provided", "reason", "referral",
    "not applicable", "unknown reason", "other",
}

TEXT_STOPWORDS = {
    "patient", "patients", "referral", "refer", "referred", "referring", "create",
    "please", "needs", "need", "requires", "require", "required", "with", "that",
    "this", "from", "their", "them", "they", "have", "has", "for", "because",
    "the", "and", "into", "about", "department", "specialist", "review", "cancel",
    "cancelled", "update", "change", "request", "clinical",
}


def words(text: str | None) -> list[str]:
    """Lower-case alphanumeric words with Synthea digit suffixes removed."""

    stripped = (re.sub(r"\d+", "", w) for w in re.findall(r"[a-z0-9]+", (text or "").casefold()))
    return [w for w in stripped if w]


def is_pronoun(value: str | None) -> bool:
    return " ".join((value or "").casefold().split()).strip(" .") in PRONOUNS


def is_placeholder(text: str | None) -> bool:
    cleaned = " ".join((text or "").split()).strip(" .!").casefold()
    return len(cleaned) < 3 or cleaned in PLACEHOLDER_TEXT


# ============================================================
# Structured workflow state
# ============================================================

REFERRAL_FIELDS = ("patient", "department", "reason")


@dataclass
class PatientReference:
    """
    Structured patient identity as it is known in the conversation.

    The three fields are never merged: ``patient_name`` is only a name,
    ``date_of_birth`` only a date (as typed until resolved, then the
    record's ISO date) and ``patient_id`` is set only once deterministic
    resolution identified exactly one patient. The model never sees the ID.
    """

    patient_name: str | None = None
    date_of_birth: str | None = None
    patient_id: str | None = None

    @classmethod
    def from_record(cls, record: PatientRecord) -> "PatientReference":
        return cls(patient_name=record.name, date_of_birth=record.date_of_birth, patient_id=record.patient_id)

    def describe(self) -> str:
        """'Ram Kumar (date of birth 2 October 2003)' for messages."""

        name = self.patient_name or "the patient"
        if self.date_of_birth:
            return f"{name} (date of birth {format_date_of_birth(self.date_of_birth)})"
        return name


@dataclass
class PendingLookup:
    """A read request waiting for the staff member to identify the patient."""

    request: str  # the staff member's original message
    query: PatientReference  # what was looked up
    status: str  # NOT_FOUND | MULTIPLE_MATCHES | INVALID_INPUT
    candidates: list[dict[str, Any]] = field(default_factory=list)
    resolved: bool = False  # the clarification identified the patient


@dataclass
class ReferralContext:
    """Validated facts established in this conversation."""

    intent: str | None = None  # "REFERRAL" | "REVIEW" | None
    patient: PatientRecord | None = None  # resolved deterministically
    department: str | None = None  # normalised against the catalogue
    reason: str | None = None  # staff member's own words
    stage: str = "IDLE"  # IDLE | COLLECTING | READY | CONFIRMED | SUBMITTED
    awaiting: str | None = None  # patient | department | reason | confirmation
    retrieved: list[str] = field(default_factory=list)  # data categories read this conversation
    pending_lookup: PendingLookup | None = None  # waiting for patient identification
    last_referral_id: int | None = None
    last_outcome: str | None = None

    def missing(self) -> list[str]:
        if self.intent != "REFERRAL":
            return []
        return [name for name in REFERRAL_FIELDS if getattr(self, name) in (None, "")]

    def refresh_stage(self) -> None:
        if self.intent != "REFERRAL":
            self.awaiting = None
            return
        missing = self.missing()
        if missing:
            self.stage, self.awaiting = "COLLECTING", missing[0]
        elif self.stage not in ("CONFIRMED", "SUBMITTED"):
            self.stage, self.awaiting = "READY", "confirmation"

    def start_referral(self, *, keep_patient: bool) -> None:
        # Every new referral instruction starts from a clean slate: details
        # from an earlier request (another department, another reason) must
        # never carry over. The established patient is kept only when the
        # staff member refers back to them ("refer her to neurology too").
        self.department = None
        self.reason = None
        if not keep_patient:
            self.patient = None
        self.intent = "REFERRAL"
        self.stage = "COLLECTING"
        self.refresh_stage()

    def abandon(self) -> None:
        """The staff member declined or withdrew the referral being prepared."""

        self.intent = None
        self.stage = "IDLE"
        self.awaiting = None
        self.department = None
        self.reason = None

    def mark_submitted(self, referral_id: int | None, outcome: str) -> None:
        self.stage = "SUBMITTED"
        self.awaiting = None
        self.last_referral_id = referral_id or self.last_referral_id
        self.last_outcome = outcome
        self.intent = None

    # ---- views -----------------------------------------------------

    def for_client(self) -> dict[str, Any]:
        """Clinician-safe summary (no internal identifiers)."""

        return {
            "intent": self.intent,
            "stage": self.stage,
            "patient": (
                {"name": self.patient.name, "date_of_birth": self.patient.date_of_birth}
                if self.patient
                else None
            ),
            # Waiting for the staff member to identify a patient (name / DOB only).
            "patient_query": (
                {
                    "name": self.pending_lookup.query.patient_name,
                    "date_of_birth": self.pending_lookup.query.date_of_birth,
                    "status": self.pending_lookup.status,
                }
                if self.pending_lookup is not None and not self.pending_lookup.resolved
                else None
            ),
            "department": self.department,
            "reason": self.reason,
            "missing": self.missing(),
            "awaiting": self.awaiting,
            "last_referral_id": self.last_referral_id,
        }

    def for_model(self) -> str:
        """Per-turn instructions describing what is known and what to do next."""

        known = []
        if self.patient:
            dob = f", date_of_birth = {self.patient.date_of_birth}" if self.patient.date_of_birth else ""
            known.append(
                f"patient_name = {self.patient.name}{dob} (resolved; refer to them as 'she/he/the patient' "
                "or by name)"
            )
        if self.department:
            known.append(f"department = {self.department}")
        if self.reason:
            known.append(f"reason = {self.reason!r}")
        if self.last_referral_id:
            known.append(f"most recent referral number in this conversation = {self.last_referral_id}")
        if self.retrieved:
            known.append("clinical data already retrieved: " + ", ".join(sorted(set(self.retrieved))))

        lines = ["CONVERSATION STATE (maintained by the application - trust it):"]
        lines.append("Known: " + ("; ".join(known) if known else "nothing yet"))

        pending = self.pending_lookup
        if pending is not None and pending.resolved and self.patient is not None:
            dob_arg = f", date_of_birth={self.patient.date_of_birth!r}" if self.patient.date_of_birth else ""
            lines.append(
                f"PENDING REQUEST: the staff member earlier asked: {pending.request!r}. They have now "
                f"identified the patient. Next step: complete that request now with the matching "
                f"read-only tool, called with patient_name={self.patient.name!r}{dob_arg}. "
                "Do not ask for the patient again."
            )

        if self.intent == "REFERRAL":
            missing = self.missing()
            lines.append("Current task: creating a referral.")
            if missing:
                lines.append("Still missing: " + ", ".join(missing) + ".")
                lines.append(
                    f"Next step: ask ONLY for the {missing[0]}. Do not ask for anything already known. "
                    "Do not call create_referral yet."
                )
            elif self.stage == "READY":
                lines.append(
                    "All referral details are known. Next step: briefly summarise patient, department "
                    "and reason, optionally check relevant clinical data with the read-only tools, and "
                    "ask the staff member to confirm. Do not call create_referral until they confirm."
                )
            elif self.stage == "CONFIRMED":
                lines.append(
                    "The staff member has CONFIRMED. Next step: call create_referral once with "
                    f"patient_name={self.patient.name!r}, department={self.department!r}, "
                    f"reason={self.reason!r}."
                )
        elif self.intent == "REVIEW":
            lines.append("Current task: reviewing whether a referral is appropriate.")
        elif self.stage == "SUBMITTED" and self.last_outcome:
            lines.append(f"The last action finished with outcome {self.last_outcome}.")

        return "\n".join(lines)


# ============================================================
# Conversation + store
# ============================================================

@dataclass
class Conversation:
    conversation_id: str
    session: Any  # agent_framework AgentSession (message history)
    context: ReferralContext = field(default_factory=ReferralContext)
    staff_messages: list[str] = field(default_factory=list)
    names_from_tools: set[str] = field(default_factory=set)  # full names returned by search_patient
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def staff_words(self) -> set[str]:
        return {w for message in self.staff_messages for w in words(message)}

    # ---- grounding facts --------------------------------------------

    def patient_named_by_staff(self, patient_name: str | None) -> bool:
        """True when the name (or pronoun) refers to a patient the staff member named."""

        if is_pronoun(patient_name):
            return self.context.patient is not None
        name_words = [w for w in words(patient_name) if w not in NAME_FILLER]
        if not name_words:
            return False
        if all(w in self.staff_words for w in name_words):
            return True
        normalized = " ".join((patient_name or "").split()).casefold()
        if self.context.patient and normalized == self.context.patient.name.casefold():
            return True
        return normalized in self.names_from_tools

    def text_stated_by_staff(self, text: str | None, *, exclude: str = "") -> bool:
        """A free-text value (reason) must be the staff member's, not a placeholder."""

        if is_placeholder(text):
            return False
        excluded = TEXT_STOPWORDS | set(words(exclude))
        meaningful = {w for w in words(text) if len(w) >= 3 and w not in excluded}
        return bool(meaningful & self.staff_words)

    def reference_stated_by_staff(self, referral_number: Any) -> bool:
        try:
            number = int(str(referral_number).strip().lstrip("#"))
        except (TypeError, ValueError):
            return False
        if number == self.context.last_referral_id:
            return True
        return any(re.search(rf"(?<!\d)#?{number}(?!\d)", message) for message in self.staff_messages)

    def date_stated_by_staff(self, date_of_birth: str | None) -> bool:
        """A date of birth is usable only if the staff member typed it."""

        wanted = set(parse_date_of_birth(date_of_birth))
        if not wanted:
            return False
        for message in self.staff_messages:
            for match in DATE_PATTERN.finditer(message):
                if wanted & set(parse_date_of_birth(match.group(0))):
                    return True
        return False

    def patient_reference(self, patient_name: str | None, date_of_birth: str | None = None) -> PatientReference:
        """
        Structured reference for a tool's patient arguments.

        - a pronoun ("she", "the patient") -> the established patient;
        - a name that the established patient matches (e.g. "Ram" after
          "Ram Kumar, 02/10/2003" was resolved) -> that patient, so an
          ambiguous name is not re-resolved from scratch;
        - otherwise the typed name and date of birth, for resolution.

        A date of birth is used only when the staff member stated it (the
        model cannot narrow a search with an invented date), and a date
        accidentally placed inside the name is split out.
        """

        name, embedded = split_name_and_date_of_birth(patient_name or "")
        dob = date_of_birth or embedded
        if dob and not self.date_stated_by_staff(dob):
            dob = None

        established = self.context.patient
        if is_pronoun(name):
            return PatientReference.from_record(established) if established else PatientReference()
        if established is not None:
            name_words = [w for w in words(name) if w not in NAME_FILLER]
            same_name = bool(name_words) and set(name_words) <= set(words(established.name))
            same_dob = dob is None or (
                established.date_of_birth is not None
                and set(parse_date_of_birth(dob)) & set(parse_date_of_birth(established.date_of_birth))
            )
            if same_name and same_dob:
                return PatientReference.from_record(established)
        return PatientReference(patient_name=name or None, date_of_birth=dob)

    def resolve_patient(self, patient_name: str | None, date_of_birth: str | None = None):
        """(PatientReference, PatientResolution) - deterministic, read-only."""

        reference = self.patient_reference(patient_name, date_of_birth)
        established = self.context.patient
        if reference.patient_id is not None and established is not None \
                and reference.patient_id == established.patient_id:
            return reference, PatientResolution(
                status=ResolutionStatus.RESOLVED,
                message="The patient established in this conversation.",
                patient=established,
            )
        with SessionLocal() as session:
            resolution = resolve_patient_by_name(session, reference.patient_name, reference.date_of_birth)
        if resolution.patient is not None:
            reference = PatientReference.from_record(resolution.patient)
        return reference, resolution

    def resolve_patient_reference(self, patient_name: str | None) -> str | None:
        """Backwards-compatible: the name to use for a (possibly pronoun) reference."""

        return self.patient_reference(patient_name).patient_name or patient_name


class ConversationStore:
    """In-memory conversations with idle expiry (Phase 3: single process)."""

    def __init__(self, *, ttl_seconds: int = 2 * 60 * 60, max_conversations: int = 500) -> None:
        self._ttl = ttl_seconds
        self._max = max_conversations
        self._items: dict[str, Conversation] = {}

    def _evict(self) -> None:
        now = time.time()
        for key in [k for k, c in self._items.items() if now - c.updated_at > self._ttl]:
            del self._items[key]
        while len(self._items) > self._max:
            oldest = min(self._items.values(), key=lambda c: c.updated_at)
            del self._items[oldest.conversation_id]

    def get_or_create(self, conversation_id: str | None, session_factory) -> Conversation:
        self._evict()
        if conversation_id and conversation_id in self._items:
            conversation = self._items[conversation_id]
        else:
            new_id = str(uuid.uuid4())
            conversation = Conversation(conversation_id=new_id, session=session_factory(new_id))
            self._items[new_id] = conversation
        conversation.updated_at = time.time()
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self._items.get(conversation_id)


# ============================================================
# Current conversation (per chat turn)
# ============================================================

_current: ContextVar[Conversation | None] = ContextVar("healthcare_referral_conversation", default=None)


@contextmanager
def conversation_turn(conversation: Conversation) -> Iterator[Conversation]:
    token = _current.set(conversation)
    try:
        yield conversation
    finally:
        _current.reset(token)


def current_conversation() -> Conversation | None:
    return _current.get()


# ============================================================
# Deterministic slot filling (conservative)
# ============================================================
#
# The model handles open-ended language. The fields an action
# depends on are additionally captured deterministically when the staff
# member's message clearly supplies them, so a short answer such as
# "Cardiology." or "Aisha Wiegand." is never lost or re-asked.

_REFERRAL_INTENT = re.compile(r"\b(refer|referral|referring)\b", re.I)
_REVIEW_INTENT = re.compile(r"\b(review|whether|appropriate|makes? sense|should (?:i|we) refer)\b", re.I)
_CHANGE_INTENT = re.compile(r"\b(cancel|update|change|amend)\b", re.I)
_QUESTION = re.compile(r"\?\s*$|^(what|which|who|when|where|why|how|is|are|does|do|can|could)\b", re.I)
_CONFIRM = re.compile(
    r"^(yes|yep|yeah|y|ok|okay|sure|confirm(?:ed)?|correct|go ahead|proceed|do it|please do|"
    r"yes,? (?:please|go ahead|proceed|do it|that'?s right|confirm(?:ed)?)|sounds good|that'?s (?:right|correct))"
    r"[\s.!]*(?:please)?[\s.!]*$",
    re.I,
)
_DECLINE = re.compile(
    r"^(no|nope|no thanks?|no thank you|cancel(?: that| it| this)?|stop|never ?mind|forget (?:it|that)|"
    r"don'?t|do not|abort|not now)[\s.!,]*(?:please|thanks?)?[\s.!]*$",
    re.I,
)
_CHANGE_CUE = re.compile(r"\b(actually|instead|change|make it|rather|switch|should be|correction)\b", re.I)
_BACK_REFERENCE = re.compile(r"\b(she|he|her|him|they|them|the same patient|this patient|that patient)\b", re.I)
_REASON_LEAD = re.compile(r"\b(?:because(?: of)?|due to|as (?:she|he|they) (?:has|have))\s+(.+)$", re.I)
_PRONOUN_SUBJECT = re.compile(r"^(she|he|they|the patient|patient)\s+(has|have|is|was|had|reports?|complains?)\b", re.I)
_NAME_CANDIDATE = re.compile(r"\b([A-Z][a-z]+(?:\d+)?(?:\s+[A-Z][a-z]+(?:\d+)?){0,2})\b")
_NOT_NAMES = {
    "refer", "referral", "create", "please", "review", "yes", "no", "she", "he", "they", "hi", "hello",
    "hey", "thanks", "thank", "cancel", "update", "i", "we", "the", "a", "can", "could", "would", "what",
    "which", "show", "find", "get", "tell", "is", "does", "ok", "okay",
    # Sentence starters and pronouns that are capitalised but never names.
    "also", "and", "now", "then", "send", "make", "her", "him", "them", "his", "their",
    "lets", "let's", "need", "want", "add", "great", "sure", "fine",
}


def _clean(text: str) -> str:
    return " ".join(text.split()).strip(" .!?")


def _find_department(message: str) -> str | None:
    lowered = message.casefold()
    for department in SUPPORTED_DEPARTMENTS:
        if re.search(rf"\b{re.escape(department.casefold())}\b", lowered):
            return department
    return None


_DEPARTMENT_PHRASE = re.compile(
    r"\b(?:refer\w*|send)\b.*?\bto\s+(?:the\s+|a\s+|an\s+)?(?P<phrase>[A-Za-z][A-Za-z&\- ]{0,40}?)"
    r"(?:\s+(?:department|dept|clinic|team|service|unit))?(?=\s+(?:for|because|due to|as)\b|\s*[.,!?]|\s*$)",
    re.I,
)
_DEPARTMENT_SUFFIX = re.compile(r"\s+(?:department|dept|clinic|team|service|unit)$", re.I)


def _department_mention(message: str, *, awaiting_department: bool) -> str | None:
    """The department the staff member named, as typed (may be unsupported)."""

    match = _DEPARTMENT_PHRASE.search(message)
    if match:
        return match.group("phrase").strip()
    text = _clean(message)
    if awaiting_department and 1 <= len(text.split()) <= 3 and not _QUESTION.search(text):
        return _DEPARTMENT_SUFFIX.sub("", text).strip()
    return None


def _resolve_unique(name: str, date_of_birth: str | None = None) -> PatientRecord | None:
    with SessionLocal() as session:
        resolution = resolve_patient_by_name(session, name, date_of_birth)
    if resolution.status is ResolutionStatus.RESOLVED and resolution.patient is not None:
        return resolution.patient
    return None


def _name_candidates(message: str) -> list[str]:
    names = []
    for match in _NAME_CANDIDATE.finditer(message):
        tokens = [t for t in match.group(1).split() if t.casefold() not in _NOT_NAMES]
        if tokens and not normalize_department(" ".join(tokens)):
            names.append(" ".join(tokens))
    return names


def _find_patient(message: str, *, whole_message: bool) -> PatientRecord | None:
    """A uniquely resolvable patient the staff member literally named (+ optional DOB)."""

    name_text, dob = split_name_and_date_of_birth(message)
    if whole_message:
        candidate = _clean(name_text)
        if 1 <= len(candidate.split()) <= 4 and not _QUESTION.search(candidate):
            patient = _resolve_unique(candidate, dob)
            if patient:
                return patient

    for match in _NAME_CANDIDATE.finditer(name_text):
        tokens = [t for t in match.group(1).split() if t.casefold() not in _NOT_NAMES]
        if not tokens or normalize_department(" ".join(tokens)):
            continue
        patient = _resolve_unique(" ".join(tokens), dob)
        if patient:
            return patient
    return None


def _clarification(message: str) -> tuple[str, str | None] | None:
    """
    (name, date_of_birth) when the message reads as an answer to "which
    patient?" - e.g. "Ram Kumar, 02/10/2003", "Ram Kumar", "DOB 2/10/2003".
    """

    name, dob = split_name_and_date_of_birth(message)
    name = _clean(name)
    if _QUESTION.search(name) or _REFERRAL_INTENT.search(name) or _CHANGE_INTENT.search(name):
        return None
    tokens = [t for t in name.split() if t.casefold() not in _NOT_NAMES]
    if dob is not None and len(tokens) <= 5:
        return " ".join(tokens), dob
    if dob is None and 1 <= len(tokens) <= 4 and _name_candidates(name):
        return " ".join(tokens), None
    return None


@dataclass
class SlotUpdate:
    """What the deterministic pass captured from one staff message."""

    filled: list[str] = field(default_factory=list)
    confirmed: bool = False
    consumed: bool = False  # the message was fully an answer to the pending question
    # A patient name was given but did not resolve to exactly one patient
    # (not found / ambiguous). The model + workflow must handle and report it.
    unresolved_name: bool = False
    # The staff member declined / withdrew the referral being prepared.
    declined: bool = False
    # A department was named that referrals cannot be sent to (as typed).
    unknown_department: str | None = None
    # The staff member answered "which patient?" for a pending lookup:
    # the resolution (RESOLVED -> continue the request; otherwise explain).
    lookup_resolution: PatientResolution | None = None

    @property
    def changed(self) -> bool:
        return bool(self.filled) or self.confirmed


def has_action_intent(message: str) -> bool:
    """Did the staff member ask for a referral, a change or a review?"""

    return bool(
        _REFERRAL_INTENT.search(message) or _CHANGE_INTENT.search(message) or _REVIEW_INTENT.search(message)
    )


def apply_staff_message(conversation: Conversation, message: str) -> SlotUpdate:
    """Record the staff message and capture clearly supplied referral facts."""

    conversation.staff_messages.append(message)
    ctx = conversation.context
    update = SlotUpdate()
    text = _clean(message)

    # Confirmation of an already complete referral.
    if ctx.intent == "REFERRAL" and ctx.stage == "READY" and _CONFIRM.match(text):
        ctx.stage = "CONFIRMED"
        ctx.awaiting = None
        update.confirmed = update.consumed = True
        return update

    # "No" / "cancel" / "never mind" while a referral is being prepared:
    # nothing is submitted and the prepared details are discarded.
    if ctx.intent == "REFERRAL" and ctx.stage in ("COLLECTING", "READY", "CONFIRMED") and _DECLINE.match(text):
        ctx.abandon()
        update.declined = update.consumed = True
        return update

    # Answer to "which patient did you mean?" for a pending read request.
    pending = ctx.pending_lookup
    if pending is not None and ctx.intent is None:
        clarification = _clarification(message)
        if clarification is None:
            ctx.pending_lookup = None  # the staff member moved on
        else:
            name, dob = clarification
            query = PatientReference(patient_name=name or pending.query.patient_name, date_of_birth=dob)
            with SessionLocal() as session:
                resolution = resolve_patient_by_name(session, query.patient_name, query.date_of_birth)
            update.lookup_resolution = resolution
            update.consumed = True
            if resolution.status is ResolutionStatus.RESOLVED and resolution.patient is not None:
                if ctx.patient is None or ctx.patient.patient_id != resolution.patient.patient_id:
                    ctx.patient = resolution.patient
                    ctx.retrieved = []
                pending.query = PatientReference.from_record(resolution.patient)
                pending.resolved = True
            else:
                pending.query = query
                pending.status = resolution.status.value
                pending.candidates = [
                    {"name": c.name, "date_of_birth": c.date_of_birth} for c in resolution.candidates
                ]
            return update

    starting = bool(_REFERRAL_INTENT.search(message)) and not _REVIEW_INTENT.search(message) \
        and not _CHANGE_INTENT.search(message)
    if starting:
        ctx.start_referral(keep_patient=bool(_BACK_REFERENCE.search(message)))
    elif _REVIEW_INTENT.search(message) and _REFERRAL_INTENT.search(message):
        ctx.intent = "REVIEW"
        ctx.awaiting = None

    if ctx.intent not in ("REFERRAL", "REVIEW"):
        return update

    awaiting = ctx.awaiting

    # Patient: a uniquely resolvable name the staff member typed.
    if ctx.patient is None or starting:
        patient = _find_patient(message, whole_message=(awaiting == "patient"))
        if patient:
            ctx.patient = patient
            update.filled.append("patient")
            if awaiting == "patient" and len(text.split()) <= 4:
                update.consumed = True
        elif _name_candidates(message) or (awaiting == "patient" and 1 <= len(text.split()) <= 4
                                            and not _QUESTION.search(text)):
            update.unresolved_name = True
            if starting and _name_candidates(message):
                # Fail closed: a new request naming someone who does not
                # resolve (not found / ambiguous) must never inherit the
                # previously established patient.
                ctx.patient = None

    # Department: catalogue names, aliases ("haematology"), or an
    # unsupported department the staff member must be told about.
    department = _find_department(message)
    department_text = department
    if department is None and ctx.intent == "REFERRAL":
        mention = _department_mention(message, awaiting_department=(awaiting == "department"))
        if mention and not _resolve_unique(mention) and not is_pronoun(mention):
            normalized = normalize_department(mention)
            if normalized:
                department, department_text = normalized, mention
            elif mention.casefold() not in {"a patient", "patient", "someone", "somebody"}:
                update.unknown_department = mention
    if department and (ctx.department is None or starting):
        ctx.department = department
        update.filled.append("department")
        if awaiting == "department" and len(words(message)) <= 3:
            update.consumed = True
    elif department and department != ctx.department and ctx.intent == "REFERRAL" and _CHANGE_CUE.search(message):
        # "Actually, make it neurology": the department changes and any
        # earlier confirmation no longer applies.
        ctx.department = department
        update.filled.append("department")
        update.consumed = True
        if ctx.stage == "CONFIRMED":
            ctx.stage = "READY"

    # Reason.
    if ctx.intent == "REFERRAL" and ctx.reason is None:
        lead = _REASON_LEAD.search(message)
        named = department_text or update.unknown_department
        if lead is None and named:
            lead = re.search(
                rf"{re.escape(named)}(?:\s+(?:department|dept|clinic|team|service|unit))?\s+(?:referral\s+)?for\s+(.+)$",
                message, re.I,
            )
            # "a cardiology referral for Aisha Wiegand": that "for" names the
            # patient, not the reason.
            if lead and (any(_resolve_unique(n) for n in _name_candidates(lead.group(1))) or (
                ctx.patient and set(words(lead.group(1))) <= set(words(ctx.patient.name))
            )):
                lead = None
        if lead and not is_placeholder(lead.group(1)):
            ctx.reason = _clean(lead.group(1))
            update.filled.append("reason")
        elif awaiting == "reason" and not _QUESTION.search(text) and not is_placeholder(text) \
                and "patient" not in update.filled and "department" not in update.filled \
                and not any(_resolve_unique(n) for n in _name_candidates(message)):
            ctx.reason = text
            update.filled.append("reason")
            update.consumed = True
        elif _PRONOUN_SUBJECT.search(text) and ctx.patient is not None and not _QUESTION.search(text):
            ctx.reason = text
            update.filled.append("reason")
            update.consumed = True

    ctx.refresh_stage()

    # A single explicit, complete instruction ("Create a cardiology referral
    # for Aisha Wiegand because ...") is itself the confirmation. Details
    # gathered over several turns are confirmed explicitly first.
    if starting and ctx.intent == "REFERRAL" and not ctx.missing():
        ctx.stage, ctx.awaiting = "CONFIRMED", None

    return update


NEXT_QUESTION = {
    "patient": "Sure. Which patient would you like to refer?",
    "department": "Which department should the referral go to?",
    "reason": "What is the reason for the referral?",
}


def next_question(context: ReferralContext) -> str | None:
    """The single question to ask next, or None when nothing is missing."""

    if context.intent != "REFERRAL":
        return None
    missing = context.missing()
    return NEXT_QUESTION[missing[0]] if missing else None


# ============================================================
# MAF context provider
# ============================================================

def build_context_provider():
    """A ContextProvider that injects the structured state every turn."""

    from agent_framework import ContextProvider

    class ReferralContextProvider(ContextProvider):
        def __init__(self) -> None:
            super().__init__(source_id="healthcare_referral_context")

        async def before_run(self, *, agent, session, context, state) -> None:
            conversation = current_conversation()
            if conversation is not None:
                context.extend_instructions(self.source_id, conversation.context.for_model())

    return ReferralContextProvider()
