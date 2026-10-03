"""
Deterministic healthcare referral workflow.

    request
       -> deterministic patient resolution (exactly one patient)
       -> internal patient UUID (from the database, never from the LLM)
       -> referral candidate
       -> validation and database write

This is the single authoritative path for referral creation.
Both the FastAPI ``POST /referrals`` endpoint and the
Microsoft Agent Framework ``create_referral`` tool call ``run()``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy.orm import Session

from backend import actions
from backend.database.connection import SessionLocal
from backend.domain import (
    GovernanceDecision,
    GovernanceOutcome,
    PatientRecord,
    ReferralCandidate,
    ReferralOutcome,
    ReferralRecord,
    ReferralRequest,
    WorkflowStatus,
)
from backend.patient_resolution import (
    ResolutionStatus,
    normalize_patient_id,
    resolve_patient_by_name,
)


logger = logging.getLogger(__name__)

MAX_DEPARTMENT_LENGTH = 100
MAX_REASON_LENGTH = 2000

GovernanceFn = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]
ReferralWriter = Callable[[Session, GovernanceDecision], ReferralRecord]
SessionFactory = Callable[[], Session]


def _allow_governance_result() -> dict[str, Any]:
    """No-op decision for the governance-free runtime."""
    return {
        "decision": "ALLOW",
        "decision_id": "manual-no-governance",
        "reason": "No governance layer is configured; the request was accepted.",
    }


async def _allow_governance_async(_candidate: dict[str, Any]) -> dict[str, Any]:
    return _allow_governance_result()


_RESOLUTION_TO_STATUS: dict[ResolutionStatus, WorkflowStatus] = {
    ResolutionStatus.INVALID_INPUT: WorkflowStatus.INVALID_REQUEST,
    ResolutionStatus.NOT_FOUND: WorkflowStatus.PATIENT_NOT_FOUND,
    ResolutionStatus.MULTIPLE_MATCHES: WorkflowStatus.MULTIPLE_PATIENT_MATCHES,
    ResolutionStatus.INVALID_PATIENT_ID: WorkflowStatus.INVALID_PATIENT_ID,
}


class ReferralWorkflow:
    """Orchestrates one referral. Stateless and re-entrant."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        governance: GovernanceFn | None = None,
        writer: ReferralWriter = actions.create_referral_record,
    ) -> None:
        self._session_factory = session_factory
        self._governance = governance or _allow_governance_async
        self._writer = writer

    # --------------------------------------------------------
    # Public entry point
    # --------------------------------------------------------

    async def run(self, request: ReferralRequest) -> ReferralOutcome:
        """
        Execute the workflow. Never raises: every failure is converted
        into a structured ``ReferralOutcome``. Internal details are logged,
        not returned.
        """

        try:
            return await self._run(request)
        except Exception:
            logger.exception("Referral workflow failed with an unexpected error.")
            return ReferralOutcome(
                status=WorkflowStatus.INTERNAL_ERROR,
                message=(
                    "An internal error occurred. The referral was not created."
                ),
            )

    # --------------------------------------------------------
    # Steps
    # --------------------------------------------------------

    async def _run(self, request: ReferralRequest) -> ReferralOutcome:

        # 1. Request shape --------------------------------------------------
        problem = self._validate_request(request)
        if problem is not None:
            return ReferralOutcome(
                status=WorkflowStatus.INVALID_REQUEST,
                message=problem,
            )

        # 2. Deterministic patient resolution -------------------------------
        patient: PatientRecord | None = None

        if request.patient_id is not None:
            patient_id = normalize_patient_id(request.patient_id)
            if patient_id is None:
                return ReferralOutcome(
                    status=WorkflowStatus.INVALID_PATIENT_ID,
                    message=(
                        "The patient identifier is not a valid internal "
                        "patient ID. The referral was not created."
                    ),
                )
            # Existence of an explicitly supplied ID is deliberately NOT
            # pre-checked here: the VSL invariant PATIENT_MUST_EXIST is the
            # enforcement authority for that rule.
        else:
            with self._session_factory() as session:
                resolution = resolve_patient_by_name(session, request.patient_name, request.date_of_birth)

            if resolution.status is not ResolutionStatus.RESOLVED:
                return ReferralOutcome(
                    status=_RESOLUTION_TO_STATUS[resolution.status],
                    message=resolution.message + " The referral was not created.",
                    candidates=resolution.candidates,
                )

            if resolution.patient is None:  # defensive; RESOLVED implies a patient
                raise RuntimeError("Resolution reported RESOLVED without a patient.")
            patient = resolution.patient
            patient_id = normalize_patient_id(patient.patient_id)
            if patient_id is None:  # defensive; resolution already checks
                return ReferralOutcome(
                    status=WorkflowStatus.INVALID_PATIENT_ID,
                    message="Resolved patient has an invalid identifier.",
                )

        # 3. Deterministic candidate ----------------------------------------
        from backend.proposals import normalize_department

        department = normalize_department(request.department) or request.department.strip()
        candidate = ReferralCandidate(
            patient_id=patient_id,
            department=department,
            reason=request.reason.strip(),
            origin=request.origin,
            evidence=tuple(request.evidence),
            workflow_id=request.workflow_id,
        )

        # 4. X-Verba VSL governance (the enforcement boundary) --------------
        gate_result = await self._governance(candidate.to_governance_input())
        decision = GovernanceDecision.from_gate_result(gate_result, candidate)

        if decision.outcome is GovernanceOutcome.DENY:
            return ReferralOutcome(
                status=WorkflowStatus.GOVERNANCE_DENIED,
                message=_deny_message(decision, candidate.department),
                patient=patient,
                patient_id=patient_id,
                governance=decision,
            )

        if decision.outcome is GovernanceOutcome.TERMINAL:
            return ReferralOutcome(
                status=WorkflowStatus.GOVERNANCE_TERMINAL,
                message=(
                    "X-Verba governance entered a terminal state "
                    f"({decision.terminal_state}). Automated referral "
                    "creation was halted and the referral was not created."
                ),
                patient=patient,
                patient_id=patient_id,
                governance=decision,
            )

        # 5. ALLOW -> the controlled side effect ----------------------------
        if not decision.allows_side_effect:  # exhaustive guard, fail closed
            raise RuntimeError("Unhandled governance outcome.")

        try:
            with self._session_factory() as session:
                record = self._writer(session, decision)
        except Exception:
            logger.exception(
                "Referral write failed after governance ALLOW (decision_id=%s).",
                decision.decision_id,
            )
            return ReferralOutcome(
                status=WorkflowStatus.INTERNAL_ERROR,
                message=(
                    "Governance allowed the referral but the database write "
                    "failed and was rolled back. The referral was not created."
                ),
                patient=patient,
                patient_id=patient_id,
                governance=decision,
            )

        return ReferralOutcome(
            status=WorkflowStatus.REFERRAL_CREATED,
            message=(
                f"Referral {record.referral_id} to {record.department} "
                "was created after X-Verba governance returned ALLOW."
            ),
            patient=patient,
            patient_id=patient_id,
            referral=record,
            governance=decision,
        )

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    @staticmethod
    def _validate_request(request: ReferralRequest) -> str | None:
        """
        Structural validation only. Completeness of department / reason
        is a governance concern (PreNode REFERRAL_REQUEST_VALID), so blank
        values are passed through to VSL rather than rejected here.
        """

        if not isinstance(request, ReferralRequest):
            return "Malformed referral request."

        has_name = request.patient_name is not None
        has_id = request.patient_id is not None

        if has_name == has_id:
            return "Provide exactly one of patient_name or patient_id."

        if not isinstance(request.department, str) or not isinstance(request.reason, str):
            return "department and reason must be text."

        if len(request.department) > MAX_DEPARTMENT_LENGTH:
            return "department is too long."

        if len(request.reason) > MAX_REASON_LENGTH:
            return "reason is too long."

        if not isinstance(request.evidence, tuple) or not all(
            isinstance(item, str) for item in request.evidence
        ):
            return "evidence must be a tuple of text items."

        return None


def _deny_message(decision: GovernanceDecision, department: str) -> str:
    if decision.pre_node == "REFERRAL_NOT_DUPLICATE":
        return (
            f"An equivalent referral to {department} is already active for this "
            "patient. X-Verba governance denied a duplicate; no new referral was created."
        )
    if decision.pre_node == "REFERRAL_TARGET_VALID":
        return (
            f"'{department}' is not a supported referral department. X-Verba "
            "governance denied the referral; it was not created."
        )
    return "X-Verba governance denied the referral. The referral was not created."


_default_workflow: ReferralWorkflow | None = None


def get_default_workflow() -> ReferralWorkflow:
    """Process-wide workflow bound to the application database and ledger."""

    global _default_workflow
    if _default_workflow is None:
        _default_workflow = ReferralWorkflow()
    return _default_workflow



# ============================================================
# Phase 3: update / cancel / clinical review request
# ============================================================
#
# Same discipline as ReferralWorkflow: deterministic patient resolution
# -> candidate -> database write. Never raises; failures become outcomes.

from backend.domain import (  # noqa: E402
    ActionOutcome,
    ActionStatus,
    ReferralChangeCandidate,
    ReviewRequestCandidate,
)


@dataclass(frozen=True)
class ReferralChangeRequest:
    action: str  # "UPDATE" | "CANCEL"
    referral_id: Any
    patient_name: str | None = None
    patient_id: str | None = None
    department: str | None = None
    date_of_birth: str | None = None
    reason: str | None = None
    cancellation_reason: str | None = None


@dataclass(frozen=True)
class ClinicalReviewRequestInput:
    reason: str
    patient_name: str | None = None
    patient_id: str | None = None
    department: str | None = None
    referral_id: Any = None
    date_of_birth: str | None = None


_RESOLUTION_TO_ACTION_STATUS = {
    ResolutionStatus.INVALID_INPUT: ActionStatus.INVALID_REQUEST,
    ResolutionStatus.NOT_FOUND: ActionStatus.PATIENT_NOT_FOUND,
    ResolutionStatus.MULTIPLE_MATCHES: ActionStatus.MULTIPLE_PATIENT_MATCHES,
    ResolutionStatus.INVALID_PATIENT_ID: ActionStatus.INVALID_PATIENT_ID,
}


class GovernedActionWorkflow:
    """UPDATE_REFERRAL, CANCEL_REFERRAL and REQUEST_CLINICAL_REVIEW."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        update_governance: GovernanceFn | None = None,
        cancel_governance: GovernanceFn | None = None,
        review_governance: GovernanceFn | None = None,
        change_writer=actions.change_referral_record,
        review_writer=actions.create_review_request_record,
    ) -> None:
        self._session_factory = session_factory
        self._update_governance = update_governance or _allow_governance_async
        self._cancel_governance = cancel_governance or _allow_governance_async
        self._review_governance = review_governance or _allow_governance_async
        self._change_writer = change_writer
        self._review_writer = review_writer

    # -- shared -------------------------------------------------------

    def _resolve(self, action: str, patient_name: Any, patient_id: Any, date_of_birth: Any = None):
        """Returns (patient_id, patient, failure_outcome)."""

        if (patient_name is None) == (patient_id is None):
            return None, None, ActionOutcome(action, ActionStatus.INVALID_REQUEST,
                                             "Provide exactly one of patient_name or patient_id.")
        if patient_id is not None:
            normalized = normalize_patient_id(patient_id)
            if normalized is None:
                return None, None, ActionOutcome(action, ActionStatus.INVALID_PATIENT_ID,
                                                 "The patient identifier is not valid. No change was made.")
            return normalized, None, None

        with self._session_factory() as session:
            resolution = resolve_patient_by_name(session, patient_name, date_of_birth)
        if resolution.status is not ResolutionStatus.RESOLVED or resolution.patient is None:
            return None, None, ActionOutcome(
                action,
                _RESOLUTION_TO_ACTION_STATUS.get(resolution.status, ActionStatus.INTERNAL_ERROR),
                resolution.message + " No change was made.",
                candidates=resolution.candidates,
            )
        normalized = normalize_patient_id(resolution.patient.patient_id)
        if normalized is None:
            return None, None, ActionOutcome(action, ActionStatus.INVALID_PATIENT_ID,
                                             "The patient record has an invalid identifier. No change was made.")
        return normalized, resolution.patient, None

    async def _govern_and_write(self, action, governance, writer, candidate, patient_id, patient, success_status, success_message):
        decision = GovernanceDecision.from_gate_result(
            await governance(candidate.to_governance_input()), candidate
        )
        if decision.outcome is GovernanceOutcome.DENY:
            return ActionOutcome(action, ActionStatus.GOVERNANCE_DENIED,
                                 "X-Verba governance denied the action. No change was made.",
                                 patient, patient_id, governance=decision)
        if decision.outcome is GovernanceOutcome.TERMINAL:
            return ActionOutcome(action, ActionStatus.GOVERNANCE_TERMINAL,
                                 "X-Verba governance halted the automated action (terminal state). No change was made.",
                                 patient, patient_id, governance=decision)
        if not decision.allows_side_effect:
            raise RuntimeError("Unhandled governance outcome.")

        try:
            with self._session_factory() as session:
                record = writer(session, decision)
        except Exception:
            logger.exception("%s write failed after ALLOW (decision_id=%s).", action, decision.decision_id)
            return ActionOutcome(action, ActionStatus.INTERNAL_ERROR,
                                 "Governance allowed the action but the database write failed and was rolled back. No change was made.",
                                 patient, patient_id, governance=decision)

        if isinstance(record, ReferralRecord):
            return ActionOutcome(action, success_status, success_message(record), patient, patient_id,
                                 referral=record, governance=decision)
        return ActionOutcome(action, success_status, success_message(record), patient, patient_id,
                             review_request=record, governance=decision)

    # -- update / cancel ----------------------------------------------

    async def change_referral(self, request: ReferralChangeRequest) -> ActionOutcome:
        action = "CANCEL_REFERRAL" if request.action == "CANCEL" else "UPDATE_REFERRAL"
        try:
            if request.action not in ("UPDATE", "CANCEL"):
                return ActionOutcome(action, ActionStatus.INVALID_REQUEST, "Unknown referral change.")
            try:
                referral_id = int(request.referral_id)
            except (TypeError, ValueError):
                return ActionOutcome(action, ActionStatus.INVALID_REQUEST,
                                     "A valid referral number is required. No change was made.")

            patient_id, patient, failure = self._resolve(action, request.patient_name, request.patient_id, request.date_of_birth)
            if failure is not None:
                return failure

            department = request.department
            if request.action == "UPDATE" and department is not None and str(department).strip():
                from backend.proposals import normalize_department

                department = normalize_department(department) or str(department).strip()
            elif request.action == "UPDATE":
                department = None

            candidate = ReferralChangeCandidate(
                action=request.action,
                referral_id=referral_id,
                patient_id=patient_id,
                department=department,
                reason=(request.reason.strip() if isinstance(request.reason, str) and request.reason.strip() else None)
                if request.action == "UPDATE" else None,
                cancellation_reason=(request.cancellation_reason or "").strip() or None
                if request.action == "CANCEL" else None,
            )

            if request.action == "CANCEL":
                return await self._govern_and_write(
                    action, self._cancel_governance, self._change_writer, candidate, patient_id, patient,
                    ActionStatus.REFERRAL_CANCELLED,
                    lambda r: f"Referral {r.referral_id} ({r.department}) was cancelled after governance approval.",
                )
            return await self._govern_and_write(
                action, self._update_governance, self._change_writer, candidate, patient_id, patient,
                ActionStatus.REFERRAL_UPDATED,
                lambda r: f"Referral {r.referral_id} was updated after governance approval.",
            )
        except Exception:
            logger.exception("Referral change workflow failed.")
            return ActionOutcome(action, ActionStatus.INTERNAL_ERROR, "An internal error occurred. No change was made.")

    # -- clinical review request --------------------------------------

    async def request_review(self, request: ClinicalReviewRequestInput) -> ActionOutcome:
        action = "REQUEST_CLINICAL_REVIEW"
        try:
            referral_id = None
            if request.referral_id not in (None, ""):
                try:
                    referral_id = int(request.referral_id)
                except (TypeError, ValueError):
                    return ActionOutcome(action, ActionStatus.INVALID_REQUEST, "Invalid referral number.")

            patient_id, patient, failure = self._resolve(action, request.patient_name, request.patient_id, request.date_of_birth)
            if failure is not None:
                return failure

            department = None
            if isinstance(request.department, str) and request.department.strip():
                from backend.proposals import normalize_department

                department = normalize_department(request.department) or request.department.strip()

            candidate = ReviewRequestCandidate(
                patient_id=patient_id,
                reason=(request.reason or "").strip(),
                department=department,
                referral_id=referral_id,
            )
            return await self._govern_and_write(
                action, self._review_governance, self._review_writer, candidate, patient_id, patient,
                ActionStatus.CLINICAL_REVIEW_REQUESTED,
                lambda r: f"Clinical review request {r.review_request_id} was opened after governance approval.",
            )
        except Exception:
            logger.exception("Clinical review request workflow failed.")
            return ActionOutcome(action, ActionStatus.INTERNAL_ERROR, "An internal error occurred. No request was made.")


_default_action_workflow: GovernedActionWorkflow | None = None


def get_default_action_workflow() -> GovernedActionWorkflow:
    global _default_action_workflow
    if _default_action_workflow is None:
        _default_action_workflow = GovernedActionWorkflow()
    return _default_action_workflow
