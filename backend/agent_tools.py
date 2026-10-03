"""
Microsoft Agent Framework tool functions exposed to the configured OpenAI model.

Design rule: the LLM never handles internal patient identifiers.

    LLM responsibility          Application responsibility
    ------------------          --------------------------
    understand the request      resolve the patient deterministically
    extract patient name        obtain the internal patient UUID
    extract department          build the referral candidate
    extract reason              run X-Verba VSL governance
    call create_referral        perform the database write on ALLOW

Tool results returned to the model therefore contain names, dates of
birth and outcomes - never patient UUIDs - so the model has nothing to
copy between calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Iterator

from agent_framework import tool

from backend.clinical_workflow import (
    ClinicalReviewRequest,
    ClinicalWorkflowResult,
    get_default_clinical_workflow,
)
from backend.conversation import current_conversation
from backend.database.connection import SessionLocal
from backend.domain import ActionOutcome, PatientRecord, ReferralOutcome, ReferralRequest
from backend.patient_resolution import (
    ResolutionStatus,
    resolve_patient_by_name,
    search_patients,
    split_name_and_date_of_birth,
)
from backend.tools.patient_tools import get_patient
from backend.workflow import (
    ClinicalReviewRequestInput,
    ReferralChangeRequest,
    get_default_action_workflow,
    get_default_workflow,
)


# ============================================================
# Capture of authoritative workflow outcomes for the /chat API
# ============================================================

_captured_outcomes: ContextVar[list[ReferralOutcome] | None] = ContextVar(
    "xverba_captured_referral_outcomes",
    default=None,
)


@contextmanager
def capture_referral_outcomes() -> Iterator[list[ReferralOutcome]]:
    """
    Collect every ReferralOutcome produced by ``create_referral`` while
    the block runs, so the API can return the authoritative result
    instead of relying on the LLM's paraphrase of it.
    """

    outcomes: list[ReferralOutcome] = []
    token = _captured_outcomes.set(outcomes)
    try:
        yield outcomes
    finally:
        _captured_outcomes.reset(token)


def _record(outcome: ReferralOutcome) -> None:
    sink = _captured_outcomes.get()
    if sink is not None:
        sink.append(outcome)


_captured_reviews: ContextVar[list[ClinicalWorkflowResult] | None] = ContextVar(
    "xverba_captured_clinical_reviews",
    default=None,
)


@contextmanager
def capture_clinical_reviews() -> Iterator[list[ClinicalWorkflowResult]]:
    """Collect every clinical-review workflow result produced in the block."""

    results: list[ClinicalWorkflowResult] = []
    token = _captured_reviews.set(results)
    try:
        yield results
    finally:
        _captured_reviews.reset(token)


def _name_matches(wanted: str, actual: str | None) -> bool:
    tokens = (wanted or "").strip().casefold().split()
    return bool(tokens) and bool(actual) and all(t in actual.casefold() for t in tokens)


def _review_this_turn(patient_name: str, department: str | None) -> ClinicalWorkflowResult | None:
    """A review already run in this chat turn for the same patient/department."""

    wanted_department = (department or "").strip().casefold()
    for review in _captured_reviews.get() or []:
        requested = review.request or {}
        if (
            (requested.get("patient_name") or "").strip().casefold() == (patient_name or "").strip().casefold()
            and (requested.get("department") or "").strip().casefold() == wanted_department
        ):
            return review
    return None


def _referral_created_by_review(patient_name: str, department: str) -> ClinicalWorkflowResult | None:
    wanted_department = (department or "").strip().casefold()
    if not wanted_department:
        return None
    for review in _captured_reviews.get() or []:
        referral = review.referral or {}
        if (
            referral
            and (referral.get("department") or "").casefold() == wanted_department
            and _name_matches(patient_name, (review.patient or {}).get("name"))
        ):
            return review
    return None


def _review_for_model(result: ClinicalWorkflowResult) -> dict[str, Any]:
    """LLM-facing view of a clinical review. No patient UUID, no raw output."""

    proposal = result.proposal or {}
    return {
        "success": result.success,
        "status": result.status,
        "workflow_state": result.state,
        "message": result.message,
        "recommendation": proposal.get("recommendation"),
        "proposed_action": proposal.get("action"),
        "department": proposal.get("department"),
        "referral_id": (result.referral or {}).get("referral_id"),
        "governance_decision": (result.governance or {}).get("decision"),
        "candidates": [
            {"name": c.get("name"), "date_of_birth": c.get("date_of_birth")}
            for c in result.candidates
        ],
    }


def _already_created_this_turn(
    patient_name: str,
    department: str,
) -> ReferralOutcome | None:
    sink = _captured_outcomes.get()
    if not sink:
        return None

    wanted_name = (patient_name or "").strip().casefold()
    wanted_department = (department or "").strip().casefold()
    if not wanted_name or not wanted_department:
        return None

    for outcome in sink:
        if (
            outcome.success
            and outcome.referral is not None
            and outcome.patient is not None
            and outcome.referral.department.casefold() == wanted_department
            and all(
                token in outcome.patient.name.casefold()
                for token in wanted_name.split()
            )
        ):
            return outcome

    return None


def _public_patient(record: Any) -> dict[str, Any]:
    return {
        "name": record.name,
        "date_of_birth": record.date_of_birth,
        "gender": record.gender,
    }


def _outcome_for_model(outcome: ReferralOutcome) -> dict[str, Any]:
    """LLM-facing view of a workflow outcome. Contains no patient UUID."""

    return {
        "success": outcome.success,
        "status": outcome.status.value,
        "message": outcome.message,
        "patient_name": outcome.patient.name if outcome.patient else None,
        "department": outcome.referral.department if outcome.referral else None,
        "referral_id": outcome.referral.referral_id if outcome.referral else None,
        "governance_decision": (
            outcome.governance.outcome.value if outcome.governance else None
        ),
        "candidates": [_public_patient(c) for c in outcome.candidates],
    }


# ============================================================
# Phase 3: governed-action outcomes and conversation state
# ============================================================

_captured_actions: ContextVar[list[ActionOutcome] | None] = ContextVar(
    "xverba_captured_action_outcomes",
    default=None,
)


@contextmanager
def capture_action_outcomes() -> Iterator[list[ActionOutcome]]:
    """Collect update / cancel / review-request outcomes produced in the block."""

    outcomes: list[ActionOutcome] = []
    token = _captured_actions.set(outcomes)
    try:
        yield outcomes
    finally:
        _captured_actions.reset(token)


def _record_action(outcome: ActionOutcome) -> None:
    sink = _captured_actions.get()
    if sink is not None:
        sink.append(outcome)


def _action_for_model(outcome: ActionOutcome) -> dict[str, Any]:
    """LLM-facing view of a governed action. Contains no patient UUID."""

    return {
        "success": outcome.success,
        "status": outcome.status.value,
        "message": outcome.message,
        "referral_id": outcome.referral.referral_id if outcome.referral else None,
        "referral_status": outcome.referral.status if outcome.referral else None,
        "review_request_id": outcome.review_request.review_request_id if outcome.review_request else None,
        "governance_decision": outcome.governance.outcome.value if outcome.governance else None,
        "candidates": [_public_patient(c) for c in outcome.candidates],
    }


def _resolve_reference(patient_name: str | None) -> str | None:
    """Pronouns ("she", "that patient") -> the patient established in this conversation."""

    conversation = current_conversation()
    return conversation.resolve_patient_reference(patient_name) if conversation else patient_name


def _patient_args(patient_name: str | None, date_of_birth: str | None = None) -> dict[str, str | None]:
    """
    Clean, structured patient arguments for a workflow request:
    {"patient_name": ..., "date_of_birth": ...} - never one merged string.
    """

    dob = date_of_birth.strip() if isinstance(date_of_birth, str) and date_of_birth.strip() else None
    conversation = current_conversation()
    if conversation is not None:
        reference = conversation.patient_reference(patient_name, dob)
        return {"patient_name": reference.patient_name or patient_name, "date_of_birth": reference.date_of_birth}
    name, embedded = split_name_and_date_of_birth(patient_name or "")
    return {"patient_name": name or patient_name, "date_of_birth": dob or embedded}


_DOB_DOC = (
    "Date of birth ONLY if the staff member gave one (e.g. '02/10/2003'), "
    "otherwise an empty string. Never put the date of birth in patient_name."
)


def _remember_patient(patient: PatientRecord | None, category: str | None = None) -> None:
    """Record the patient (and data category) established by a successful tool call."""

    conversation = current_conversation()
    if conversation is None or patient is None:
        return
    context = conversation.context
    if context.patient is None or context.patient.patient_id != patient.patient_id:
        context.patient = patient
        context.retrieved = []
    if category and category not in context.retrieved:
        context.retrieved.append(category)
    if category and context.pending_lookup is not None:
        context.pending_lookup = None  # the waiting request has now been answered
    context.refresh_stage()


def _finish_referral_flow(referral_id: int | None, status: str) -> None:
    conversation = current_conversation()
    if conversation is not None:
        conversation.context.mark_submitted(referral_id, status)


# ============================================================
# Tools: patient lookup (read-only)
# ============================================================

@tool(approval_mode="never_require")
def search_patient(
    name: Annotated[
        str,
        "Patient name as written by the healthcare staff member, "
        "e.g. 'Aisha Wiegand'.",
    ],
) -> dict:
    """
    Find patients by name (read-only).

    Use when the staff member wants to find a patient, or when a name may
    match several patients. Not needed before create_referral.
    """

    session = SessionLocal()
    try:
        matches, truncated = search_patients(session, name or "")
    finally:
        session.close()

    conversation = current_conversation()
    if conversation is not None:
        conversation.names_from_tools.update(m.name.casefold() for m in matches)
        if len(matches) == 1:
            _remember_patient(matches[0])

    return {
        "success": bool(matches),
        "match_count": len(matches),
        "more_matches": truncated,
        "patients": [_public_patient(m) for m in matches],
        "message": (
            "No patient found." if not matches
            else "Patients found. Ask the staff member to clarify if more than one."
        ),
    }


def _resolve_for_tool(patient_name: str | None, date_of_birth: str | None = None):
    """Deterministic resolution of the structured patient reference for read tools."""

    dob = date_of_birth.strip() if isinstance(date_of_birth, str) and date_of_birth.strip() else None
    conversation = current_conversation()
    if conversation is not None:
        _, resolution = conversation.resolve_patient(patient_name, dob)
    else:
        session = SessionLocal()
        try:
            resolution = resolve_patient_by_name(session, patient_name, dob)
        finally:
            session.close()
    if resolution.status is not ResolutionStatus.RESOLVED or resolution.patient is None:
        return None, {
            "success": False,
            "status": resolution.status.value,
            "message": resolution.message,
            "candidates": [_public_patient(c) for c in resolution.candidates],
        }
    return resolution.patient, None


@tool(approval_mode="never_require")
def get_patient_information(
    patient_name: Annotated[
        str,
        "Patient name only (e.g. 'Ram Kumar'), or 'she'/'the patient' "
        "for the patient already discussed.",
    ],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """
    Retrieve a patient's full clinical summary (read-only): demographics,
    conditions, medications, allergies, recent observations and encounters.
    Prefer the narrower get_patient_* tools when only one category is needed.
    """

    patient, failure = _resolve_for_tool(patient_name, date_of_birth)
    if failure:
        return failure

    session = SessionLocal()
    try:
        details = get_patient(session, patient.patient_id)
    finally:
        session.close()

    if details.get("success") and isinstance(details.get("patient"), dict):
        details["patient"].pop("patient_id", None)
    details.pop("patient_id", None)
    _remember_patient(patient, "summary")
    return details


# ============================================================
# Tools: focused clinical data (read-only)
# ============================================================

MAX_ITEMS = 25


def _category(patient_name: str | None, category: str, key: str, transform, date_of_birth: str | None = None) -> dict:
    patient, failure = _resolve_for_tool(patient_name, date_of_birth)
    if failure:
        return failure

    session = SessionLocal()
    try:
        details = get_patient(session, patient.patient_id)
    finally:
        session.close()

    items = [transform(item) for item in details.get(key, [])][:MAX_ITEMS]
    _remember_patient(patient, category)
    return {
        "success": True,
        "patient": patient.name,
        category: items,
        "count": len(items),
        "message": f"No {category} recorded." if not items else f"{len(items)} {category} found.",
    }


@tool(approval_mode="never_require")
def get_patient_conditions(
    patient_name: Annotated[str, "Patient name only, or 'she'/'the patient' for the patient already discussed."],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """List the patient's recorded conditions (diagnoses/findings) with status and onset (read-only)."""

    return _category(patient_name, "conditions", "conditions", lambda c: c, date_of_birth)


@tool(approval_mode="never_require")
def get_patient_medications(
    patient_name: Annotated[str, "Patient name only, or 'she'/'the patient' for the patient already discussed."],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """List the patient's recorded medications with status (read-only)."""

    return _category(patient_name, "medications", "medications", lambda m: m, date_of_birth)


@tool(approval_mode="never_require")
def get_patient_allergies(
    patient_name: Annotated[str, "Patient name only, or 'she'/'the patient' for the patient already discussed."],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """List the patient's recorded allergies with status (read-only)."""

    return _category(patient_name, "allergies", "allergies", lambda a: a, date_of_birth)


@tool(approval_mode="never_require")
def get_patient_observations(
    patient_name: Annotated[str, "Patient name only, or 'she'/'the patient' for the patient already discussed."],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """List the patient's 20 most recent observations (vitals, labs, scores) with values and dates (read-only)."""

    return _category(patient_name, "observations", "observations", lambda o: o, date_of_birth)


@tool(approval_mode="never_require")
def get_patient_encounters(
    patient_name: Annotated[str, "Patient name only, or 'she'/'the patient' for the patient already discussed."],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """List the patient's 10 most recent encounters (visit type, status, date) (read-only)."""

    return _category(patient_name, "encounters", "recent_encounters", lambda e: e, date_of_birth)


# ============================================================
# Tools: governed side effects
# ============================================================

@tool(approval_mode="never_require")
async def create_referral(
    patient_name: Annotated[
        str,
        "Patient name as the staff member wrote it, or 'she'/'the patient' for "
        "the patient already discussed. Never an ID or UUID.",
    ],
    department: Annotated[
        str,
        "Department receiving the referral, e.g. 'Cardiology'.",
    ],
    reason: Annotated[
        str,
        "Clinical reason for the referral, in the staff member's own words.",
    ],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """
    Create a healthcare referral (governed side effect).

    Call ONCE when the staff member has asked for a referral and the
    patient, department and reason are all known. X-Verba governance
    decides whether it is created. Report the returned message exactly.
    """

    patient_name = _resolve_reference(patient_name)

    # A clinical review in this turn already created this referral.
    reviewed = _referral_created_by_review(patient_name, department)
    if reviewed is not None:
        view = _review_for_model(reviewed)
        view["message"] = (
            "This referral was already created by the clinical review in this "
            f"conversation turn (referral {view['referral_id']}). No duplicate was created."
        )
        return view

    # Guard against the model repeating the same referral within one turn.
    duplicate = _already_created_this_turn(patient_name, department)
    if duplicate is not None:
        view = _outcome_for_model(duplicate)
        view["message"] = (
            "This referral was already created in this conversation turn "
            f"(referral {view['referral_id']}). No duplicate was created."
        )
        return view

    outcome = await get_default_workflow().run(
        ReferralRequest(
            **_patient_args(patient_name, date_of_birth),
            department=department,
            reason=reason,
        )
    )

    _record(outcome)
    if outcome.patient is not None:
        _remember_patient(outcome.patient)
    _finish_referral_flow(outcome.referral.referral_id if outcome.referral else None, outcome.status.value)

    return _outcome_for_model(outcome)


@tool(approval_mode="never_require")
async def update_referral(
    referral_number: Annotated[int, "The referral number the staff member gave (e.g. 12)."],
    patient_name: Annotated[str, "Patient the referral belongs to, or 'she'/'the patient'."],
    department: Annotated[str, "New department, or empty string to keep it."] = "",
    reason: Annotated[str, "New reason in the staff member's words, or empty string to keep it."] = "",
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """
    Change the department and/or reason of an existing ACTIVE referral
    (governed side effect). Only when the staff member asks to change a
    specific referral.
    """

    outcome = await get_default_action_workflow().change_referral(
        ReferralChangeRequest(
            action="UPDATE",
            referral_id=referral_number,
            **_patient_args(patient_name, date_of_birth),
            department=department or None,
            reason=reason or None,
        )
    )
    _record_action(outcome)
    if outcome.patient is not None:
        _remember_patient(outcome.patient)
    return _action_for_model(outcome)


@tool(approval_mode="never_require")
async def cancel_referral(
    referral_number: Annotated[int, "The referral number the staff member gave (e.g. 12)."],
    patient_name: Annotated[str, "Patient the referral belongs to, or 'she'/'the patient'."],
    cancellation_reason: Annotated[str, "Why it is cancelled, in the staff member's words."],
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """
    Cancel an existing ACTIVE referral (governed side effect). Only when the
    staff member explicitly asks to cancel a specific referral.
    """

    outcome = await get_default_action_workflow().change_referral(
        ReferralChangeRequest(
            action="CANCEL",
            referral_id=referral_number,
            **_patient_args(patient_name, date_of_birth),
            cancellation_reason=cancellation_reason,
        )
    )
    _record_action(outcome)
    if outcome.patient is not None:
        _remember_patient(outcome.patient)
    return _action_for_model(outcome)


@tool(approval_mode="never_require")
async def request_clinical_review(
    patient_name: Annotated[str, "Patient name, or 'she'/'the patient'."],
    reason: Annotated[str, "Why a clinician should review, in the staff member's words."],
    department: Annotated[str, "Related department, or empty string."] = "",
    referral_number: Annotated[int, "Related referral number, or 0 if none."] = 0,
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """
    Ask a human clinician to review a patient or referral (governed side
    effect: opens a clinical review request). Use when the staff member asks
    for a clinician's review, e.g. after a referral was blocked.
    """

    outcome = await get_default_action_workflow().request_review(
        ClinicalReviewRequestInput(
            **_patient_args(patient_name, date_of_birth),
            reason=reason,
            department=department or None,
            referral_id=referral_number or None,
        )
    )
    _record_action(outcome)
    if outcome.patient is not None:
        _remember_patient(outcome.patient)
    return _action_for_model(outcome)


# ============================================================
# Tools: clinical reasoning workflow (Phase 2)
# ============================================================

@tool(approval_mode="never_require")
async def review_patient_for_referral(
    patient_name: Annotated[
        str,
        "Patient name as the staff member wrote it, or 'she'/'the patient'. Never an ID or UUID.",
    ],
    department: Annotated[
        str,
        "Department the staff member asked about, e.g. 'Cardiology'. "
        "Use an empty string if they did not name one.",
    ] = "",
    date_of_birth: Annotated[str, _DOB_DOC] = "",
) -> dict:
    """
    Review a patient's record and decide whether a referral is appropriate.

    Use this when the staff member asks you to REVIEW a patient or asks
    WHETHER a referral is appropriate. The application retrieves the
    record, runs the clinical analysis and, if a referral is proposed,
    submits it to X-Verba governance. Report the returned message exactly.
    Do NOT call create_referral afterwards for the same request.
    """

    patient = _patient_args(patient_name, date_of_birth)
    patient_name = patient["patient_name"]
    department = department or None

    duplicate = _review_this_turn(patient_name, department)
    if duplicate is not None:
        view = _review_for_model(duplicate)
        view["message"] = "This review was already run in this conversation turn. " + view["message"]
        return view

    result = await get_default_clinical_workflow().run(
        ClinicalReviewRequest(patient_name=patient_name, department=department,
                              date_of_birth=patient["date_of_birth"])
    )

    sink = _captured_reviews.get()
    if sink is not None:
        sink.append(result)

    if result.patient:
        _remember_patient(
            PatientRecord(
                patient_id=result.patient["patient_id"],
                name=result.patient["name"],
                date_of_birth=result.patient.get("date_of_birth"),
                gender=result.patient.get("gender"),
            ),
            "clinical review",
        )

    return _review_for_model(result)


AGENT_TOOLS = [
    search_patient,
    get_patient_information,
    get_patient_conditions,
    get_patient_medications,
    get_patient_allergies,
    get_patient_observations,
    get_patient_encounters,
    review_patient_for_referral,
    create_referral,
    update_referral,
    cancel_referral,
    request_clinical_review,
]
