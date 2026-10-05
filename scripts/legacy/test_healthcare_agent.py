import asyncio

from backend.agent import create_agent


async def main() -> None:
    print("=" * 70)
    print("HEALTHCARE REFERRAL AGENT")
    print("=" * 70)

    agent = create_agent()

    query = (
        "Find the patient named "
        "Aisha756 Melina208 Wiegand701 "
        "and tell me their patient ID."
    )

    print("\nUSER:")
    print(query)

    print("\nAGENT:")
    result = await agent.run(query)

    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())