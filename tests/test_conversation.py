"""
Phase 3: multi-turn conversational context.

The model is a scripted stand-in (OpenAI is not contacted in CI); these
tests prove the plumbing that makes the model context-aware:
  - one MAF AgentSession per conversation (history reaches the model),
  - structured ReferralContext (patient / department / reason / stage),
  - per-turn state injection into the model's instructions,
  - deterministic capture of answers, so known facts are never re-asked,
  - pronoun resolution against the established patient,
  - governed actions still decided by X-Verba, not by the conversation.
"""

from __future__ import annotations

import pytest

from backend.chat import ChatService
from backend.database.connection import SessionLocal
from backend.database.models import Referral
from tests.conftest import AISHA_ID, referral_count
from tests.test_agent import _agent, _referral_call
from tests.test_chat_intent import ToolAgentSpy, _conversation_agent

AISHA_FULL = "Aisha756 Melina208 Wiegand701"


def _call(tool, /, **arguments):
    return {"name": tool, "arguments": arguments}


class Harness:
    """A ChatService whose tool agent follows a script; records the client."""

    def __init__(self, script):
        self.holder = {}

        def factory():
            agent, self.holder["client"] = _agent(script)
            return agent

        self.service = ChatService(agent_factory=factory, conversation_agent_factory=_conversation_agent)
        self.conversation_id = None
        self.replies: list[str] = []

    async def say(self, message):
        result = await self.service.chat(message, self.conversation_id)
        self.conversation_id = result.conversation_id
        self.replies.append(result.reply)
        return result

    @property
    def client(self):
        return self.holder.get("client")


# ------------------------------------------------------------------
# The reference conversation from the brief
# ------------------------------------------------------------------

async def test_referral_collected_over_multiple_turns_then_confirmed():
    h = Harness(
        [
            # Turn 4: the model checks relevant data, then asks to confirm.
            _call("get_patient_conditions", patient_name="she"),
            "Thanks. I have the patient, department and referral reason. "
            "Her record shows essential hypertension. Shall I create the referral?",
            # Turn 5: confirmed -> the model proposes the governed action.
            _referral_call(patient_name="she", department="Cardiology",
                           reason="She has been experiencing chest pain"),
            "Done.",
        ]
    )

    r1 = await h.say("I want to refer a patient.")
    assert r1.reply == "Sure. Which patient would you like to refer?"
    assert r1.context["missing"] == ["patient", "department", "reason"]

    r2 = await h.say("Aisha Wiegand.")
    assert r2.reply == "Which department should the referral go to?"
    assert r2.context["patient"]["name"] == AISHA_FULL

    r3 = await h.say("Cardiology.")
    assert r3.reply == "What is the reason for the referral?"
    assert r3.context["department"] == "Cardiology"

    r4 = await h.say("She has been experiencing chest pain.")
    assert r4.context["reason"] == "She has been experiencing chest pain"
    assert r4.context["stage"] == "READY"
    assert "Shall I create the referral?" in r4.reply
    assert referral_count() == 0, "nothing is written before confirmation"

    r5 = await h.say("Yes, go ahead.")
    assert r5.workflow_results[0].status.value == "REFERRAL_CREATED"
    assert r5.workflow_results[0].governance.outcome.value == "ALLOW"
    assert r5.context["stage"] == "SUBMITTED"
    assert referral_count() == 1
    with SessionLocal() as session:
        row = session.query(Referral).one()
        assert row.patient_id == AISHA_ID
        assert row.department == "Cardiology"
        assert row.reason == "She has been experiencing chest pain"

    # Only two model calls per LLM turn; the slot-filling turns needed none.
    assert len(h.client.requests) == 4


async def test_history_and_state_reach_the_model():
    h = Harness(["Summary. Confirm?", "Noted."])

    await h.say("I want to refer a patient.")
    await h.say("Aisha Wiegand.")
    await h.say("Cardiology.")
    await h.say("She has chest pain.")

    first = h.client.requests[0]
    texts = [(m.role, m.text) for m in first]
    # The deterministic exchanges are part of the model's history.
    assert ("user", "Aisha Wiegand.") in texts
    assert ("assistant", "Which department should the referral go to?") in texts
    assert texts[-1] == ("user", "She has chest pain.")

    instructions = h.client.options[0]["instructions"]
    assert "CONVERSATION STATE" in instructions
    assert AISHA_FULL in instructions
    assert "department = Cardiology" in instructions
    assert "chest pain" in instructions
    assert "ask the staff member to confirm" in instructions

    # Next turn: MAF session memory carries the previous model exchange.
    await h.say("What is her latest blood pressure?")
    second = [(m.role, m.text) for m in h.client.requests[1]]
    assert ("assistant", "Summary. Confirm?") in second
    assert ("user", "She has chest pain.") in second


async def test_patient_and_department_in_one_message_asks_only_for_reason():
    spy = ToolAgentSpy([])
    service = ChatService(agent_factory=spy)

    result = await service.chat("Refer Aisha to cardiology.")

    assert result.reply == "What is the reason for the referral?"
    assert result.context["patient"]["name"] == AISHA_FULL
    assert result.context["department"] == "Cardiology"
    assert result.context["missing"] == ["reason"]
    assert spy.created == 0


async def test_known_information_is_never_asked_twice():
    h = Harness(["Please confirm.", "Done."])

    for message in ["I need to refer someone.", "Aisha Wiegand", "Cardiology", "She has chest pain"]:
        await h.say(message)

    questions = [r for r in h.replies if r.endswith("?") and r.startswith(("Sure", "Which", "What"))]
    assert questions == [
        "Sure. Which patient would you like to refer?",
        "Which department should the referral go to?",
        "What is the reason for the referral?",
    ]
    assert len(set(questions)) == len(questions)


async def test_follow_up_pronoun_resolves_to_established_patient():
    h = Harness(
        [
            _call("get_patient_information", patient_name="Aisha Wiegand"),
            "Here is her summary.",
            _call("get_patient_medications", patient_name="her"),
            "She takes lisinopril.",
        ]
    )

    await h.say("Show me Aisha Wiegand's details")
    result = await h.say("What medications is she on?")

    tool_results = [
        c.result for m in h.client.requests[-1] for c in m.contents if c.type == "function_result"
    ]
    assert "lisinopril 10 MG Oral Tablet" in str(tool_results[-1])
    assert AISHA_ID not in str(tool_results)
    assert result.context["patient"]["name"] == AISHA_FULL


async def test_pronoun_without_established_patient_is_not_guessed():
    h = Harness([_call("get_patient_medications", patient_name="she"), "Which patient do you mean?"])

    result = await h.say("What medications is she on?")

    [denied] = [c.result for m in h.client.requests[-1] for c in m.contents if c.type == "function_result"]
    assert '"tool_executed": false' in str(denied)
    assert result.reply == "Which patient do you mean?"


async def test_mentioning_a_symptom_does_not_start_a_referral():
    spy = ToolAgentSpy(["Chest pain can have many causes. Would you like to look up a patient?"])
    service = ChatService(agent_factory=spy)

    result = await service.chat("chest pain is common in older adults, isn't it?")

    assert result.context["intent"] is None
    assert result.workflow_results == []
    assert referral_count() == 0


async def test_greeting_inside_a_conversation_keeps_context():
    h = Harness([])

    await h.say("Refer Aisha to cardiology.")
    greeting = await h.say("thanks")
    assert greeting.context["patient"]["name"] == AISHA_FULL
    assert greeting.context["missing"] == ["reason"]

    result = await h.say("She has chest pain.")
    assert result.context["reason"] == "She has chest pain"
    assert result.context["stage"] == "READY"


async def test_conversations_are_isolated():
    service = ChatService(agent_factory=ToolAgentSpy([]), conversation_agent_factory=_conversation_agent)

    first = await service.chat("Refer Aisha to cardiology.")
    second = await service.chat("I want to refer a patient.")

    assert first.conversation_id != second.conversation_id
    assert second.context["patient"] is None
    assert second.reply == "Sure. Which patient would you like to refer?"


async def test_unresolvable_name_is_handled_by_the_workflow_not_re_asked():
    h = Harness(
        [
            _referral_call(patient_name="Zebulon Nobody", department="Cardiology", reason="chest pain"),
            "Done.",
        ]
    )

    result = await h.say("Refer Zebulon Nobody to cardiology for chest pain")

    assert result.workflow_results[0].status.value == "PATIENT_NOT_FOUND"
    assert referral_count() == 0


async def test_a_new_referral_after_submission_keeps_patient_but_not_old_details():
    h = Harness(
        [
            _referral_call(patient_name="Aisha Wiegand", department="Cardiology", reason="chest pain"),
            "Done.",
        ]
    )

    await h.say("Refer Aisha Wiegand to cardiology because of chest pain")
    result = await h.say("Also refer her to neurology.")

    assert result.reply == "What is the reason for the referral?"
    assert result.context["patient"]["name"] == AISHA_FULL
    assert result.context["department"] == "Neurology"
    assert result.context["reason"] is None


async def test_a_new_request_naming_an_unknown_patient_never_inherits_the_previous_one():
    # Regression (found by the browser E2E): after a referral for Aisha, a
    # request for a patient who does not resolve must not be confirmed for
    # Aisha. The workflow reports the real resolution outcome instead.
    h = Harness(
        [
            _referral_call(patient_name="Aisha Wiegand", department="Cardiology", reason="chest pain"),
            "Done.",
            _referral_call(patient_name="Zebulon Nobody", department="Neurology", reason="recurrent migraines"),
            "Done.",
            _referral_call(patient_name="Jordan", department="Dermatology", reason="a suspicious mole"),
            "Done.",
        ]
    )

    await h.say("Refer Aisha Wiegand to cardiology because of chest pain")
    assert referral_count() == 1

    unknown = await h.say("Refer Zebulon Nobody to Neurology for recurrent migraines.")
    assert unknown.context["patient"] is None
    assert unknown.context["stage"] != "CONFIRMED"
    assert unknown.workflow_results[0].status.value == "PATIENT_NOT_FOUND"

    ambiguous = await h.say("Refer Jordan to Dermatology for a suspicious mole.")
    assert ambiguous.context["patient"] is None
    assert ambiguous.workflow_results[0].status.value == "MULTIPLE_PATIENT_MATCHES"
    assert referral_count() == 1


# ------------------------------------------------------------------
# Client-readiness review regressions
# ------------------------------------------------------------------

async def _ready(h):
    await h.say("Refer Aisha Wiegand to cardiology.")
    result = await h.say("She has chest pain.")
    assert result.context["stage"] == "READY"
    return result


@pytest.mark.parametrize("answer", ["No.", "cancel", "never mind", "Stop"])
async def test_declining_a_prepared_referral_creates_nothing(answer):
    h = Harness([_referral_call(patient_name="she", department="Cardiology", reason="chest pain"), "Done."])
    await _ready(h)
    model_calls = len(h.client.requests) if h.client else 0

    result = await h.say(answer)

    assert result.reply == "Understood. I won't submit that referral. Nothing was created."
    assert result.context["intent"] is None
    assert referral_count() == 0
    # Handled deterministically: the model was not asked to act on "no".
    assert (len(h.client.requests) if h.client else 0) == model_calls


async def test_model_cannot_create_a_referral_that_was_not_confirmed():
    h = Harness([
        "All set. Shall I create the referral?",
        _referral_call(patient_name="she", department="Cardiology", reason="She has chest pain"),
        "Referral created!",
    ])
    await _ready(h)

    # Not a confirmation: the model nevertheless tries to create the referral.
    result = await h.say("hmm, let me think about the timing")

    assert referral_count() == 0
    assert result.workflow_results == []
    assert "Referral created" not in result.reply
    assert result.reply.startswith("No referral or change was made.")


async def test_a_new_patient_never_inherits_the_previous_reason():
    h = Harness([])
    await _ready(h)

    result = await h.say("Refer Jordan101 Smith202 to neurology")

    assert result.context["patient"]["name"] == "Jordan101 Smith202"
    assert result.context["department"] == "Neurology"
    assert result.context["reason"] is None
    assert result.reply == "What is the reason for the referral?"


async def test_changing_the_department_before_confirmation_is_honoured():
    h = Harness([
        "Shall I create the referral?",
        "Neurology then. Shall I create it?",
        _referral_call(patient_name="she", department="Neurology", reason="She has chest pain"),
        "Done.",
    ])
    await _ready(h)

    changed = await h.say("Actually, make it neurology.")
    assert changed.context["department"] == "Neurology"
    assert changed.context["stage"] == "READY"

    done = await h.say("Yes, go ahead.")
    assert done.workflow_results[0].status.value == "REFERRAL_CREATED"
    with SessionLocal() as session:
        assert session.query(Referral).one().department == "Neurology"


async def test_a_patient_name_is_not_taken_as_the_referral_reason():
    h = Harness([])
    await h.say("I want to refer a patient.")
    await h.say("Aisha Wiegand.")
    await h.say("Cardiology.")

    result = await h.say("Jordan101 Smith202")

    assert result.context["reason"] is None


async def test_model_text_cannot_claim_success_when_the_tool_failed(monkeypatch):
    from backend import agent_tools

    class Broken:
        async def run(self, request):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(agent_tools, "get_default_workflow", lambda: Broken())
    h = Harness([
        _referral_call(patient_name="Aisha Wiegand", department="Cardiology", reason="chest pain"),
        "Referral 7 created successfully.",
    ])

    result = await h.say("Refer Aisha Wiegand to cardiology because of chest pain")

    assert referral_count() == 0
    assert "created" not in result.reply.lower()
    assert result.reply.startswith("No referral or change was made because of a system error.")


async def test_unsupported_department_is_explained_and_the_reason_kept():
    h = Harness([])

    result = await h.say("Refer Aisha Wiegand to XYZ Department for chest pain")

    assert result.reply.startswith("“XYZ” is not a department I can refer to.")
    assert "Cardiology" in result.reply
    assert result.context["department"] is None
    assert result.context["reason"] == "chest pain"
    assert referral_count() == 0

    follow_up = await h.say("Cardiology")
    assert follow_up.context["department"] == "Cardiology"
    assert follow_up.context["stage"] == "READY" or follow_up.reply.endswith("?")


async def test_unsupported_department_given_as_an_answer_is_explained():
    h = Harness([])
    await h.say("I want to refer a patient.")
    await h.say("Aisha Wiegand")

    result = await h.say("Astrology")

    assert result.reply.startswith("“Astrology” is not a department I can refer to.")
    assert result.context["department"] is None


async def test_department_aliases_are_understood_in_conversation():
    h = Harness([])

    result = await h.say("Refer Aisha Wiegand to haematology")

    assert result.context["department"] == "Hematology"
    assert result.reply == "What is the reason for the referral?"
