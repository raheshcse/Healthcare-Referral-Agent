"""
Deterministic patient resolution.

This module is the ONLY place that turns a human-readable patient
name into an internal patient identifier. The LLM never performs
that transfer.

Rules:
    - Every whitespace-separated token of the supplied name must appear
      (case-insensitively) in the stored patient name. This lets staff
      type "Aisha Wiegand" for the Synthea name "Aisha756 Melina208
      Wiegand701".
    - An optional date of birth is a SEPARATE structured input that
      narrows the name matches to patients born on that date. It is never
      part of the name. (A date accidentally embedded in the name, e.g.
      "Ram Kumar, 02/10/2003", is split out defensively.)
    - Dates are read day-first (NZ convention): 02/10/2003 is 2 October
      2003. When a numeric date is genuinely ambiguous and no patient
      matches the day-first reading, the month-first reading is tried;
      both are never accepted at once.
    - Exactly one match   -> RESOLVED
    - Zero matches        -> NOT_FOUND
    - More than one match -> MULTIPLE_MATCHES (never guess)
    - The resolved identifier must be a canonical UUID, otherwise the
      record is treated as having an INVALID identifier.

All functions here are read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database.models import Patient
from backend.domain import PatientRecord


MAX_NAME_LENGTH = 200
MAX_CANDIDATES = 10

_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_PATIENT_ID = "INVALID_PATIENT_ID"


@dataclass(frozen=True)
class PatientResolution:
    status: ResolutionStatus
    message: str
    patient: PatientRecord | None = None
    candidates: tuple[PatientRecord, ...] = field(default_factory=tuple)
    more_candidates: bool = False


# ============================================================
# Identifier validation
# ============================================================


def normalize_patient_id(value: object) -> str | None:
    """
    Return the canonical (lower-case, hyphenated) form of a patient
    UUID, or ``None`` if the value is not a well-formed identifier.
    """

    if not isinstance(value, str):
        return None

    candidate = value.strip().lower()

    if _CANONICAL_UUID.fullmatch(candidate):
        return candidate

    return None


# ============================================================
# Date of birth (structured, separate from the name)
# ============================================================

_MONTHS = {
    name: index
    for index, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
         ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
         ("oct", "october"), ("nov", "november"), ("dec", "december")],
        start=1,
    )
    for name in names
}
_MONTH_WORD = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_DATE = r"(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})"
_NUMERIC_DATE = r"(?P<a>\d{1,2})[/.\-](?P<b>\d{1,2})[/.\-](?P<y>\d{4})"
_DAY_MONTH_WORD = rf"(?P<wd>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<wm>{_MONTH_WORD})\.?,?\s+(?P<wy>\d{{4}})"
_MONTH_WORD_DAY = rf"(?P<mm>{_MONTH_WORD})\.?\s+(?P<md>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<my>\d{{4}})"
DATE_PATTERN = re.compile(
    rf"\b(?:{_ISO_DATE}|{_NUMERIC_DATE}|{_DAY_MONTH_WORD}|{_MONTH_WORD_DAY})\b",
    re.I,
)
_DOB_LABEL = re.compile(r"\b(?:d\.?o\.?b\.?|date of birth|born(?: on)?|birth ?date)\s*[:\-]?\s*$", re.I)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates_from_match(match: re.Match) -> tuple[date, ...]:
    g = match.groupdict()
    if g.get("iy"):
        found = _safe_date(int(g["iy"]), int(g["im"]), int(g["id"]))
        return (found,) if found else ()
    if g.get("y"):
        a, b, year = int(g["a"]), int(g["b"]), int(g["y"])
        day_first = _safe_date(year, b, a)
        month_first = _safe_date(year, a, b)
        ordered = [d for d in (day_first, month_first) if d is not None]
        return tuple(dict.fromkeys(ordered))  # day-first preferred, no duplicates
    if g.get("wy"):
        found = _safe_date(int(g["wy"]), _MONTHS[g["wm"].casefold()], int(g["wd"]))
        return (found,) if found else ()
    if g.get("my"):
        found = _safe_date(int(g["my"]), _MONTHS[g["mm"].casefold()], int(g["md"]))
        return (found,) if found else ()
    return ()


def parse_date_of_birth(value: object) -> tuple[date, ...]:
    """
    Possible calendar dates for a typed date of birth, most likely first.

    "2003-10-02", "2 October 2003", "Oct 2, 2003" -> one date.
    "02/10/2003" -> (2 Oct 2003, 10 Feb 2003): day-first, then month-first.
    "25/12/2003" -> one date (only one reading is valid).
    Anything unrecognised (or a date in the future) -> ().
    """

    if isinstance(value, date):
        return (value,)
    if not isinstance(value, str) or not value.strip():
        return ()
    match = DATE_PATTERN.search(value.strip())
    if match is None:
        return ()
    today = date.today()
    return tuple(d for d in _dates_from_match(match) if d <= today)


def split_name_and_date_of_birth(text: object) -> tuple[str, str | None]:
    """
    Separate a typed patient reference into (name, date_of_birth text).

    "Ram Kumar, 02/10/2003" -> ("Ram Kumar", "02/10/2003")
    "Ram Kumar DOB 2 Oct 2003" -> ("Ram Kumar", "2 Oct 2003")
    "Ram Kumar" -> ("Ram Kumar", None)
    """

    if not isinstance(text, str):
        return "", None
    match = DATE_PATTERN.search(text)
    if match is None:
        return " ".join(text.split()).strip(" ,;"), None
    before = _DOB_LABEL.sub("", text[: match.start()])
    name = " ".join(f"{before} {text[match.end():]}".split()).strip(" ,;:-()")
    return name, match.group(0)


def format_date_of_birth(value: object) -> str:
    """Unambiguous display form: '2 October 2003'."""

    dates = parse_date_of_birth(value) if not isinstance(value, date) else (value,)
    if not dates:
        return str(value or "")
    d = dates[0]
    return f"{d.day} {d.strftime('%B %Y')}"


# ============================================================
# Search
# ============================================================


def _escape_like(token: str) -> str:
    return (
        token.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _to_record(patient: Patient) -> PatientRecord:
    return PatientRecord(
        patient_id=patient.patient_id,
        name=patient.name,
        date_of_birth=patient.date_of_birth,
        gender=patient.gender,
    )


def search_patients(
    session: Session,
    name: str,
    *,
    limit: int = MAX_CANDIDATES,
) -> tuple[list[PatientRecord], bool]:
    """
    Token search over patient names.

    Returns ``(matches, truncated)`` where ``truncated`` is True when
    more than ``limit`` patients matched.
    """

    tokens = name.split()

    if not tokens:
        return [], False

    statement = select(Patient)

    for token in tokens:
        statement = statement.where(
            Patient.name.ilike(f"%{_escape_like(token)}%", escape="\\")
        )

    statement = (
        statement
        .order_by(Patient.name, Patient.patient_id)
        .limit(limit + 1)
    )

    patients = session.scalars(statement).all()

    truncated = len(patients) > limit

    return [_to_record(p) for p in patients[:limit]], truncated


# ============================================================
# Resolution
# ============================================================


def _born_on(session: Session, name: str, dates: tuple[date, ...]) -> list[PatientRecord]:
    """Name matches born on the given date (no candidate limit: DOB narrows)."""

    statement = select(Patient).where(Patient.date_of_birth == dates[0].isoformat())
    for token in name.split():
        statement = statement.where(Patient.name.ilike(f"%{_escape_like(token)}%", escape="\\"))
    statement = statement.order_by(Patient.name, Patient.patient_id).limit(MAX_CANDIDATES + 1)
    return [_to_record(p) for p in session.scalars(statement).all()]


def resolve_patient_by_name(
    session: Session,
    name: object,
    date_of_birth: object = None,
) -> PatientResolution:
    """Resolve a name (optionally narrowed by date of birth) to exactly one patient."""

    if isinstance(name, str) and date_of_birth in (None, ""):
        # Defensive: never treat "Name, 02/10/2003" as a single name.
        name, date_of_birth = split_name_and_date_of_birth(name)

    if not isinstance(name, str) or not name.strip():
        return PatientResolution(
            status=ResolutionStatus.INVALID_INPUT,
            message="A patient name is required.",
        )

    if len(name) > MAX_NAME_LENGTH:
        return PatientResolution(
            status=ResolutionStatus.INVALID_INPUT,
            message="The patient name is too long.",
        )

    name = name.strip()
    dates: tuple[date, ...] = ()
    if date_of_birth not in (None, ""):
        dates = parse_date_of_birth(date_of_birth)
        if not dates:
            return PatientResolution(
                status=ResolutionStatus.INVALID_INPUT,
                message=(
                    f"The date of birth '{date_of_birth}' was not recognised. "
                    "Please use DD/MM/YYYY, for example 02/10/2003."
                ),
            )

    if dates:
        # Day-first reading first; the alternative reading only if the
        # first matches nobody. Never both.
        matches: list[PatientRecord] = []
        used = dates[0]
        for candidate in dates:
            matches = _born_on(session, name, (candidate,))
            used = candidate
            if matches:
                break
        truncated = len(matches) > MAX_CANDIDATES
        matches = matches[:MAX_CANDIDATES]
        described = f"'{name}' with date of birth {format_date_of_birth(used)}"
    else:
        matches, truncated = search_patients(session, name)
        described = f"'{name}'"

    if not matches:
        return PatientResolution(
            status=ResolutionStatus.NOT_FOUND,
            message=f"No patient matches {described}.",
        )

    if len(matches) > 1:
        count = f"More than {len(matches)}" if truncated else str(len(matches))
        hint = (
            "Please clarify which patient you mean (for example with the full name)."
            if dates else
            "Please clarify which patient you mean (for example with the full name or date of birth)."
        )
        return PatientResolution(
            status=ResolutionStatus.MULTIPLE_MATCHES,
            message=f"{count} patients match {described}. {hint}",
            candidates=tuple(matches),
            more_candidates=truncated,
        )

    patient = matches[0]

    if normalize_patient_id(patient.patient_id) is None:
        return PatientResolution(
            status=ResolutionStatus.INVALID_PATIENT_ID,
            message=(
                "The matching patient record has an invalid internal "
                "identifier. The request cannot proceed."
            ),
        )

    return PatientResolution(
        status=ResolutionStatus.RESOLVED,
        message="Exactly one patient matched.",
        patient=patient,
    )


def resolve_patient_by_id(session: Session, patient_id: object) -> PatientResolution:
    """Resolve a canonical internal identifier and confirm the patient exists."""
    normalized = normalize_patient_id(patient_id)
    if normalized is None:
        return PatientResolution(ResolutionStatus.INVALID_PATIENT_ID, "The patient identifier is not valid.")
    patient = session.get(Patient, normalized)
    if patient is None:
        return PatientResolution(ResolutionStatus.NOT_FOUND, "No patient matches that identifier.")
    return PatientResolution(ResolutionStatus.RESOLVED, "Patient resolved.", patient=_to_record(patient))
