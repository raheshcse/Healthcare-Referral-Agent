"""
Phase 2: governed clinical-review workflow.

AI proposes -> workflow validates -> X-Verba governs -> only ALLOW writes
-> ledger records the evidence.
"""

from __future__ import annotations

import json

import pytest

from backend import actions
from backend.analysis import AnalysisUnavailableError
from backend.clinical_workflow import (
    ClinicalReviewRequest,
    ClinicalReviewWorkflow,
    ClinicalWorkflowStatus,
    WorkflowState,
)
from backend.database.connection import SessionLocal
from backend.database.models import Referral
from backend.workflow import ReferralWorkflow
from governance import gates
from governance.gates import governed_referral
from governance.ledger import entries_for_decision, ledger
from tests.conftest import (
    AI_REFERRAL_ALLOW_SHAPE,
    AI_REFERRAL_PRE_NODES,
    AISHA_ID,
    JORDAN_A_ID,
    REFERRAL_ALLOW_SHAPE,
    assert_causal_chain,
    pre_node_names,
    referral_count,
)

GROUNDED = {
    "recommendation": "Hypertension with raised systolic pressure on treatment; cardiology review is reasonable.",
    "action": "CREATE_REFERRAL",
    "department": "Cardiology",
    "reason": "Essential hypertension with raised systolic blood pressure despite treatment.",
    "evidence": ["Essential hypertension", "Systolic Blood Pressure", "lisinopril 10 MG Oral Tablet"],
}

HAPPY_PATH = [
    "REQUESTED",
    "PATIENT_RESOLVED",
    "DATA_RETRIEVED",
    "ANALYSIS_COMPLETED",
    "ACTION_PROPOSED",
    "GOVERNANCE_CHECK",
    "ACTION_EXECUTED",
    "COMPLETED",
]


class FakeAnalyzer:
    """Stands in for the configured OpenAI model. Records its context."""

    def __init__(self, output=None, raises=None):
        self.output = output
        self.raises = raises
        self.calls: list[dict] = []

    async def analyze(self, context, *, department, question):
        self.calls.append({"context": context, "department": department, "question": question})
        if self.raises:
            raise self.raises
        return self.output


class Recorder:
    """Records governance and writer calls in order."""

    def __init__(self):
        self.events: list[str] = []
        self.decisions = []

    async def governance(self, candidate):
        self.events.append("governance")
        return await governed_referral(candidate)

    def writer(self, session, decision):
        self.events.append("write")
        self.decisions.append(decision)
        return actions.create_referral_record(session, decision)


def _workflow(output=None, raises=None, recorder=None):
    recorder = recorder or Recorder()
    analyzer = FakeAnalyzer(output, raises)
    workflow = ClinicalReviewWorkflow(
        analyzer=analyzer,
        referral_workflow=ReferralWorkflow(governance=recorder.governance, writer=recorder.writer),
    )
    return workflow, analyzer, recorder


def _states(result):
    return [t["state"] for t in result.transitions]


# ------------------------------------------------------------------
# 1. Valid patient + valid proposal + ALLOW
# ------------------------------------------------------------------

async def test_valid_review_creates_referral_only_after_allow():
    workflow, analyzer, recorder = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand", department="Cardiology"))

    assert result.status == "REFERRAL_CREATED"
    assert result.state == "COMPLETED"
    assert result.success is True
    assert _states(result) == HAPPY_PATH
    assert result.governance["decision"] == "ALLOW"
    assert recorder.events == ["governance", "write"]
    assert recorder.decisions[0].outcome.value == "ALLOW"
    assert referral_count() == 1

    with SessionLocal() as session:
        row = session.get(Referral, result.referral["referral_id"])
        assert row.patient_id == AISHA_ID
        assert row.department == "Cardiology"
        assert row.governance_decision_id == result.governance["decision_id"]


async def test_clinical_context_uses_real_record_and_hides_uuid():
    workflow, analyzer, _ = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    context = analyzer.calls[0]["context"]
    assert AISHA_ID not in json.dumps(context)
    assert context["patient"]["name"] == "Aisha756 Melina208 Wiegand701"
    assert context["conditions"][0]["condition"] == "Essential hypertension"
    assert context["medications"][0]["medication"] == "lisinopril 10 MG Oral Tablet"
    assert context["recent_observations"][0]["type"] == "Systolic Blood Pressure"
    assert result.context_summary["conditions"] == 1


async def test_ledger_evidence_and_caused_by_for_ai_proposal():
    workflow, _, _ = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand", department="Cardiology"))
    entries = entries_for_decision(result.governance["decision_id"])
    # Phase 3: REQUEST_VALID -> TARGET_VALID -> AI_PROPOSAL_GROUNDED -> NOT_DUPLICATE
    assert [e.entry_type.value for e in entries] == AI_REFERRAL_ALLOW_SHAPE
    assert pre_node_names(entries) == AI_REFERRAL_PRE_NODES
    assert_causal_chain(entries)
    monitor, pre_request, verify_request = entries[0], entries[1], entries[2]
    pre_grounding, verify_grounding = entries[5], entries[6]
    verify_final = entries[-1]

    assert monitor.payload["origin"] == "AI_PROPOSAL"
    assert monitor.payload["workflow_id"] == result.workflow_id
    assert monitor.payload["patient_id"] == AISHA_ID
    assert pre_request.payload["pre_node"] == "REFERRAL_REQUEST_VALID"
    assert pre_request.caused_by == monitor.entry_id
    assert verify_request.caused_by == pre_request.entry_id
    assert verify_request.payload["outcome"] == "passed"
    assert pre_grounding.payload["pre_node"] == "AI_PROPOSAL_GROUNDED"
    assert pre_grounding.payload["evidence_cited"] == 3
    assert pre_grounding.payload["evidence_supported"] == 3
    assert pre_grounding.caused_by == monitor.entry_id
    assert verify_grounding.caused_by == pre_grounding.entry_id
    assert verify_grounding.payload["outcome"] == "passed"
    assert verify_final.payload["outcome"] == "approved"

    # Every PRE_NODE has its VERIFICATION (audit check 3) and the chain is intact.
    assert "pre_node_has_verification" not in ledger.audit().checks_failed
    assert ledger.verify_integrity() is True


# ------------------------------------------------------------------
# 2-3. Patient resolution failures stop the workflow
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, status",
    [
        ("Nonexistent Person", ClinicalWorkflowStatus.PATIENT_NOT_FOUND),
        ("Jordan", ClinicalWorkflowStatus.MULTIPLE_PATIENT_MATCHES),
        ("Legacy999", ClinicalWorkflowStatus.INVALID_PATIENT_ID),
        ("   ", ClinicalWorkflowStatus.INVALID_REQUEST),
    ],
)
async def test_resolution_failures_stop_before_analysis(name, status):
    workflow, analyzer, recorder = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest(name))

    assert result.status == status.value
    assert result.state == "FAILED"
    assert analyzer.calls == []
    assert recorder.events == []
    assert referral_count() == 0


async def test_ambiguous_patient_returns_candidates_without_uuids():
    workflow, _, _ = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest("Jordan"))

    assert len(result.candidates) == 2
    assert JORDAN_A_ID not in json.dumps(result.candidates)


# ------------------------------------------------------------------
# 4. Governance DENY -> REVIEW_REQUIRED, no write
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "evidence",
    [
        ["Congestive heart failure"],      # not in the record (hallucinated)
        [],                                # nothing cited
        ["Essential hypertension", "Atrial fibrillation"],  # partly ungrounded
    ],
)
async def test_ungrounded_proposal_is_denied_and_routed_to_review(evidence):
    workflow, _, recorder = _workflow(dict(GROUNDED, evidence=evidence))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "GOVERNANCE_DENIED"
    assert result.state == "REVIEW_REQUIRED"
    assert result.governance["decision"] == "DENY"
    assert result.governance["pre_node"] == "AI_PROPOSAL_GROUNDED"
    assert recorder.events == ["governance"]
    assert referral_count() == 0

    grounding = [
        e for e in entries_for_decision(result.governance["decision_id"])
        if e.entry_type.value == "PRE_NODE" and e.payload["pre_node"] == "AI_PROPOSAL_GROUNDED"
    ][0]
    assert grounding.payload["denied"] is True
    assert grounding.payload["evidence_supported"] < max(grounding.payload["evidence_cited"], 1)


# ------------------------------------------------------------------
# 5. Invariant failure -> TERMINAL, no write
# ------------------------------------------------------------------

async def test_invariant_failure_is_terminal_and_blocks_write(monkeypatch):
    # Simulate the patient disappearing between resolution and governance:
    # the real PATIENT_MUST_EXIST invariant then fails inside real VSL gates.
    monkeypatch.setattr(gates, "_patient_exists", lambda _pid: False)
    workflow, _, recorder = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "GOVERNANCE_TERMINAL"
    assert result.state == "TERMINAL"
    assert result.governance["invariant"] == "PATIENT_MUST_EXIST"
    assert result.governance["terminal_state"] == "referral-action-suspended"
    assert recorder.events == ["governance"]
    assert referral_count() == 0

    entries = entries_for_decision(result.governance["decision_id"])
    assert entries[-1].entry_type.value == "TERMINAL"
    assert entries[-1].caused_by == entries[-2].entry_id
    assert entries[-2].payload["result"] == "INSUFFICIENT"


# ------------------------------------------------------------------
# 6. Malformed / unsupported AI output -> no governance, no write
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "output",
    [
        "I think a referral would be good.",
        {"action": "DROP_TABLE referrals", "recommendation": "x"},
        dict(GROUNDED, department="Astrology"),
        dict(GROUNDED, reason=None),
        None,
    ],
)
async def test_malformed_proposal_never_reaches_governance(output):
    workflow, _, recorder = _workflow(output)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "INVALID_PROPOSAL"
    assert result.state == "FAILED"
    assert result.proposal is None
    assert result.proposal_errors
    assert "ACTION_PROPOSED" not in _states(result)
    assert recorder.events == []
    assert referral_count() == 0


async def test_out_of_scope_department_is_rejected():
    workflow, _, recorder = _workflow(dict(GROUNDED, department="Neurology"))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand", department="Cardiology"))

    assert result.status == "INVALID_PROPOSAL"
    assert recorder.events == []


# ------------------------------------------------------------------
# 7. LLM-supplied / manipulated UUIDs
# ------------------------------------------------------------------

async def test_ai_supplied_patient_uuid_is_ignored():
    workflow, _, recorder = _workflow(dict(GROUNDED, patient_id=JORDAN_A_ID, patient=JORDAN_A_ID))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "REFERRAL_CREATED"
    assert set(result.ignored_ai_fields) == {"patient_id", "patient"}
    assert recorder.decisions[0].candidate.patient_id == AISHA_ID
    with SessionLocal() as session:
        assert session.get(Referral, result.referral["referral_id"]).patient_id == AISHA_ID


async def test_uuid_embedded_in_ai_text_is_rejected():
    workflow, _, recorder = _workflow(dict(GROUNDED, reason=f"Patient {JORDAN_A_ID} needs review"))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "INVALID_PROPOSAL"
    assert recorder.events == []
    assert referral_count() == 0


# ------------------------------------------------------------------
# 8. The model's claims never override the actual outcome
# ------------------------------------------------------------------

async def test_ai_claiming_referral_created_does_not_create_one():
    claim = {"action": "NO_ACTION", "recommendation": "I have created the cardiology referral (ID 42)."}
    workflow, _, recorder = _workflow(claim)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "NO_ACTION_RECOMMENDED"
    assert result.referral is None
    assert "No action was taken" in result.message
    assert recorder.events == []
    assert referral_count() == 0


async def test_no_action_recommendation_completes_without_governance():
    workflow, _, recorder = _workflow({"action": "NO_ACTION", "recommendation": "No referral indicated."})

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.state == "COMPLETED"
    assert _states(result) == [
        "REQUESTED", "PATIENT_RESOLVED", "DATA_RETRIEVED", "ANALYSIS_COMPLETED", "ACTION_PROPOSED", "COMPLETED",
    ]
    assert result.governance is None
    assert recorder.events == []


# ------------------------------------------------------------------
# Failure handling, persistence, duplicate prevention
# ------------------------------------------------------------------

async def test_analysis_unavailable_fails_safely():
    workflow, _, recorder = _workflow(raises=AnalysisUnavailableError("down"))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "ANALYSIS_UNAVAILABLE"
    assert result.state == "FAILED"
    assert recorder.events == []
    assert referral_count() == 0


async def test_unexpected_analyzer_error_is_internal_error_without_write():
    workflow, _, recorder = _workflow(raises=RuntimeError("boom"))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))

    assert result.status == "INTERNAL_ERROR"
    assert "boom" not in result.message
    assert recorder.events == []
    assert referral_count() == 0


async def test_unsupported_requested_department_is_invalid_request():
    workflow, analyzer, _ = _workflow(GROUNDED)

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand", department="Astrology"))

    assert result.status == "INVALID_REQUEST"
    assert analyzer.calls == []


async def test_idempotency_key_prevents_duplicate_execution():
    workflow, analyzer, recorder = _workflow(GROUNDED)
    request = ClinicalReviewRequest("Aisha Wiegand", department="Cardiology", idempotency_key="review-key-0001")

    first = await workflow.run(request)
    second = await workflow.run(request)

    assert first.status == second.status == "REFERRAL_CREATED"
    assert second.replayed is True
    assert second.workflow_id == first.workflow_id
    assert len(analyzer.calls) == 1
    assert recorder.events == ["governance", "write"]
    assert referral_count() == 1


async def test_run_is_persisted_with_final_state():
    workflow, _, _ = _workflow(dict(GROUNDED, evidence=["Congestive heart failure"]))

    result = await workflow.run(ClinicalReviewRequest("Aisha Wiegand"))
    stored = workflow.store.get(result.workflow_id)

    assert stored["state"] == WorkflowState.REVIEW_REQUIRED.value
    assert stored["governance"]["decision_id"] == result.governance["decision_id"]
    assert [t["state"] for t in stored["transitions"]] == _states(result)
    assert workflow.store.list(5)[0]["workflow_id"] == result.workflow_id


async def test_human_referral_is_not_subject_to_ai_grounding():
    """
    Human-initiated (non-AI) referrals are not evaluated by
    AI_PROPOSAL_GROUNDED and carry no AI provenance. (Phase 3 renamed this
    test: the evidence is no longer three entries because of the new
    REFERRAL_TARGET_VALID and REFERRAL_NOT_DUPLICATE PreNodes.)
    """

    decision = await governed_referral({"patient_id": AISHA_ID, "department": "Cardiology", "reason": "x"})
    entries = entries_for_decision(decision["decision_id"])

    assert [e.entry_type.value for e in entries] == REFERRAL_ALLOW_SHAPE
    assert "AI_PROPOSAL_GROUNDED" not in pre_node_names(entries)
    assert "origin" not in entries[0].payload
