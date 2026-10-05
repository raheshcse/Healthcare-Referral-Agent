"""
AI output -> validated action proposal (Phase 2).

The AI recommendation is only text until this module turns it into an
explicit, validated ActionProposal. Nothing the model returns is trusted
blindly:

- only known actions and departments are accepted;
- identifier fields (patient_id, uuid, ...) are ignored and recorded;
- text containing an internal identifier (UUID) is rejected;
- malformed output produces errors and no proposal.

Validation here checks SHAPE. Whether the action may run is decided by
application validation afterwards (e.g. checking that the cited
evidence exists in the patient's record).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


ACTION_CREATE_REFERRAL = "CREATE_REFERRAL"
ACTION_NO_ACTION = "NO_ACTION"
SUPPORTED_ACTIONS = (ACTION_CREATE_REFERRAL, ACTION_NO_ACTION)

SUPPORTED_DEPARTMENTS = (
    "Cardiology",
    "Dermatology",
    "Endocrinology",
    "Gastroenterology",
    "Hematology",
    "Nephrology",
    "Neurology",
    "Obstetrics and Gynecology",
    "Oncology",
    "Ophthalmology",
    "Orthopedics",
    "Otolaryngology",
    "Psychiatry",
    "Pulmonology",
    "Rheumatology",
    "Urology",
)

_DEPARTMENT_ALIASES = {
    "cardiac": "Cardiology",
    "cardiologist": "Cardiology",
    "orthopaedics": "Orthopedics",
    "haematology": "Hematology",
    "ent": "Otolaryngology",
    "obgyn": "Obstetrics and Gynecology",
    "gynecology": "Obstetrics and Gynecology",
    "respiratory": "Pulmonology",
    "endocrine": "Endocrinology",
}

ALLOWED_FIELDS = {"recommendation", "action", "department", "reason", "evidence"}

# Any other field (e.g. patient_id, uuid, referral_id) is ignored and
# recorded in ProposalValidation.ignored_fields; it is never used.

MAX_RECOMMENDATION = 2000
MAX_REASON = 1000
MAX_EVIDENCE_ITEMS = 10
MAX_EVIDENCE_LENGTH = 200
MAX_RAW_OUTPUT = 20_000

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


@dataclass(frozen=True)
class ActionProposal:
    action: str
    recommendation: str
    department: str | None = None
    reason: str | None = None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(self.evidence)
        return data


@dataclass(frozen=True)
class ProposalValidation:
    proposal: ActionProposal | None
    errors: tuple[str, ...] = ()
    ignored_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return self.proposal is not None and not self.errors


def normalize_department(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = " ".join(value.split()).casefold()
    for suffix in (" department", " clinic", " service"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    for department in SUPPORTED_DEPARTMENTS:
        if department.casefold() == key:
            return department
    return _DEPARTMENT_ALIASES.get(key)


def parse_model_output(raw: Any) -> Any:
    """Parse model output into a Python object. Returns None if impossible."""

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or len(raw) > MAX_RAW_OUTPUT:
        return None

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None


def _text(value: Any) -> str | None:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def validate_proposal(raw: Any, *, requested_department: str | None = None) -> ProposalValidation:
    data = parse_model_output(raw)

    if not isinstance(data, dict):
        return ProposalValidation(None, ("AI output is not a JSON object.",))

    ignored = tuple(
        sorted(k for k in data if not isinstance(k, str) or k.casefold() not in ALLOWED_FIELDS)
    )
    fields = {k.casefold(): v for k, v in data.items() if isinstance(k, str) and k.casefold() in ALLOWED_FIELDS}

    errors: list[str] = []

    action_raw = fields.get("action")
    action = (
        action_raw.strip().upper().replace(" ", "_").replace("-", "_")
        if isinstance(action_raw, str)
        else None
    )
    if action not in SUPPORTED_ACTIONS:
        errors.append(f"Unsupported action {action_raw!r}.")

    recommendation = _text(fields.get("recommendation"))
    if recommendation is None:
        errors.append("Missing recommendation.")
    elif len(recommendation) > MAX_RECOMMENDATION:
        errors.append("Recommendation is too long.")

    department = reason = None
    evidence: tuple[str, ...] = ()

    if action == ACTION_CREATE_REFERRAL:
        department = normalize_department(fields.get("department"))
        if department is None:
            errors.append(f"Unsupported department {fields.get('department')!r}.")
        elif requested_department and department != requested_department:
            errors.append(
                f"Proposed department {department} is outside the requested scope ({requested_department})."
            )

        reason = _text(fields.get("reason"))
        if reason is None:
            errors.append("Missing referral reason.")
        elif len(reason) > MAX_REASON:
            errors.append("Referral reason is too long.")

        raw_evidence = fields.get("evidence", [])
        if raw_evidence is None:
            raw_evidence = []
        if not isinstance(raw_evidence, list) or not all(isinstance(e, str) for e in raw_evidence):
            errors.append("Evidence must be a list of text items.")
        elif len(raw_evidence) > MAX_EVIDENCE_ITEMS or any(len(e) > MAX_EVIDENCE_LENGTH for e in raw_evidence):
            errors.append("Too much evidence text.")
        else:
            evidence = tuple(t for t in (_text(e) for e in raw_evidence) if t)

    for label, value in (("recommendation", recommendation), ("reason", reason), *(("evidence", e) for e in evidence)):
        if value and _UUID.search(value):
            errors.append(f"The {label} contains an internal identifier, which the AI may not supply.")
            break

    if errors:
        return ProposalValidation(None, tuple(errors), ignored)

    return ProposalValidation(
        ActionProposal(
            action=action,
            recommendation=recommendation,
            department=department,
            reason=reason,
            evidence=evidence,
        ),
        (),
        ignored,
    )
