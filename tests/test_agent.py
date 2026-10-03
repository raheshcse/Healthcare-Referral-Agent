"""
Microsoft Agent Framework integration.

These tests drive the REAL Agent Framework function-invocation loop,
the REAL vsl-maf VSLFunctionMiddleware and the REAL tools. Only the
language model is replaced by a scripted chat client, so the tests are
deterministic and do not need a live OpenAI connection.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agent_framework import Agent, BaseChatClient, ChatResponse, Content, Message
from agent_framework._middleware import ChatMiddlewareLayer
from agent_framework._tools import FunctionInvocationLayer
from vsl_core.exceptions import AutomationDeniedException

from backend.agent_tools import AGENT_TOOLS
from backend.chat import ChatService
from backend.domain import WorkflowStatus
from backend.conversation import build_context_provider
from governance.maf_gates import GOVERNED_TOOL_MIDDLEWARE, referral_vsl_middleware
from tests.conftest import AISHA_ID, referral_count


def test_openai_configuration_fails_clearly_without_a_key(monkeypatch):
    """The backend never falls back to an undocumented local provider."""

    from backend.agent import OpenAIConfigurationError, validate_openai_configuration

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(OpenAIConfigurationError, match="OPENAI_API_KEY is not configured"):
        validate_openai_configuration()


class ScriptedChatClient(
    FunctionInvocationLayer,
    ChatMiddlewareLayer,
    BaseChatClient,
):
    """Stands in for OpenAI: emits a scripted tool call, then text."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__()
        self.script = list(script)
        self.requests: list[list[Message]] = []
        self.options: list[Any] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        async def respond() -> ChatResponse:
            self.requests.append(list(messages))
            self.options.append(dict(options or {}))
            step = self.script.pop(0) if self.script else "OK."
            if isinstance(step, dict):
                content = Content.from_function_call(
                    call_id=f"call-{len(self.requests)}",
                    name=step["name"],
                    arguments=step["arguments"],
                )
            else:
                content = step
            return ChatResponse(messages=[Message(role="assistant", contents=[content])])

        return respond()


def _agent(script: list[Any]) -> tuple[Agent, ScriptedChatClient]:
    client = ScriptedChatClient(script)
    agent = Agent(
        client=client,
        name="TestAgent",
        tools=AGENT_TOOLS,
        # Phase 3: the full production pre-tool governance (all tools) and
        # the per-turn conversation-state provider, as in backend.agent.
        middleware=list(GOVERNED_TOOL_MIDDLEWARE),
        context_providers=[build_context_provider()],
    )
    return agent, client


def _tool_results(client: ScriptedChatClient) -> list[Any]:
    return [
        content.result
        for message in client.requests[-1]
        for content in message.contents
        if content.type == "function_result"
    ]


def _referral_call(**arguments: str) -> dict:
    return {"name": "create_referral", "arguments": arguments}


# ------------------------------------------------------------
# End-to-end through Agent Framework
# ------------------------------------------------------------

async def test_agent_referral_end_to_end_without_llm_handling_uuid():
    agent, client = _agent(
        [
            _referral_call(
                patient_name="Aisha Wiegand",
                department="Cardiology",
                reason="Persistent chest pain on exertion.",
            ),
            "The referral was created.",
        ]
    )

    await agent.run("Refer Aisha Wiegand to cardiology for persistent chest pain on exertion.")

    assert referral_count() == 1

    results = _tool_results(client)
    assert len(results) == 1
    serialized = json.dumps(results[0], default=str)
    assert "REFERRAL_CREATED" in serialized
    # The internal UUID is never handed to the model.
    assert AISHA_ID not in serialized


def _last_denied_pre_node() -> str:
    """The PreNode that denied the most recent ledgered decision."""

    from governance.ledger import entries_for_decision, recent_decisions

    decision_id = recent_decisions(1)[0]["decision_id"]
    [denied] = [
        e.payload["pre_node"]
        for e in entries_for_decision(decision_id)
        if e.entry_type.value == "PRE_NODE" and e.payload.get("denied")
    ]
    return denied


async def test_vsl_maf_middleware_blocks_incomplete_intent_before_tool_runs():
    agent, client = _agent(
        [
            _referral_call(patient_name="Aisha Wiegand", department="Cardiology", reason=""),
            "Could not create the referral.",
        ]
    )

    await agent.run("Refer Aisha to cardiology.")

    assert referral_count() == 0
    [denied] = [
        content
        for message in client.requests[-1]
        for content in message.contents
        if content.type == "function_result"
    ]
    # The tool body never ran. Phase 3: the governed middleware returns a
    # structured denial to the model (instead of an opaque tool exception).
    assert '"tool_executed": false' in str(denied.result)
    # The rule that denied is recorded in the ledger, not handed to the model.
    assert "REFERRAL_INTENT_COMPLETE" not in str(denied.result)
    assert _last_denied_pre_node() == "REFERRAL_INTENT_COMPLETE"


async def test_llm_supplying_uuid_as_name_cannot_create_referral():
    agent, _ = _agent(
        [
            _referral_call(patient_name=AISHA_ID, department="Cardiology", reason="Chest pain."),
            "Done.",
        ]
    )

    await agent.run("Refer the patient to cardiology.")

    assert referral_count() == 0


async def test_repeated_tool_call_in_one_chat_turn_does_not_duplicate_referral():
    call = _referral_call(patient_name="Aisha Wiegand", department="Cardiology", reason="Chest pain.")
    service = ChatService(agent_factory=lambda: _agent([call, call, "Done."])[0])

    result = await service.chat("Refer Aisha Wiegand to cardiology for chest pain.")

    assert referral_count() == 1
    assert len(result.workflow_results) == 1


# ------------------------------------------------------------
# Middleware unit behaviour
# ------------------------------------------------------------

class _Fn:
    def __init__(self, name: str) -> None:
        self.name = name


class _Ctx:
    def __init__(self, name: str, arguments: dict) -> None:
        self.function = _Fn(name)
        self.arguments = arguments
        self.result = None
        self.metadata = {}


async def test_middleware_denies_and_never_calls_next_on_denial():
    """
    Phase 3: on DENY the governed middleware sets a structured result for
    the model and does not call the tool (it no longer raises; the
    pre-commitment guarantee - call_next never reached - is unchanged).
    """

    called = []

    async def call_next():
        called.append(True)

    context = _Ctx("create_referral", {"patient_name": "Aisha", "department": " ", "reason": "chest pain"})
    await referral_vsl_middleware.process(context, call_next)

    assert called == []
    assert '"tool_executed": false' in context.result
    assert _last_denied_pre_node() == "REFERRAL_INTENT_COMPLETE"

    with pytest.raises(AutomationDeniedException):  # the underlying gate still raises
        await referral_vsl_middleware._governance_gate(referral_vsl_middleware._prepare(context))


async def test_middleware_passes_complete_intent_and_other_tools():
    called = []

    async def call_next():
        called.append(True)

    await referral_vsl_middleware.process(
        _Ctx("create_referral", {"patient_name": "Aisha", "department": "Cardiology", "reason": "chest pain"}),
        call_next,
    )
    await referral_vsl_middleware.process(_Ctx("search_patient", {"name": "Aisha"}), call_next)

    assert called == [True, True]


# ------------------------------------------------------------
# Chat service: authoritative reply
# ------------------------------------------------------------

async def test_chat_reply_comes_from_workflow_not_llm_text():
    service = ChatService(
        agent_factory=lambda: _agent(
            [
                # Realistic, grounded inputs (the bug-fix guards reject an
                # unstated placeholder reason such as "x" before any workflow).
                _referral_call(patient_name="Nonexistent Person", department="Cardiology", reason="chest pain"),
                "Great news, the referral was created!",  # an LLM misreport
            ]
        )[0]
    )

    result = await service.chat("Refer Nonexistent Person to cardiology for chest pain.")

    assert referral_count() == 0
    assert result.workflow_results[0].status is WorkflowStatus.PATIENT_NOT_FOUND
    assert "not created" in result.reply
    assert result.agent_reply == "Great news, the referral was created!"
