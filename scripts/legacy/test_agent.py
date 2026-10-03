import asyncio

from backend.agent import create_agent


async def main():

    print("\n======================================")
    print("MICROSOFT AGENT FRAMEWORK TEST")
    print("======================================")

    agent = create_agent()

    print("Agent created successfully.")
    print("Sending request to the configured OpenAI model...\n")

    result = await agent.run(
        "Say hello and explain in one sentence "
        "what you can help healthcare staff with."
    )

    print("AGENT RESPONSE")
    print("--------------------------------------")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
