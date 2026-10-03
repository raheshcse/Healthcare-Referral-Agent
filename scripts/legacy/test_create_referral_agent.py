import asyncio

from agent_framework import Agent
from backend.agent import _create_client

from backend.agent_tools import create_referral


async def main():

    client = _create_client()

    client.function_invocation_configuration.update(
        {
            "include_detailed_errors": True,
            "allow_concurrent_invocation": False,
            "max_iterations": 4,
            "max_function_calls": 4,
        }
    )

    agent = Agent(
        client=client,
        name="CreateReferralTest",

        instructions="""
You are a healthcare referral assistant.

The patient has already been identified.

You MUST call create_referral.

Use the EXACT patient_id provided by the user.

Do not ask for confirmation.
Do not explain the workflow.
Actually call the tool.
""",

        tools=[
            create_referral,
        ],
    )

    result = await agent.run(
        "Create a cardiology referral for patient "
        "8f998bfd-bcee-9bc0-e435-7c50e571a851 "
        "because the patient requires a cardiology assessment."
    )

    print("\n===== FINAL RESULT =====")
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
