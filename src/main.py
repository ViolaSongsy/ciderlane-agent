import json

from agents import Runner
from dotenv import load_dotenv

from src.agent import ciderlane_agent
from src.context import ReturnContext
from src.tools.order_tools import load_orders


def approval_description(interruption, context: ReturnContext) -> str | None:
    """Describe the proposed item only when it belongs to the verified order."""
    raw = interruption.raw_item
    arguments = raw.get("arguments") if isinstance(raw, dict) else getattr(raw, "arguments", None)
    try:
        proposed = json.loads(arguments) if isinstance(arguments, str) else {}
    except json.JSONDecodeError:
        proposed = {}
    if not isinstance(proposed, dict) or not context.verified_order_id:
        return None
    item_id = proposed.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return None
    try:
        orders = load_orders()
    except (OSError, ValueError):
        return None
    order = next((order for order in orders
                  if order["order_id"] == context.verified_order_id), None)
    item = next((item for item in order["items"]
                 if item["item_id"] == item_id), None) if order else None
    if item is None or not isinstance(item.get("item_name"), str) or not item["item_name"].strip():
        return None
    reason = proposed.get("reason")
    return (f"{item['item_name']} (item ID {item_id}) from order "
            f"{context.verified_order_id}; reason: {reason or 'not provided'}")


def resolve_approvals(result, context: ReturnContext, *, prompt=input):
    """Ask for each SDK interruption, then resume the run from its state."""
    while result.interruptions:
        state = result.to_state()
        for interruption in result.interruptions:
            if interruption.tool_name != "start_return":
                state.reject(interruption)
                continue
            description = approval_description(interruption, context)
            if description is None:
                state.reject(
                    interruption,
                    rejection_message=("The selected item could not be resolved in the "
                                       "verified order. No return or label was created."),
                )
                continue
            answer = prompt(f"Approve starting a return for {description}? [y/N]: ")
            if answer.strip().lower() in {"y", "yes"}:
                state.approve(interruption)
            else:
                state.reject(
                    interruption,
                    rejection_message=("The customer declined this return. Do not call "
                                       "start_return again for this request. Explain that "
                                       "no return or label was created."),
                )
        result = Runner.run_sync(ciderlane_agent, state)
    return result


def main():
    load_dotenv()
    conversation = []
    context = ReturnContext()

    print("Ciderlane Return Agent")
    print("Type 'quit' to exit.\n")

    while True:
        user_message = input("You: ").strip()

        if user_message.lower() in {"quit", "exit"}:
            print("Goodbye!")
            break

        if not user_message:
            continue

        conversation.append({"role": "user", "content": user_message})
        result = Runner.run_sync(ciderlane_agent, conversation, context=context)
        result = resolve_approvals(result, context)
        print(f"Agent: {result.final_output}\n")
        conversation = result.to_input_list()


if __name__ == "__main__":
    main()
