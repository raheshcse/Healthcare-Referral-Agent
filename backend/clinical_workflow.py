"""
Phase 2: clinical-review workflow.

    Clinician request
      -> deterministic patient resolution           (backend.patient_resolution)
      -> clinical information retrieval + context    (backend.clinical_context)
      -> AI analysis / recommendation                (backend.analysis, untrusted)
      -> validated action proposal                   (backend.proposals)
      -> workflow execution                          (backend.workflow.ReferralWorkflow)
      -> workflow outcome

Separation of concerns:
    AI recommendation  -> text + JSON from the model (never executed)
    action proposal    -> ActionProposal built and validated in code
    workflow execution  -> deterministic side effect path
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from backend.analysis import AnalysisUnavailableError, ClinicalAnalyzer
from backend.clinical_context import PatientRecordUnavailableError, retrieve_clinical_context
from backend.clinical_runs import ClinicalRunStore, DuplicateIdempotencyKey
from backend.database.connection import SessionLocal
from backend.domain import ReferralRequest, WorkflowStatus
from backend.patient_resolution import ResolutionStatus, resolve_patient_by_name
from backend.proposals import (
    ACTION_CREATE_REFERRAL,
    ACTION_NO_ACTION,
    normalize_department,
    validate_proposal,
)
from backend.workflow import ReferralWorkflow, get_default_workflow


logger = logging.getLogger(__name__)

MAX_RAW_OUTPUT_STORED = 4000


class WorkflowState(str, Enum):
    REQUESTED = "REQUESTED"
    PATIENT_RESOLVED = "PATIENT_RESOLVED"
    DATA_RETRIEVED = "DATA_RETRIEVED"
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    GOVERNANCE_CHECK = "GOVERNANCE_CHECK"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TERMINAL = "TERMINAL"


FINAL_STATES = {
    WorkflowState.COMPLETED,
    WorkflowState.REVIEW_REQUIRED,
    WorkflowState.FAILED,
    WorkflowState.TERMINAL,
}


class ClinicalWorkflowStatus(str, Enum):
    REFERRAL_CREATED = "REFERRAL_CREATED"
    NO_ACTION_RECOMMENDED = "NO_ACTION_RECOMMENDED"
    INVALID_REQUEST = "INVALID_REQUEST"
    PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"
    MULTIPLE_PATIENT_MATCHES = "MULTIPLE_PATIENT_MATCHES"
    INVALID_PATIENT_ID = "INVALID_PATIENT_ID"
    ANALYSIS_UNAVAILABLE = "ANALYSIS_UNAVAILABLE"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    GOVERNANCE_DENIED = "GOVERNANCE_DENIED"
    GOVERNANCE_TERMINAL = "GOVERNANCE_TERMINAL"
    INTERNAL_ERROR = "INTERNAL_ERROR"


SUCCESS_STATUSES = {
    ClinicalWorkflowStatus.REFERRAL_CREATED,
    ClinicalWorkflowStatus.NO_ACTION_RECOMMENDED,
}

_RESOLUTION_STATUS = {
    ResolutionStatus.INVALID_INPUT: ClinicalWorkflowStatus.INVALID_REQUEST,
    ResolutionStatus.NOT_FOUND: ClinicalWorkflowStatus.PATIENT_NOT_FOUND,
    ResolutionStatus.MULTIPLE_MATCHES: ClinicalWorkflowStatus.MULTIPLE_PATIENT_MATCHES,
    ResolutionStatus.INVALID_PATIENT_ID: ClinicalWorkflowStatus.INVALID_PATIENT_ID,
}


@dataclass(frozen=True)
class ClinicalReviewRequest:
    patient_name: str
    department: str | None = None
    question: str | None = None
    idempotency_key: str | None = None
    date_of_birth: str | None = None  # optional, separate from the name


@dataclass
class ClinicalWorkflowResult:
    """Authoritative outcome of one run. Plain data so it round-trips via JSON."""

    workflow_id: str
    request: dict[str, Any]
    state: str = WorkflowState.REQUESTED.value
    status: str | None = None
    message: str = ""
    transitions: list[dict[str, Any]] = field(default_factory=list)
    patient: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    context_summary: dict[str, int] | None = None
    raw_ai_output: str | None = None
    proposal: dict[str, Any] | None = None
    proposal_errors: list[str] = field(default_factory=list)
    ignored_ai_fields: list[str] = field(default_factory=list)
    governance: dict[str, Any] | None = None
    referral: dict[str, Any] | None = None
    replayed: bool = False

    @property
    def success(self) -> bool:
        return self.status in {s.value for s in SUCCESS_STATUSES}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["success"] = self.success
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClinicalWorkflowResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ---- transitions -------------------------------------------------

    def enter(self, state: WorkflowState, note: str | None = None) -> None:
        self.state = state.value
        self.transitions.append(
            {
                "state": state.value,
                "at": datetime.now(timezone.utc).isoformat(),
                "note": note,
            }
        )

    def finish(self, state: WorkflowState, status: ClinicalWorkflowStatus, message: str) -> "ClinicalWorkflowResult":
        if state not in FINAL_STATES:
            raise ValueError(f"{state} is not a final state")
        self.status = status.value
        self.message = message
        self.enter(state, status.value)
        return self


class ClinicalReviewWorkflow:
    """Single authoritative service for clinical reviews (API and agent)."""

    def __init__(
        self,
        *,
        analyzer: ClinicalAnalyzer | None = None,
        referral_workflow: ReferralWorkflow | None = None,
        store: ClinicalRunStore | None = None,
        session_factory=SessionLocal,
    ) -> None:
        self._analyzer = analyzer
        self._referral_workflow = referral_workflow
        self._store = store or ClinicalRunStore(session_factory)
        self._session_factory = session_factory

    @property
    def analyzer(self) -> ClinicalAnalyzer:
        if self._analyzer is None:
            from backend.analysis import OpenAIClinicalAnalyzer

            self._analyzer = OpenAIClinicalAnalyzer()
        return self._analyzer

    @property
    def referral_workflow(self) -> ReferralWorkflow:
        return self._referral_workflow or get_default_workflow()

    @property
    def store(self) -> ClinicalRunStore:
        return self._store

    # ------------------------------------------------------------------

    async def run(self, request: ClinicalReviewRequest) -> ClinicalWorkflowResult:
        key = request.idempotency_key.strip() if isinstance(request.idempotency_key, str) else None
        key = key or None

        # Duplicate-execution prevention: a repeated submission with the
        # same idempotency key returns the recorded outcome and never
        # re-runs analysis, governance or the side effect.
        if key:
            existing = self._store.find_by_idempotency_key(key)
            if existing is not None:
                replay = ClinicalWorkflowResult.from_dict(existing)
                replay.replayed = True
                return replay

        result = ClinicalWorkflowResult(
            workflow_id=str(uuid.uuid4()),
            request={
                "patient_name": request.patient_name if isinstance(request.patient_name, str) else "",
                "date_of_birth": request.date_of_birth,
                "department": request.department,
                "question": request.question,
            },
        )
        result.enter(WorkflowState.REQUESTED)

        try:
            self._store.create(result.to_dict(), key)
        except DuplicateIdempotencyKey:
            existing = self._store.find_by_idempotency_key(key)
            replay = ClinicalWorkflowResult.from_dict(existing or result.to_dict())
            replay.replayed = True
            return replay

        try:
            await self._run(request, result)
        except Exception:
            logger.exception("Clinical workflow %s failed unexpectedly.", result.workflow_id)
            referral_written = bool(result.referral)
            result.finish(
                WorkflowState.FAILED,
                ClinicalWorkflowStatus.INTERNAL_ERROR,
                "An internal error occurred."
                + ("" if referral_written else " No referral was created."),
            )
        finally:
            try:
                self._store.save(result.to_dict())
            except Exception:
                logger.exception("Could not persist clinical workflow %s.", result.workflow_id)

        return result

    # ------------------------------------------------------------------

    async def _run(self, request: ClinicalReviewRequest, result: ClinicalWorkflowResult) -> None:

        # 1. Request ------------------------------------------------------
        department = None
        if request.department is not None and str(request.department).strip():
            department = normalize_department(request.department)
            if department is None:
                result.finish(
                    WorkflowState.FAILED,
                    ClinicalWorkflowStatus.INVALID_REQUEST,
                    f"'{request.department}' is not a supported department. No referral was created.",
                )
                return
        if request.question is not None and (not isinstance(request.question, str) or len(request.question) > 1000):
            result.finish(WorkflowState.FAILED, ClinicalWorkflowStatus.INVALID_REQUEST, "Invalid question.")
            return

        # 2. Deterministic patient resolution -----------------------------
        with self._session_factory() as session:
            resolution = resolve_patient_by_name(session, request.patient_name, request.date_of_birth)

        if resolution.status is not ResolutionStatus.RESOLVED or resolution.patient is None:
            result.candidates = [
                {"name": c.name, "date_of_birth": c.date_of_birth, "gender": c.gender}
                for c in resolution.candidates
            ]
            result.finish(
                WorkflowState.FAILED,
                _RESOLUTION_STATUS.get(resolution.status, ClinicalWorkflowStatus.INTERNAL_ERROR),
                resolution.message + " No referral was created.",
            )
            return

        patient = resolution.patient
        result.patient = patient.to_dict()
        result.enter(WorkflowState.PATIENT_RESOLVED)

        # 3. Clinical information retrieval + context assembly -----------
        try:
            with self._session_factory() as session:
                context = retrieve_clinical_context(session, patient)
        except PatientRecordUnavailableError:
            result.finish(
                WorkflowState.FAILED,
                ClinicalWorkflowStatus.PATIENT_NOT_FOUND,
                "The patient record could not be retrieved. No referral was created.",
            )
            return

        result.context_summary = context.counts()
        result.enter(WorkflowState.DATA_RETRIEVED)

        # 4. AI analysis (untrusted output) ------------------------------
        try:
            raw = await self.analyzer.analyze(
                context.for_model(), department=department, question=request.question
            )
        except AnalysisUnavailableError:
            result.finish(
                WorkflowState.FAILED,
                ClinicalWorkflowStatus.ANALYSIS_UNAVAILABLE,
                "The clinical analysis service is unavailable. No referral was created.",
            )
            return

        raw_text = raw if isinstance(raw, str) else _safe_json(raw)
        result.raw_ai_output = (raw_text or "")[:MAX_RAW_OUTPUT_STORED]
        result.enter(WorkflowState.ANALYSIS_COMPLETED)

        # 5. Validated action proposal -----------------------------------
        validation = validate_proposal(raw, requested_department=department)
        result.ignored_ai_fields = list(validation.ignored_fields)

        if not validation.valid or validation.proposal is None:
            result.proposal_errors = list(validation.errors)
            result.finish(
                WorkflowState.FAILED,
                ClinicalWorkflowStatus.INVALID_PROPOSAL,
                "The AI analysis did not produce a valid action proposal. No action was taken and no referral was created.",
            )
            return

        proposal = validation.proposal
        result.proposal = proposal.to_dict()
        result.enter(WorkflowState.ACTION_PROPOSED, proposal.action)

        if proposal.action == ACTION_NO_ACTION:
            result.finish(
                WorkflowState.COMPLETED,
                ClinicalWorkflowStatus.NO_ACTION_RECOMMENDED,
                "The review did not recommend a referral. No action was taken.",
            )
            return

        if proposal.action != ACTION_CREATE_REFERRAL:  # exhaustive guard
            raise RuntimeError("Unhandled proposal action.")

        # 6. X-Verba governance -> side effect (existing single path) ----
        #    The patient ID comes from deterministic resolution, never from
        #    the AI output.
        result.enter(WorkflowState.GOVERNANCE_CHECK)
        # Persist the resolved patient BEFORE governance: the invariant
        # GOVERNANCE_CONTEXT_MUST_MATCH_PATIENT compares the governed
        # candidate against this recorded workflow context.
        self._store.save(result.to_dict())

        outcome = await self.referral_workflow.run(
            ReferralRequest(
                patient_id=patient.patient_id,
                department=proposal.department or "",
                reason=proposal.reason or "",
                origin="AI_PROPOSAL",
                evidence=proposal.evidence,
                workflow_id=result.workflow_id,
            )
        )

        if outcome.governance is not None:
            result.governance = outcome.governance.summary()

        if outcome.status is WorkflowStatus.REFERRAL_CREATED and outcome.referral is not None:
            result.referral = outcome.referral.to_dict()
            result.enter(WorkflowState.ACTION_EXECUTED, f"referral {outcome.referral.referral_id}")
            result.finish(
                WorkflowState.COMPLETED,
                ClinicalWorkflowStatus.REFERRAL_CREATED,
                f"Referral {outcome.referral.referral_id} to {outcome.referral.department} was created after governance approval.",
            )
            return

        if outcome.status is WorkflowStatus.GOVERNANCE_DENIED:
            result.finish(
                WorkflowState.REVIEW_REQUIRED,
                ClinicalWorkflowStatus.GOVERNANCE_DENIED,
                "Governance did not approve the proposed referral. It requires human review. No referral was created.",
            )
            return

        if outcome.status is WorkflowStatus.GOVERNANCE_TERMINAL:
            result.finish(
                WorkflowState.TERMINAL,
                ClinicalWorkflowStatus.GOVERNANCE_TERMINAL,
                "Governance halted the automated action (terminal state). No referral was created.",
            )
            return

        status = (
            ClinicalWorkflowStatus.INVALID_PATIENT_ID
            if outcome.status is WorkflowStatus.INVALID_PATIENT_ID
            else ClinicalWorkflowStatus.INTERNAL_ERROR
        )
        result.finish(WorkflowState.FAILED, status, outcome.message)


def _safe_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


_default: ClinicalReviewWorkflow | None = None


def get_default_clinical_workflow() -> ClinicalReviewWorkflow:
    global _default
    if _default is None:
        _default = ClinicalReviewWorkflow()
    return _default
