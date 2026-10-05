"""Pydantic request and response models for the Healthcare Referral API."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.domain import ReferralOutcome, WorkflowStatus
class ErrorResponse(BaseModel): success: Literal[False] = False; error: str; message: str
class PatientSummary(BaseModel): patient_id: str; name: str; date_of_birth: str | None = None; gender: str | None = None
class ReferralCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_name: str | None = Field(None, max_length=200); patient_id: str | None = Field(None, max_length=64); date_of_birth: str | None = Field(None, max_length=40); department: str = Field(..., max_length=100); reason: str = Field(..., max_length=2000)
class ReferralInfo(BaseModel): referral_id: int; patient_id: str; department: str; reason: str; status: str; created_at: datetime
class ReferralResponse(BaseModel):
    success: bool; status: WorkflowStatus; error: WorkflowStatus | None = None; message: str; patient_id: str | None = None; patient: PatientSummary | None = None; referral: ReferralInfo | None = None; candidates: list[PatientSummary] = Field(default_factory=list)
    @classmethod
    def from_outcome(cls, outcome: ReferralOutcome):
        data = outcome.to_dict(); data['error'] = None if outcome.success else outcome.status; return cls.model_validate(data)
class PatientSearchResponse(BaseModel): success: bool; query: str; match_count: int; more_matches: bool; patients: list[PatientSummary]
class PatientDetailResponse(BaseModel): success: Literal[True] = True; patient: PatientSummary; conditions: list[dict[str, Any]]; medications: list[dict[str, Any]]; allergies: list[dict[str, Any]]; observations: list[dict[str, Any]]; recent_encounters: list[dict[str, Any]]
class ClinicalWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_name: str = Field(..., min_length=1, max_length=200); date_of_birth: str | None = Field(None, max_length=40); department: str | None = Field(None, max_length=100); question: str | None = Field(None, max_length=1000); idempotency_key: str | None = Field(None, min_length=8, max_length=128)
class WorkflowTransition(BaseModel): state: str; at: str; note: str | None = None
class ClinicalWorkflowResponse(BaseModel):
    workflow_id: str; success: bool; state: str; status: str | None; error: str | None = None; message: str; request: dict[str, Any]; transitions: list[WorkflowTransition]; patient: dict[str, Any] | None = None; candidates: list[dict[str, Any]] = Field(default_factory=list); context_summary: dict[str, int] | None = None; raw_ai_output: str | None = None; proposal: dict[str, Any] | None = None; proposal_errors: list[str] = Field(default_factory=list); ignored_ai_fields: list[str] = Field(default_factory=list); referral: ReferralInfo | None = None; replayed: bool = False
    @classmethod
    def from_result(cls, result: Any):
        data = result.to_dict(); data['error'] = None if result.success else result.status; return cls.model_validate(data)
class ClinicalWorkflowListItem(BaseModel): workflow_id: str; created_at: datetime; state: str; status: str | None = None; patient_name: str; department: str | None = None; referral_id: int | None = None
class ClinicalWorkflowListResponse(BaseModel): success: Literal[True] = True; workflows: list[ClinicalWorkflowListItem]
class ChatRequest(BaseModel): model_config = ConfigDict(extra="forbid"); message: str = Field(..., min_length=1, max_length=4000); conversation_id: str | None = Field(None, max_length=64)
class ActionResponse(BaseModel):
    action: str; success: bool; status: str; error: str | None = None; message: str; patient_id: str | None = None; patient: PatientSummary | None = None; referral: ReferralInfo | None = None; review_request: dict[str, Any] | None = None; candidates: list[PatientSummary] = Field(default_factory=list)
    @classmethod
    def from_outcome(cls, outcome: Any):
        data = outcome.to_dict(); data['error'] = None if outcome.success else outcome.status.value; return cls.model_validate(data)
class ConversationContextView(BaseModel): intent: str | None = None; stage: str; patient: dict[str, Any] | None = None; patient_query: dict[str, Any] | None = None; department: str | None = None; reason: str | None = None; missing: list[str] = Field(default_factory=list); awaiting: str | None = None; last_referral_id: int | None = None
class ChatResponse(BaseModel):
    success: bool; reply: str; agent_reply: str | None = None; referral_attempted: bool = False; workflow_results: list[ReferralResponse] = Field(default_factory=list); clinical_review_attempted: bool = False; clinical_reviews: list[ClinicalWorkflowResponse] = Field(default_factory=list); action_attempted: bool = False; action_results: list[ActionResponse] = Field(default_factory=list); conversation_id: str | None = None; context: ConversationContextView | None = None
class HealthResponse(BaseModel): status: Literal['ok'] = 'ok'; service: str
