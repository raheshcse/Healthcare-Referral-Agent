"""
Call the agent-facing create_referral tool directly (no LLM).

The tool takes a patient NAME; the application resolves the internal
patient UUID deterministically and runs application validation.
"""

import asyncio

from backend.agent_tools import create_referral
from backend.database.schema import ensure_schema


async def main() -> None:
    ensure_schema()

    result = await create_referral(
        patient_name="Aisha Wiegand",
        department="Cardiology",
        reason="Persistent cardiac symptoms requiring specialist review.",
    )

    print(result)


if __name__ == "__main__":
    asyncio.run(main())
