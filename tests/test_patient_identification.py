"""
Structured patient identification in conversation.

The model is a scripted stand-in for OpenAI. These tests prove that:
  - patient identity is kept as structured fields (name, date of birth,
    patient ID) and never merged into one string;
  - an ambiguous or unknown patient gets a clear, deterministic reply that
    lists candidates by name and date of birth (never IDs);
  - a follow-up "Name, DD/MM/YYYY" resolves the patient and the earlier
    request continues with clean, separate tool arguments;
  - the model cannot narrow a search with a date the staff member never gave;
  - tool failures are reported as failures, never as model-written success.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import agent_tools
from backend.app import app
from backend.database.connection import SessionLocal
from backend.database.models import Condition, Patient, Referral
from backend.patient_resolution import (
    ResolutionStatus,
    format_date_of_birth,
    parse_date_of_birth,
    resolve_patient_by_name,
    split_name_and_date_of_birth,
)
from governance.ledger import entries_for_decision
from tests.test_chat_intent import _ledger_size, _new_decisions
from tests.test_conversation import Harness

RAM_2003 = "aaaaaaaa-0000-4000-8000-000000000001"  # Ram Kumar, 2 October 2003
RAM_1980 = "aaaaaaaa-0000-4000-8000-000000000002"  # Ram Kumar, 15 March 1980
RAM_SINGH = "aaaaaaaa-0000-4000-8000-000000000003"  # Ram Singh, 7 July 1975
MAY_FEB = "aaaaaaaa-0000-4000-8000-000000000004"  # Mayank Feb, 10 February 2003 (month-first reading)
IDS = (RAM_2003, RAM_1980, RAM_SINGH, MAY_FEB)


@pytest.fixture(autouse=True)
def ram_patients():
    with SessionLocal() as session:
        session.add_all([
            Patient(patient_id=RAM_2003, name="Ram Kumar", date_of_birth="2003-10-02", gender="male"),
            Patient(patient_id=RAM_1980, name="Ram Kumar", date_of_birth="1980-03-15", gender="male"),
            Patient(patient_id=RAM_SINGH, name="Ram Singh", date_of_birth="1975-07-07", gender="male"),
            Patient(patient_id=MAY_FEB, name="Mayank Feb", date_of_birth="2003-02-10", gender="male"),
            Condition(patient_id=RAM_2003, condition="Childhood asthma", status="active", onset="2010-05-01"),
            Condition(patient_id=RAM_1980, condition="Type 2 diabetes", status="active", onset="2015-01-01"),
        ])
        session.commit()
    yield
    with SessionLocal() as session:
        session.query(Referral).filter(Referral.patient_id.in_(IDS)).delete(synchronize_session=False)
        session.query(Condition).filter(Condition.patient_id.in_(IDS)).delete(synchronize_session=False)
        session.query(Patient).filter(Patient.patient_id.in_(IDS)).delete(synchronize_session=False)
        session.commit()


def _call(tool, /, **arguments):
    return {"name": tool, "arguments": arguments}


def _function_calls(client, request_index=-1):
    return [c for m in client.requests[request_index] for c in m.contents if c.type == "function_call"]


def _function_results(client, request_index=-1):
    return [c.result for m in client.requests[request_index] for c in m.contents if c.type == "function_result"]


# ------------------------------------------------------------------
# Deterministic parsing and resolution (no model)
# ------------------------------------------------------------------

def test_name_and_date_of_birth_are_split_never_merged():
    assert split_name_and_date_of_birth("Ram Kumar, 02/10/2003") == ("Ram Kumar", "02/10/2003")
    assert split_name_and_date_of_birth("Ram Kumar DOB 2 Oct 2003") == ("Ram Kumar", "2 Oct 2003")
    assert split_name_and_date_of_birth("Ram Kumar") == ("Ram Kumar", None)


def test_dates_are_read_day_first_with_an_unambiguous_display():
    assert parse_date_of_birth("02/10/2003")[0] == date(2003, 10, 2)
    assert parse_date_of_birth("25/12/2003") == (date(2003, 12, 25),)
    assert parse_date_of_birth("2003-10-02") == (date(2003, 10, 2),)
    assert parse_date_of_birth("not a date") == ()
    assert format_date_of_birth("2003-10-02") == "2 October 2003"


def test_resolution_uses_the_date_of_birth_as_a_separate_field():
    with SessionLocal() as session:
        ambiguous = resolve_patient_by_name(session, "Ram Kumar")
        exact = resolve_patient_by_name(session, "Ram Kumar", "02/10/2003")
        merged = resolve_patient_by_name(session, "Ram Kumar, 02/10/2003")  # defensive split
        wrong_dob = resolve_patient_by_name(session, "Ram Kumar", "05/05/1999")
        bad_dob = resolve_patient_by_name(session, "Ram Kumar", "31/02/2003")

    assert ambiguous.status is ResolutionStatus.MULTIPLE_MATCHES
    assert exact.status is ResolutionStatus.RESOLVED and exact.patient.patient_id == RAM_2003
    assert merged.status is ResolutionStatus.RESOLVED and merged.patient.patient_id == RAM_2003
    assert wrong_dob.status is ResolutionStatus.NOT_FOUND
    assert "5 May 1999" in wrong_dob.message
    assert bad_dob.status is ResolutionStatus.INVALID_INPUT


def test_month_first_reading_is_only_a_fallback():
    with SessionLocal() as session:
        # 10/02/2003 day-first = 10 Feb 2003 -> Mayank Feb.
        assert resolve_patient_by_name(session, "Mayank", "10/02/2003").patient.patient_id == MAY_FEB
        # 02/10/2003 day-first = 2 Oct 2003 (nobody named Mayank) -> month-first 10 Feb 2003.
        assert resolve_patient_by_name(session, "Mayank", "02/10/2003").patient.patient_id == MAY_FEB


# ------------------------------------------------------------------
# The reference conversation
# ------------------------------------------------------------------

async def test_ambiguous_name_then_name_and_date_of_birth_retrieves_the_right_patient():
    h = Harness([
        # Turn 1: the model looks the patient up by the name given.
        _call("get_patient_information", patient_name="Ram"),
        "Which Ram?",
        # Turn 2: after clarification, the pending request is completed.
        _call("get_patient_information", patient_name="Ram Kumar", date_of_birth="2003-10-02"),
        "Ram Kumar (born 2 October 2003) has childhood asthma.",
    ])
    ledger_before = _ledger_size()

    first = await h.say("show me patient information for Ram")

    # A clear, deterministic clarification listing name + date of birth only.
    assert first.reply.startswith("More than one patient matches Ram:")
    assert "- Ram Kumar, born 2 October 2003" in first.reply
    assert "- Ram Kumar, born 15 March 1980" in first.reply
    assert "- Ram Singh, born 7 July 1975" in first.reply
    assert "“Ram Kumar, " in first.reply
    assert RAM_2003 not in first.reply
    assert first.context["patient"] is None
    assert first.context["patient_query"] == {"name": "Ram", "date_of_birth": None, "status": "MULTIPLE_MATCHES"}
    [denied] = _new_decisions(ledger_before)
    assert denied["tool"] == "get_patient_information" and denied["decision"] == "DENY"

    ledger_before = _ledger_size()
    second = await h.say("Ram Kumar, 02/10/2003")

    # Structured patient state: name and DOB are separate fields.
    assert second.context["patient"] == {"name": "Ram Kumar", "date_of_birth": "2003-10-02"}
    assert second.context["patient_query"] is None

    # The model was told to complete the earlier request, with clean arguments.
    instructions = h.client.options[-2]["instructions"]
    assert "PENDING REQUEST" in instructions and "show me patient information for Ram" in instructions
    assert "patient_name='Ram Kumar', date_of_birth='2003-10-02'" in instructions
    call = _function_calls(h.client)[-1]  # history also holds turn 1's call
    assert call.parse_arguments() == {"patient_name": "Ram Kumar", "date_of_birth": "2003-10-02"}

    # Governed (ALLOW) and the right patient's data came back, without an ID.
    [allowed] = _new_decisions(ledger_before)
    assert allowed["decision"] == "ALLOW"
    monitor = entries_for_decision(_latest_decision_id())[0]
    assert monitor.payload["patient_id"] == RAM_2003
    result = _function_results(h.client)[-1]
    assert "Childhood asthma" in str(result) and "Type 2 diabetes" not in str(result)
    assert RAM_2003 not in str(result)
    assert second.reply == "Ram Kumar (born 2 October 2003) has childhood asthma."


def _latest_decision_id():
    from governance.ledger import recent_decisions

    return recent_decisions(1)[0]["decision_id"]


async def test_follow_ups_use_the_identified_patient_not_the_ambiguous_name():
    h = Harness([
        _call("get_patient_conditions", patient_name="Ram Kumar", date_of_birth="02/10/2003"),
        "Asthma.",
        _call("get_patient_conditions", patient_name="Ram Kumar"),  # no DOB this time
        "Still asthma.",
        _call("get_patient_conditions", patient_name="he"),
        "Asthma again.",
    ])

    await h.say("What conditions does Ram Kumar, 02/10/2003 have?")
    again = await h.say("And Ram Kumar's conditions once more?")
    pronoun = await h.say("What conditions does he have?")

    for index in (1, 3, 5):
        assert "Childhood asthma" in str(_function_results(h.client, index)[-1])
    assert again.context["patient"]["date_of_birth"] == "2003-10-02"
    assert pronoun.context["patient"]["name"] == "Ram Kumar"


async def test_a_name_and_date_merged_by_the_model_are_still_resolved_correctly():
    h = Harness([
        _call("get_patient_information", patient_name="Ram Kumar, 02/10/2003"),
        "Done.",
    ])

    await h.say("show me patient information for Ram Kumar, 02/10/2003")

    assert "Childhood asthma" in str(_function_results(h.client))


async def test_the_model_cannot_narrow_a_search_with_an_invented_date_of_birth():
    h = Harness([
        _call("get_patient_information", patient_name="Ram Kumar", date_of_birth="2003-10-02"),
        "Here you go.",
    ])

    result = await h.say("show me patient information for Ram Kumar")

    # The staff member never gave a date, so it is ignored: still ambiguous.
    assert result.reply.startswith("More than one patient matches Ram Kumar:")
    assert "Childhood asthma" not in str(_function_results(h.client)[-1])


# ------------------------------------------------------------------
# Not found, wrong details, errors
# ------------------------------------------------------------------

async def test_patient_not_found_is_explained_and_the_follow_up_is_checked_again():
    h = Harness([_call("get_patient_information", patient_name="Zed Nobody"), "Hmm."])

    first = await h.say("show me patient information for Zed Nobody")
    assert first.reply == (
        "I couldn't find a patient named Zed Nobody. Please check the spelling, or give the patient's "
        "full name and date of birth (DD/MM/YYYY). No patient information was retrieved."
    )
    assert first.context["patient_query"]["status"] == "NOT_FOUND"

    calls_before = len(h.client.requests)
    second = await h.say("Zed Nobody, 01/01/2000")

    assert second.reply.startswith("I couldn't find a patient named Zed Nobody with date of birth 1 January 2000.")
    assert len(h.client.requests) == calls_before  # answered deterministically


async def test_name_with_a_non_matching_date_of_birth_is_not_found():
    h = Harness([_call("get_patient_information", patient_name="Ram"), "?"])
    await h.say("show me patient information for Ram")

    result = await h.say("Ram Kumar, 05/05/1999")

    assert result.reply.startswith("I couldn't find a patient named Ram Kumar with date of birth 5 May 1999.")
    assert result.context["patient"] is None


async def test_an_unreadable_date_of_birth_is_explained():
    h = Harness([_call("get_patient_information", patient_name="Ram"), "?"])
    await h.say("show me patient information for Ram")

    result = await h.say("Ram Kumar, 31/02/2003")

    # 31/02/2003 is not a real date: the staff member is told how to give it.
    assert result.reply == "The date of birth '31/02/2003' was not recognised. Please use DD/MM/YYYY, for example 02/10/2003."
    assert result.context["patient"] is None


async def test_a_new_request_clears_the_pending_lookup():
    h = Harness([_call("get_patient_information", patient_name="Zed Nobody"), "Hmm."])
    await h.say("show me patient information for Zed Nobody")

    result = await h.say("Refer Aisha Wiegand to cardiology.")

    assert result.context["patient_query"] is None
    assert result.reply == "What is the reason for the referral?"


async def test_tool_failure_is_reported_as_a_failure(monkeypatch):
    def broken(session, patient_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(agent_tools, "get_patient", broken)
    h = Harness([
        _call("get_patient_information", patient_name="Ram Kumar", date_of_birth="02/10/2003"),
        "Ram Kumar is perfectly healthy.",
    ])

    result = await h.say("show me patient information for Ram Kumar, 02/10/2003")

    assert result.reply.startswith("I couldn't retrieve that patient information because of a system error.")
    assert "healthy" not in result.reply


# ------------------------------------------------------------------
# Referrals and the API use the same structured identity
# ------------------------------------------------------------------

async def test_referral_patient_can_be_given_as_name_and_date_of_birth():
    h = Harness([
        "Shall I create the referral?",
        _call("create_referral", patient_name="he", department="Neurology", reason="He has migraines"),
        "Done.",
    ])

    await h.say("I want to refer a patient.")
    patient = await h.say("Ram Kumar, 02/10/2003")
    assert patient.context["patient"] == {"name": "Ram Kumar", "date_of_birth": "2003-10-02"}
    assert patient.reply == "Which department should the referral go to?"

    await h.say("Neurology")
    await h.say("He has migraines.")
    done = await h.say("Yes, go ahead.")

    assert done.workflow_results[0].status.value == "REFERRAL_CREATED"
    with SessionLocal() as session:
        assert session.query(Referral).filter(Referral.patient_id == RAM_2003).count() == 1


def test_referral_api_accepts_a_separate_date_of_birth():
    with TestClient(app) as client:
        ambiguous = client.post("/referrals", json={
            "patient_name": "Ram Kumar", "department": "Cardiology", "reason": "Chest pain",
        })
        exact = client.post("/referrals", json={
            "patient_name": "Ram Kumar", "date_of_birth": "02/10/2003",
            "department": "Cardiology", "reason": "Chest pain",
        })

    assert ambiguous.status_code == 409
    assert {c["date_of_birth"] for c in ambiguous.json()["candidates"]} == {"2003-10-02", "1980-03-15"}
    assert exact.status_code == 201
    assert exact.json()["patient_id"] == RAM_2003
    assert json.loads(exact.content)["referral"]["department"] == "Cardiology"
