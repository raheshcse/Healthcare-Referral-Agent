import asyncio

from agent_framework import Agent
from backend.agent import _create_client

from backend.agent_tools import (
    search_patient,
    create_referral,
)


async def main():

    client = _create_client()

    client.function_invocation_configuration.update(
        {
            "include_detailed_errors": True,
            "allow_concurrent_invocation": False,
            "max_iterations": 6,
            "max_function_calls": 6,
        }
    )

    agent = Agent(
        client=client,
        name="HealthcareReferralToolsTest",

        instructions="""
You are testing a healthcare referral workflow.

IMPORTANT WORKFLOW:

1. The user provides a patient name.
2. ALWAYS call search_patient FIRST.
3. Read the exact patient_id returned by search_patient.
4. Then call create_referral using that EXACT patient_id.
5. Never invent a patient ID.
6. Never use the patient's name as patient_id.
7. Do not ask for confirmation.
8. Actually execute the tools.
""",

        tools=[
            search_patient,
            create_referral,
        ],
    )

    result = await agent.run(
        "Create a cardiology referral for "
        "Aisha756 Melina208 Wiegand701 "
        "because the patient requires a cardiology assessment."
    )

    print("\n===== FINAL RESULT =====")
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
