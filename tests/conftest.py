"""
Shared test fixtures.

The suite never touches data/healthcare.db. The environment override is
set BEFORE any application module is imported so the engine binds to a
temporary database file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="healthcare-referral-tests-"))
os.environ["HEALTHCARE_REFERRAL_DATABASE_URL"] = f"sqlite:///{(_TEST_DIR / 'test.db').as_posix()}"

import pytest  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from backend.database.connection import SessionLocal, engine  # noqa: E402
from backend.database.models import (  # noqa: E402
    Condition,
    Encounter,
    Medication,
    Observation,
    Patient,
    Referral,
)
from backend.database.schema import ensure_schema  # noqa: E402


AISHA_ID = "8f998bfd-bcee-9bc0-e435-7c50e571a851"
AISHA_NAME = "Aisha756 Melina208 Wiegand701"

JORDAN_A_ID = "11111111-2222-3333-4444-555555555555"
JORDAN_B_ID = "66666666-7777-8888-9999-000000000000"

LEGACY_BAD_ID = "legacy-record-0001"
LEGACY_NAME = "Legacy999 Record888"

NONEXISTENT_ID = "00000000-0000-0000-0000-000000000000"


def _seed() -> None:
    ensure_schema(engine)
    with SessionLocal() as session:
        if session.get(Patient, AISHA_ID) is not None:
            return
        session.add_all(
            [
                Patient(patient_id=AISHA_ID, name=AISHA_NAME, date_of_birth="1991-10-19", gender="female"),
                Patient(patient_id=JORDAN_A_ID, name="Jordan101 Smith202", date_of_birth="1980-01-01", gender="male"),
                Patient(patient_id=JORDAN_B_ID, name="Jordan303 Smithers404", date_of_birth="1975-05-05", gender="male"),
                Patient(patient_id=LEGACY_BAD_ID, name=LEGACY_NAME, date_of_birth="1950-02-02", gender="female"),
                Condition(patient_id=AISHA_ID, condition="Essential hypertension", status="active", onset="2019-01-01"),
                # Phase 2 clinical context (additive; existing assertions unaffected)
                Medication(patient_id=AISHA_ID, medication="lisinopril 10 MG Oral Tablet", status="active"),
                Observation(patient_id=AISHA_ID, type="Systolic Blood Pressure", value="152 mm[Hg]", date="2026-01-03"),
                Encounter(patient_id=AISHA_ID, type="General examination of patient (procedure)", status="finished", start="2026-01-03"),
            ]
        )
        session.commit()


_seed()


def referral_count() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(Referral)) or 0


@pytest.fixture(autouse=True)
def clean_referrals():
    from backend.database.models import ClinicalWorkflowRun

    with SessionLocal() as session:
        session.query(Referral).delete()
        session.query(ClinicalWorkflowRun).delete()
        session.commit()
    yield
