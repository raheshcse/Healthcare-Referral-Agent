"""Framework-independent domain types for the healthcare referral application."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

class WorkflowStatus(str, Enum):
    REFERRAL_CREATED = "REFERRAL_CREATED"
    INVALID_REQUEST = "INVALID_REQUEST"
    PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"
    MULTIPLE_PATIENT_MATCHES = "MULTIPLE_PATIENT_MATCHES"
    INVALID_PATIENT_ID = "INVALID_PATIENT_ID"
    INTERNAL_ERROR = "INTERNAL_ERROR"

@dataclass(frozen=True)
class PatientRecord:
    patient_id: str; name: str; date_of_birth: str | None = None; gender: str | None = None
    def to_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass(frozen=True)
class ReferralRequest:
    department: str; reason: str; patient_name: str | None = None; patient_id: str | None = None; date_of_birth: str | None = None
    origin: str | None = None; evidence: tuple[str, ...] = (); workflow_id: str | None = None

@dataclass(frozen=True)
class ReferralRecord:
    referral_id: int; patient_id: str; department: str; reason: str; status: str; created_at: datetime
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self); data["created_at"] = self.created_at.isoformat(); return data

@dataclass(frozen=True)
class ReferralOutcome:
    status: WorkflowStatus; message: str; patient: PatientRecord | None = None; patient_id: str | None = None; referral: ReferralRecord | None = None
    candidates: tuple[PatientRecord, ...] = field(default_factory=tuple)
    @property
    def success(self) -> bool: return self.status is WorkflowStatus.REFERRAL_CREATED
    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "status": self.status.value, "message": self.message, "patient_id": self.patient_id, "patient": self.patient.to_dict() if self.patient else None, "referral": self.referral.to_dict() if self.referral else None, "candidates": [c.to_dict() for c in self.candidates]}

class ActionStatus(str, Enum):
    REFERRAL_UPDATED = "REFERRAL_UPDATED"; REFERRAL_CANCELLED = "REFERRAL_CANCELLED"; CLINICAL_REVIEW_REQUESTED = "CLINICAL_REVIEW_REQUESTED"
    INVALID_REQUEST = "INVALID_REQUEST"; PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"; MULTIPLE_PATIENT_MATCHES = "MULTIPLE_PATIENT_MATCHES"; INVALID_PATIENT_ID = "INVALID_PATIENT_ID"; INTERNAL_ERROR = "INTERNAL_ERROR"
ACTION_SUCCESS = {ActionStatus.REFERRAL_UPDATED, ActionStatus.REFERRAL_CANCELLED, ActionStatus.CLINICAL_REVIEW_REQUESTED}

@dataclass(frozen=True)
class ReviewRequestRecord:
    review_request_id: int; patient_id: str; referral_id: int | None; department: str | None; reason: str; status: str; created_at: datetime
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self); data["created_at"] = self.created_at.isoformat(); return data

@dataclass(frozen=True)
class ActionOutcome:
    action: str; status: ActionStatus; message: str; patient: PatientRecord | None = None; patient_id: str | None = None; referral: ReferralRecord | None = None; review_request: ReviewRequestRecord | None = None
    candidates: tuple[PatientRecord, ...] = field(default_factory=tuple)
    @property
    def success(self) -> bool: return self.status in ACTION_SUCCESS
    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "success": self.success, "status": self.status.value, "message": self.message, "patient_id": self.patient_id, "patient": self.patient.to_dict() if self.patient else None, "referral": self.referral.to_dict() if self.referral else None, "review_request": self.review_request.to_dict() if self.review_request else None, "candidates": [c.to_dict() for c in self.candidates]}
