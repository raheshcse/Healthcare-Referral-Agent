"""Database writes for validated referral application workflows."""
from sqlalchemy.orm import Session
from backend.database.models import ClinicalReviewRequestRecord, Referral
from backend.domain import ReferralRecord, ReviewRequestRecord
class StaleReferralError(RuntimeError): pass
def _record(row: Referral) -> ReferralRecord: return ReferralRecord(row.referral_id, row.patient_id, row.department, row.reason, row.status, row.created_at)
def create_referral_record(session: Session, *, patient_id: str, department: str, reason: str) -> ReferralRecord:
    row = Referral(patient_id=patient_id, department=department, reason=reason, status="PENDING")
    try: session.add(row); session.commit(); session.refresh(row)
    except Exception: session.rollback(); raise
    return _record(row)
def change_referral_record(session: Session, *, referral_id: int, patient_id: str, action: str, department: str | None = None, reason: str | None = None) -> ReferralRecord:
    row = session.get(Referral, referral_id)
    if row is None or row.patient_id != patient_id or row.status != "PENDING": raise StaleReferralError("Referral is not active or does not belong to this patient.")
    if action == "CANCEL": row.status = "CANCELLED"
    elif action == "UPDATE":
        if department is not None: row.department = department
        if reason is not None: row.reason = reason
    else: raise ValueError("Unknown referral change.")
    try: session.commit(); session.refresh(row)
    except Exception: session.rollback(); raise
    return _record(row)
def create_review_request_record(session: Session, *, patient_id: str, reason: str, department: str | None = None, referral_id: int | None = None) -> ReviewRequestRecord:
    row = ClinicalReviewRequestRecord(patient_id=patient_id, referral_id=referral_id, department=department, reason=reason, status="OPEN")
    try: session.add(row); session.commit(); session.refresh(row)
    except Exception: session.rollback(); raise
    return ReviewRequestRecord(row.review_request_id, row.patient_id, row.referral_id, row.department, row.reason, row.status, row.created_at)
