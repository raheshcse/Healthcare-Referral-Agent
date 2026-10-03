import asyncio
from dotenv import load_dotenv
from agent_framework import Agent, tool
from backend.agent import _create_client

load_dotenv()


@tool
def search_patient(name: str) -> dict:
    """Search for a patient by their full human-readable name."""
    print(f"\n>>> ACTUAL TOOL CALLED: search_patient({name!r})")

    return {
        "success": True,
        "patient_id": "8f998bfd-bcee-9bc0-e435-7c50e571a851",
        "name": name,
    }


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
        name="ToolCallingTestAgent",
        instructions="""
You are testing tool calling.

When the user gives a patient name, you MUST call
search_patient.

Do not ask the user for confirmation.
Do not explain what you would do.
Actually call the search_patient tool.
""",
        tools=[search_patient],
    )

    result = await agent.run(
        "Find patient Aisha756 Melina208 Wiegand701."
    )

    print("\n===== FINAL RESULT =====")
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
