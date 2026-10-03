"""
Healthcare Referral Agent - FastAPI application.

    React / client
        -> FastAPI (this module)
        -> ReferralWorkflow / ChatService
        -> Microsoft Agent Framework (chat only)
        -> healthcare tools
        -> SQLite

Referral creation is reachable through ``ReferralWorkflow.run``.
No route writes referrals directly.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterator

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.agent import OpenAIConfigurationError
from backend.chat import ChatService, LLMUnavailableError
from backend.clinical_workflow import (
    ClinicalReviewRequest,
    ClinicalReviewWorkflow,
    ClinicalWorkflowStatus,
    get_default_clinical_workflow,
)
from backend.database.connection import SessionLocal
from backend.database.schema import ensure_schema
from backend.domain import ReferralRequest, WorkflowStatus
from backend.patient_resolution import normalize_patient_id, search_patients
from backend.schemas import (
    ChatRequest,
    ActionResponse,
    ChatResponse,
    ClinicalWorkflowListItem,
    ClinicalWorkflowListResponse,
    ClinicalWorkflowRequest,
    ClinicalWorkflowResponse,
    ErrorResponse,
    HealthResponse,
    PatientDetailResponse,
    PatientSearchResponse,
    PatientSummary,
    ReferralCreateRequest,
    ReferralResponse,
)
from backend.tools.patient_tools import get_patient
from backend.workflow import ReferralWorkflow, get_default_workflow


logger = logging.getLogger("xverba.api")

SERVICE_NAME = "x-verba-healthcare-referral-agent"

STATUS_CODES: dict[WorkflowStatus, int] = {
    WorkflowStatus.REFERRAL_CREATED: 201,
    WorkflowStatus.INVALID_REQUEST: 422,
    WorkflowStatus.PATIENT_NOT_FOUND: 404,
    WorkflowStatus.MULTIPLE_PATIENT_MATCHES: 409,
    WorkflowStatus.INVALID_PATIENT_ID: 422,
    WorkflowStatus.GOVERNANCE_DENIED: 403,
    WorkflowStatus.GOVERNANCE_TERMINAL: 403,
    WorkflowStatus.INTERNAL_ERROR: 500,
}

CLINICAL_STATUS_CODES: dict[str, int] = {
    ClinicalWorkflowStatus.REFERRAL_CREATED.value: 201,
    ClinicalWorkflowStatus.NO_ACTION_RECOMMENDED.value: 200,
    ClinicalWorkflowStatus.INVALID_REQUEST.value: 422,
    ClinicalWorkflowStatus.PATIENT_NOT_FOUND.value: 404,
    ClinicalWorkflowStatus.MULTIPLE_PATIENT_MATCHES.value: 409,
    ClinicalWorkflowStatus.INVALID_PATIENT_ID.value: 422,
    ClinicalWorkflowStatus.ANALYSIS_UNAVAILABLE.value: 503,
    ClinicalWorkflowStatus.INVALID_PROPOSAL.value: 502,
    ClinicalWorkflowStatus.GOVERNANCE_DENIED.value: 403,
    ClinicalWorkflowStatus.GOVERNANCE_TERMINAL.value: 403,
    ClinicalWorkflowStatus.INTERNAL_ERROR.value: 500,
}


# ============================================================
# Application
# ============================================================

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_schema()
    yield


app = FastAPI(
    title="Healthcare Referral Agent API",
    version="1.0.0",
    description=(
        "Healthcare referral API. Referrals are created through a "
        "deterministic workflow: patient resolution -> database write."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(
            "XVERBA_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ============================================================
# Dependencies
# ============================================================

def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_workflow() -> ReferralWorkflow:
    return get_default_workflow()


def get_clinical_workflow() -> ClinicalReviewWorkflow:
    return get_default_clinical_workflow()


_chat_service: ChatService | None = None


def get_chat_service() -> ChatService:
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service


# ============================================================
# Error handling
# ============================================================

def _error(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=error, message=message).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": WorkflowStatus.INVALID_REQUEST.value,
            "message": "The request is invalid.",
            "details": details,
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled API error", exc_info=exc)
    return _error(500, WorkflowStatus.INTERNAL_ERROR.value, "An internal error occurred.")


_ERRORS = {
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


# ============================================================
# Routes
# ============================================================

@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME)


@app.post(
    "/chat",
    response_model=ChatResponse,
    tags=["agent"],
    summary="Natural-language request to the healthcare agent",
    responses={503: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def chat(
    body: ChatRequest,
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse | JSONResponse:
    try:
        result = await service.chat(body.message, body.conversation_id)
    except OpenAIConfigurationError:
        return _error(503, "LLM_UNAVAILABLE", "OPENAI_API_KEY is not configured.")
    except LLMUnavailableError:
        return _error(
            503,
            "LLM_UNAVAILABLE",
            "The language model is unavailable. Check the OpenAI API configuration and connectivity.",
        )

    workflow_results = [ReferralResponse.from_outcome(o) for o in result.workflow_results]
    clinical_reviews = [ClinicalWorkflowResponse.from_result(r) for r in result.clinical_reviews]
    action_results = [ActionResponse.from_outcome(a) for a in result.action_results]
    outcomes = (
        [r.success for r in workflow_results]
        + [r.success for r in clinical_reviews]
        + [a.success for a in action_results]
    )

    return ChatResponse(
        success=all(outcomes) if outcomes else True,
        reply=result.reply,
        agent_reply=result.agent_reply,
        referral_attempted=bool(workflow_results),
        workflow_results=workflow_results,
        clinical_review_attempted=bool(clinical_reviews),
        clinical_reviews=clinical_reviews,
        action_attempted=bool(action_results),
        action_results=action_results,
        conversation_id=result.conversation_id,
        context=result.context,
    )


@app.post(
    "/referrals",
    response_model=ReferralResponse,
    status_code=201,
    tags=["referrals"],
    summary="Create a governed referral",
    responses={
        403: {"model": ReferralResponse, "description": "Blocked by X-Verba governance (DENY / TERMINAL)"},
        404: {"model": ReferralResponse, "description": "PATIENT_NOT_FOUND"},
        409: {"model": ReferralResponse, "description": "MULTIPLE_PATIENT_MATCHES"},
        422: {"model": ReferralResponse, "description": "INVALID_REQUEST / INVALID_PATIENT_ID"},
        500: {"model": ReferralResponse, "description": "INTERNAL_ERROR"},
    },
)
async def create_referral(
    body: ReferralCreateRequest,
    workflow: ReferralWorkflow = Depends(get_workflow),
) -> JSONResponse:
    outcome = await workflow.run(
        ReferralRequest(
            patient_name=body.patient_name,
            patient_id=body.patient_id,
            date_of_birth=body.date_of_birth,
            department=body.department,
            reason=body.reason,
        )
    )

    response = ReferralResponse.from_outcome(outcome)

    return JSONResponse(
        status_code=STATUS_CODES[outcome.status],
        content=response.model_dump(mode="json"),
    )


@app.post(
    "/clinical-workflows",
    response_model=ClinicalWorkflowResponse,
    status_code=201,
    tags=["clinical workflows"],
    summary="Run a governed clinical review for a patient",
    responses={
        200: {"model": ClinicalWorkflowResponse, "description": "NO_ACTION_RECOMMENDED"},
        403: {"model": ClinicalWorkflowResponse, "description": "GOVERNANCE_DENIED (REVIEW_REQUIRED) / GOVERNANCE_TERMINAL"},
        404: {"model": ClinicalWorkflowResponse, "description": "PATIENT_NOT_FOUND"},
        409: {"model": ClinicalWorkflowResponse, "description": "MULTIPLE_PATIENT_MATCHES"},
        422: {"model": ClinicalWorkflowResponse, "description": "INVALID_REQUEST / INVALID_PATIENT_ID"},
        502: {"model": ClinicalWorkflowResponse, "description": "INVALID_PROPOSAL (AI output rejected)"},
        503: {"model": ClinicalWorkflowResponse, "description": "ANALYSIS_UNAVAILABLE"},
        500: {"model": ClinicalWorkflowResponse, "description": "INTERNAL_ERROR"},
    },
)
async def run_clinical_workflow(
    body: ClinicalWorkflowRequest,
    workflow: ClinicalReviewWorkflow = Depends(get_clinical_workflow),
) -> JSONResponse:
    result = await workflow.run(
        ClinicalReviewRequest(
            patient_name=body.patient_name,
            date_of_birth=body.date_of_birth,
            department=body.department,
            question=body.question,
            idempotency_key=body.idempotency_key,
        )
    )
    response = ClinicalWorkflowResponse.from_result(result)
    return JSONResponse(
        status_code=CLINICAL_STATUS_CODES.get(result.status or "", 500),
        content=response.model_dump(mode="json"),
    )


@app.get(
    "/clinical-workflows",
    response_model=ClinicalWorkflowListResponse,
    tags=["clinical workflows"],
    summary="List recent clinical workflow runs",
)
async def list_clinical_workflows(
    limit: int = Query(20, ge=1, le=100),
    workflow: ClinicalReviewWorkflow = Depends(get_clinical_workflow),
) -> ClinicalWorkflowListResponse:
    return ClinicalWorkflowListResponse(
        workflows=[ClinicalWorkflowListItem(**run) for run in workflow.store.list(limit)]
    )


@app.get(
    "/clinical-workflows/{workflow_id}",
    response_model=ClinicalWorkflowResponse,
    tags=["clinical workflows"],
    summary="Get one clinical workflow run",
    responses=_ERRORS,
)
async def get_clinical_workflow_run(
    workflow_id: str = Path(..., max_length=64),
    workflow: ClinicalReviewWorkflow = Depends(get_clinical_workflow),
) -> ClinicalWorkflowResponse | JSONResponse:
    from backend.clinical_workflow import ClinicalWorkflowResult

    if normalize_patient_id(workflow_id) is None:  # canonical UUID shape
        return _error(422, "INVALID_WORKFLOW_ID", "workflow_id must be a UUID.")

    data = workflow.store.get(workflow_id.strip().lower())
    if data is None:
        return _error(404, "WORKFLOW_NOT_FOUND", "No clinical workflow run with this ID.")

    return ClinicalWorkflowResponse.from_result(ClinicalWorkflowResult.from_dict(data))


@app.get(
    "/clinical-review-requests",
    tags=["clinical workflows"],
    summary="List clinical review requests (human review queue)",
)
async def list_clinical_review_requests(
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    from backend.database.models import ClinicalReviewRequestRecord

    rows = (
        db.query(ClinicalReviewRequestRecord)
        .order_by(ClinicalReviewRequestRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "success": True,
        "review_requests": [
            {
                "review_request_id": r.review_request_id,
                "patient_id": r.patient_id,
                "referral_id": r.referral_id,
                "department": r.department,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
                "governance_decision_id": r.governance_decision_id,
            }
            for r in rows
        ],
    }


@app.get(
    "/patients",
    response_model=PatientSearchResponse,
    tags=["patients"],
    summary="Search patients by name",
    responses=_ERRORS,
)
async def list_patients(
    name: str = Query(..., min_length=2, max_length=200, description="Name or name tokens, e.g. 'Aisha Wiegand'"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> PatientSearchResponse:
    matches, truncated = search_patients(db, name.strip(), limit=limit)

    return PatientSearchResponse(
        success=bool(matches),
        query=name,
        match_count=len(matches),
        more_matches=truncated,
        patients=[PatientSummary(**m.to_dict()) for m in matches],
    )


@app.get(
    "/patients/{patient_id}",
    response_model=PatientDetailResponse,
    tags=["patients"],
    summary="Get patient information",
    responses=_ERRORS,
)
async def get_patient_information(
    patient_id: str = Path(..., max_length=64),
    db: Session = Depends(get_db),
) -> PatientDetailResponse | JSONResponse:
    normalized = normalize_patient_id(patient_id)

    if normalized is None:
        return _error(
            422,
            WorkflowStatus.INVALID_PATIENT_ID.value,
            "The patient identifier is not a valid internal patient ID.",
        )

    details = get_patient(db, normalized)

    if not details.get("success"):
        return _error(404, WorkflowStatus.PATIENT_NOT_FOUND.value, "Patient not found.")

    return PatientDetailResponse.model_validate(details)

