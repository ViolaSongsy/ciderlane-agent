from agents import Runner
from dotenv import load_dotenv

from src.agent import ciderlane_agent


def main():
    load_dotenv()
    conversation = []

    print("Ciderlane Return Agent")
    print("Type 'quit' to exit.\n")

    while True:
        user_message = input("You: ").strip()

        if user_message.lower() in {"quit", "exit"}:
            print("Goodbye!")
            break

        if not user_message:
            continue

        conversation.append(
            {
                "role": "user",
                "content": user_message,
            }
        )

        result = Runner.run_sync(ciderlane_agent, conversation)

        print(f"Agent: {result.final_output}\n")

        conversation = result.to_input_list()


if __name__ == "__main__":
    main()