import asyncio

from backend.agent import create_agent


async def main() -> None:
    print("=" * 70)
    print("END-TO-END HEALTHCARE REFERRAL TEST")
    print("=" * 70)

    agent = create_agent()

    request = (
        "Create a cardiology referral for "
        "Aisha756 Melina208 Wiegand701 "
        "because the patient requires a cardiology assessment."
    )

    print("\nDOCTOR:")
    print(request)

    print("\nAGENT:")
    
    result = await agent.run(request)

    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())