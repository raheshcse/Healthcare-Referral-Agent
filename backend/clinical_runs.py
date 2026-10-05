"""
Persistence for clinical-review workflow runs (business process state).

Stores a JSON snapshot of each run in ``clinical_workflow_runs`` plus a
few indexed columns for listing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.database.connection import SessionLocal
from backend.database.models import ClinicalWorkflowRun


class DuplicateIdempotencyKey(Exception):
    """A run with this idempotency key already exists."""


class ClinicalRunStore:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def create(self, snapshot: dict[str, Any], idempotency_key: str | None) -> None:
        now = datetime.utcnow()
        with self._session_factory() as session:
            session.add(
                ClinicalWorkflowRun(
                    workflow_id=snapshot["workflow_id"],
                    idempotency_key=idempotency_key,
                    created_at=now,
                    updated_at=now,
                    state=snapshot["state"],
                    status=snapshot.get("status"),
                    requested_patient_name=snapshot["request"]["patient_name"][:200],
                    requested_department=snapshot["request"].get("department"),
                    result_json=json.dumps(snapshot, default=str),
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise DuplicateIdempotencyKey(idempotency_key) from exc

    def save(self, snapshot: dict[str, Any]) -> None:
        with self._session_factory() as session:
            run = session.get(ClinicalWorkflowRun, snapshot["workflow_id"])
            if run is None:
                return
            run.updated_at = datetime.utcnow()
            run.state = snapshot["state"]
            run.status = snapshot.get("status")
            run.referral_id = (snapshot.get("referral") or {}).get("referral_id")
            run.result_json = json.dumps(snapshot, default=str)
            session.commit()

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            run = session.get(ClinicalWorkflowRun, workflow_id)
            return json.loads(run.result_json) if run else None

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            run = session.scalars(
                select(ClinicalWorkflowRun).where(ClinicalWorkflowRun.idempotency_key == key)
            ).first()
            return json.loads(run.result_json) if run else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            runs = session.scalars(
                select(ClinicalWorkflowRun)
                .order_by(ClinicalWorkflowRun.created_at.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "workflow_id": run.workflow_id,
                    "created_at": run.created_at,
                    "state": run.state,
                    "status": run.status,
                    "patient_name": run.requested_patient_name,
                    "department": run.requested_department,
                    "referral_id": run.referral_id,
                }
                for run in runs
            ]
