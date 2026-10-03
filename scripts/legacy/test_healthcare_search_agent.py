import asyncio

from agent_framework import Agent
from backend.agent import _create_client

from backend.agent_tools import search_patient


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
        name="HealthcareSearchTest",

        instructions="""
You are a healthcare patient search assistant.

When the user gives a patient name,
you MUST call search_patient.

Do not ask for confirmation.

Do not guess a patient ID.

Actually call the tool.
""",

        tools=[
            search_patient,
        ],
    )

    result = await agent.run(
        "Find the patient Aisha756 Melina208 Wiegand701."
    )

    print("\n===== RESULT =====")
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
