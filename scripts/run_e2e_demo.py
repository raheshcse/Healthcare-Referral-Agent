"""
Live end-to-end demonstration (requires OPENAI_API_KEY in .env).

    1. Doctor request -> OpenAI -> deterministic patient resolution
       -> referral candidate -> vsl-maf / X-Verba VSL -> ALLOW -> SQLite
    2. Doctor request for a nonexistent patient -> workflow detects
       PATIENT_NOT_FOUND -> no referral
    3. Referral candidate for a nonexistent patient ID -> VSL
       -> TERMINAL -> no database write
    4. Phase 2 clinical review via the agent (OpenAI analysis ->
       validated proposal -> X-Verba governance -> ALLOW/DENY)
    5. Phase 2 clinical review via the workflow service directly

Writes to the application database and ledger (data/). Run from the
project root:

    python -m scripts.run_e2e_demo
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import func, select

from backend.chat import ChatService
from backend.clinical_workflow import ClinicalReviewRequest, get_default_clinical_workflow
from backend.database.connection import SessionLocal
from backend.database.models import Referral
from backend.database.schema import ensure_schema
from backend.domain import ReferralRequest
from backend.workflow import get_default_workflow
from governance.ledger import entries_for_decision, ledger_integrity_ok, summarize_decision


def referral_count() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(Referral)) or 0


def show(title: str, payload: object) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    print(json.dumps(payload, indent=2, default=str))


def evidence(decision_id: str | None) -> None:
    if not decision_id:
        print("No governance decision (stopped before governance).")
        return
    entries = entries_for_decision(decision_id)
    print(f"Ledger decision {decision_id}: {summarize_decision(entries)['decision']}")
    for entry in entries:
        print(f"  {entry.sequence:>4} {entry.entry_type.value:<14} caused_by={entry.caused_by}")


async def main() -> None:
    ensure_schema()
    service = ChatService()

    # 1. Positive scenario through OpenAI
    before = referral_count()
    result = await service.chat(
        "Please refer Aisha Wiegand to Cardiology because of persistent "
        "chest pain on exertion."
    )
    show("SCENARIO 1 - valid patient via OpenAI", {
        "reply": result.reply,
        "agent_reply": result.agent_reply,
        "workflow_results": [o.to_dict() for o in result.workflow_results],
        "referrals_created": referral_count() - before,
    })
    for outcome in result.workflow_results:
        evidence(outcome.governance.decision_id if outcome.governance else None)

    # 2. Nonexistent patient through OpenAI
    before = referral_count()
    result = await service.chat(
        "Refer Zebulon Nobody to Neurology for recurrent migraines."
    )
    show("SCENARIO 2 - nonexistent patient via OpenAI", {
        "reply": result.reply,
        "workflow_results": [o.to_dict() for o in result.workflow_results],
        "referrals_created": referral_count() - before,
    })

    # 3. Governance failure (no LLM): VSL TERMINAL
    before = referral_count()
    outcome = await get_default_workflow().run(
        ReferralRequest(
            patient_id="00000000-0000-0000-0000-000000000000",
            department="Cardiology",
            reason="Governance failure demonstration.",
        )
    )
    show("SCENARIO 3 - VSL TERMINAL, no database write", {
        **outcome.to_dict(),
        "referrals_created": referral_count() - before,
    })
    evidence(outcome.governance.decision_id if outcome.governance else None)

    # 4. Phase 2: governed clinical review through OpenAI (agent path)
    before = referral_count()
    result = await service.chat(
        "Review Aisha Wiegand and determine whether a cardiology referral is appropriate."
    )
    show("SCENARIO 4 - Phase 2 clinical review via OpenAI (agent)", {
        "reply": result.reply,
        "agent_reply": result.agent_reply,
        "clinical_reviews": [r.to_dict() for r in result.clinical_reviews],
        "referrals_created": referral_count() - before,
    })
    for review in result.clinical_reviews:
        evidence((review.governance or {}).get("decision_id"))

    # 5. Phase 2: same workflow service called directly (API path)
    before = referral_count()
    review = await get_default_clinical_workflow().run(
        ClinicalReviewRequest(patient_name="Aisha Wiegand", department="Endocrinology")
    )
    show("SCENARIO 5 - Phase 2 clinical review (direct workflow)", {
        **review.to_dict(),
        "referrals_created": referral_count() - before,
    })
    evidence((review.governance or {}).get("decision_id"))

    print(f"\nLedger integrity: {ledger_integrity_ok()}")


if __name__ == "__main__":
    asyncio.run(main())
