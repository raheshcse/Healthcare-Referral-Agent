from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Patient(Base):
    __tablename__ = "patients"

    patient_id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False
    )

    date_of_birth: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True
    )

    gender: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )


class Condition(Base):
    __tablename__ = "conditions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    condition: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    onset: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )


class Medication(Base):
    __tablename__ = "medications"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    medication: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    status: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )


class Allergy(Base):
    __tablename__ = "allergies"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    allergy: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    type: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    value: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    date: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )


class Encounter(Base):
    __tablename__ = "encounters"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    type: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    status: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    start: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )


class Referral(Base):
    __tablename__ = "referrals"

    referral_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    department: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )



class ClinicalWorkflowRun(Base):
    """
    Phase 2: one clinical-review workflow run (business process state).

    This records clinical-review workflow state and its result snapshot.
    """

    __tablename__ = "clinical_workflow_runs"

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        unique=True,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    state: Mapped[str] = mapped_column(
        String(40),
        nullable=False
    )

    status: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True
    )

    requested_patient_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False
    )

    requested_department: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    referral_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True
    )

    # Full workflow result snapshot (JSON), including state transitions,
    # the validated action proposal and any ignored AI output fields.
    result_json: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )


class ClinicalReviewRequestRecord(Base):
    """
    Phase 3: a request for human clinical review (the human review queue).

    Created through the application clinical-review request workflow.
    Additive table: created by ensure_schema(); no existing table altered.
    """

    __tablename__ = "clinical_review_requests"

    review_request_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.patient_id"),
        nullable=False
    )

    referral_id: Mapped[int | None] = mapped_column(
        ForeignKey("referrals.referral_id"),
        nullable=True
    )

    department: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="OPEN"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
