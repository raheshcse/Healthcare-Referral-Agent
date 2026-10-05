"""Validated application workflows for referrals and clinical-review requests."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Callable
from sqlalchemy.orm import Session
from backend import actions
from backend.database.connection import SessionLocal
from backend.domain import ActionOutcome, ActionStatus, PatientRecord, ReferralOutcome, ReferralRequest, WorkflowStatus
from backend.patient_resolution import ResolutionStatus, resolve_patient_by_id, resolve_patient_by_name
logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]
_RESOLUTION = {ResolutionStatus.INVALID_INPUT: WorkflowStatus.INVALID_REQUEST, ResolutionStatus.NOT_FOUND: WorkflowStatus.PATIENT_NOT_FOUND, ResolutionStatus.MULTIPLE_MATCHES: WorkflowStatus.MULTIPLE_PATIENT_MATCHES, ResolutionStatus.INVALID_PATIENT_ID: WorkflowStatus.INVALID_PATIENT_ID}
_ACTION_RESOLUTION = {ResolutionStatus.INVALID_INPUT: ActionStatus.INVALID_REQUEST, ResolutionStatus.NOT_FOUND: ActionStatus.PATIENT_NOT_FOUND, ResolutionStatus.MULTIPLE_MATCHES: ActionStatus.MULTIPLE_PATIENT_MATCHES, ResolutionStatus.INVALID_PATIENT_ID: ActionStatus.INVALID_PATIENT_ID}

class ReferralWorkflow:
    def __init__(self, *, session_factory: SessionFactory = SessionLocal, writer=actions.create_referral_record): self._session_factory, self._writer = session_factory, writer
    async def run(self, request: ReferralRequest) -> ReferralOutcome:
        try: return self._run(request)
        except Exception:
            logger.exception("Referral workflow failed."); return ReferralOutcome(WorkflowStatus.INTERNAL_ERROR, "An internal error occurred. The referral was not created.")
    def _run(self, request: ReferralRequest) -> ReferralOutcome:
        problem = self._validate_request(request)
        if problem: return ReferralOutcome(WorkflowStatus.INVALID_REQUEST, problem)
        patient = None
        if request.patient_id is not None:
            with self._session_factory() as session: resolution = resolve_patient_by_id(session, request.patient_id)
        else:
            with self._session_factory() as session: resolution = resolve_patient_by_name(session, request.patient_name, request.date_of_birth)
        if resolution.status is not ResolutionStatus.RESOLVED or resolution.patient is None:
            return ReferralOutcome(_RESOLUTION.get(resolution.status, WorkflowStatus.INTERNAL_ERROR), resolution.message + " The referral was not created.", candidates=resolution.candidates)
        patient, patient_id = resolution.patient, resolution.patient.patient_id
        from backend.proposals import normalize_department
        department = normalize_department(request.department)
        if department is None: return ReferralOutcome(WorkflowStatus.INVALID_REQUEST, f"'{request.department.strip()}' is not a supported referral department.", patient, patient_id)
        with self._session_factory() as session: record = self._writer(session, patient_id=patient_id, department=department, reason=request.reason.strip())
        return ReferralOutcome(WorkflowStatus.REFERRAL_CREATED, f"Referral {record.referral_id} to {record.department} was created.", patient, patient_id, record)
    @staticmethod
    def _validate_request(request: ReferralRequest) -> str | None:
        if not isinstance(request, ReferralRequest): return "Malformed referral request."
        if (request.patient_name is None) == (request.patient_id is None): return "Provide exactly one of patient_name or patient_id."
        if not isinstance(request.department, str) or not request.department.strip(): return "A referral department is required."
        if not isinstance(request.reason, str) or not request.reason.strip(): return "A referral reason is required."
        if len(request.department) > 100 or len(request.reason) > 2000: return "Referral field is too long."
        if not isinstance(request.evidence, tuple) or not all(isinstance(item, str) for item in request.evidence): return "evidence must be a tuple of text items."
        return None
_default_workflow: ReferralWorkflow | None = None
def get_default_workflow() -> ReferralWorkflow:
    global _default_workflow
    if _default_workflow is None: _default_workflow = ReferralWorkflow()
    return _default_workflow

@dataclass(frozen=True)
class ReferralChangeRequest:
    action: str; referral_id: Any; patient_name: str | None = None; patient_id: str | None = None; department: str | None = None; date_of_birth: str | None = None; reason: str | None = None; cancellation_reason: str | None = None
@dataclass(frozen=True)
class ClinicalReviewRequestInput:
    reason: str; patient_name: str | None = None; patient_id: str | None = None; department: str | None = None; referral_id: Any = None; date_of_birth: str | None = None
class ActionWorkflow:
    """Referral changes and clinical-review requests, validated before database writes."""
    def __init__(self, *, session_factory: SessionFactory = SessionLocal, **_unused: Any): self._session_factory = session_factory
    def _resolve(self, action, name, patient_id, dob):
        if (name is None) == (patient_id is None): return None, None, ActionOutcome(action, ActionStatus.INVALID_REQUEST, "Provide exactly one of patient_name or patient_id.")
        with self._session_factory() as session:
            resolution = resolve_patient_by_name(session, name, dob) if name is not None else resolve_patient_by_id(session, patient_id)
        if resolution.status is not ResolutionStatus.RESOLVED or resolution.patient is None: return None, None, ActionOutcome(action, _ACTION_RESOLUTION.get(resolution.status, ActionStatus.INTERNAL_ERROR), resolution.message, candidates=resolution.candidates)
        return resolution.patient.patient_id, resolution.patient, None
    async def change_referral(self, request: ReferralChangeRequest) -> ActionOutcome:
        action = "CANCEL_REFERRAL" if request.action == "CANCEL" else "UPDATE_REFERRAL"
        try: referral_id = int(request.referral_id)
        except (TypeError, ValueError): return ActionOutcome(action, ActionStatus.INVALID_REQUEST, "A valid referral number is required.")
        patient_id, patient, failure = self._resolve(action, request.patient_name, request.patient_id, request.date_of_birth)
        if failure: return failure
        if request.action not in {"UPDATE", "CANCEL"}: return ActionOutcome(action, ActionStatus.INVALID_REQUEST, "Unknown referral change.")
        if request.action == "UPDATE" and not ((request.department or "").strip() or (request.reason or "").strip()): return ActionOutcome(action, ActionStatus.INVALID_REQUEST, "Provide a department or reason to update.", patient, patient_id)
        department = request.department.strip() if isinstance(request.department, str) and request.department.strip() else None
        if department:
            from backend.proposals import normalize_department
            department = normalize_department(department)
            if department is None: return ActionOutcome(action, ActionStatus.INVALID_REQUEST, "Unsupported referral department.", patient, patient_id)
        try:
            with self._session_factory() as session: record = actions.change_referral_record(session, referral_id=referral_id, patient_id=patient_id, action=request.action, department=department, reason=(request.reason or "").strip() or None)
        except actions.StaleReferralError as exc: return ActionOutcome(action, ActionStatus.INVALID_REQUEST, str(exc), patient, patient_id)
        except Exception: logger.exception("Referral change failed"); return ActionOutcome(action, ActionStatus.INTERNAL_ERROR, "The referral was not changed.", patient, patient_id)
        status = ActionStatus.REFERRAL_CANCELLED if request.action == "CANCEL" else ActionStatus.REFERRAL_UPDATED
        return ActionOutcome(action, status, f"Referral {record.referral_id} was {'cancelled' if request.action == 'CANCEL' else 'updated'}.", patient, patient_id, referral=record)
    async def request_review(self, request: ClinicalReviewRequestInput) -> ActionOutcome:
        patient_id, patient, failure = self._resolve("REQUEST_CLINICAL_REVIEW", request.patient_name, request.patient_id, request.date_of_birth)
        if failure: return failure
        if not isinstance(request.reason, str) or not request.reason.strip(): return ActionOutcome("REQUEST_CLINICAL_REVIEW", ActionStatus.INVALID_REQUEST, "A review reason is required.", patient, patient_id)
        with self._session_factory() as session: record = actions.create_review_request_record(session, patient_id=patient_id, reason=request.reason.strip(), department=request.department, referral_id=request.referral_id)
        return ActionOutcome("REQUEST_CLINICAL_REVIEW", ActionStatus.CLINICAL_REVIEW_REQUESTED, "Clinical review request was created.", patient, patient_id, review_request=record)
_default_actions: ActionWorkflow | None = None
def get_default_action_workflow() -> ActionWorkflow:
    global _default_actions
    if _default_actions is None: _default_actions = ActionWorkflow()
    return _default_actions
