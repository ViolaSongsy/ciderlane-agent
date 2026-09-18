from typing import Literal

from agents import RunContextWrapper, function_tool

from src.context import ReturnContext
from src.tools.order_tools import get_order
from src.tools.return_tools import check_return_eligibility, create_return_label
from src.tools.support_tools import create_support_case

@function_tool(name_override="get_order")
def get_order_tool(
    ctx: RunContextWrapper[ReturnContext],
    order_id: str,
    email: str,
) -> dict:
    """Verify an order before starting a return workflow.

    Use this tool before revealing order details or taking any return action.

    Args:
        order_id: The customer's order ID.
        email: The email address associated with the order.
    """
    ctx.context.verified_order_id = None
    result = get_order(order_id, email)

    if result["verified"]:
        ctx.context.verified_order_id = result["order_id"]

    return result

@function_tool(name_override="check_return_eligibility")
def check_return_eligibility_tool(
    delivered_date: str,
    category: str,
    final_sale: bool,
    reason: str | None = None,
) -> dict:
    """Check whether an item can enter the return workflow.

    Args:
        delivered_date: Delivery date in YYYY-MM-DD format.
        category: The item's product category.
        final_sale: Whether the item was marked final sale.
        reason: Canonical return reason, or None if not provided.
    """
    return check_return_eligibility(delivered_date,category,final_sale,reason)


@function_tool(name_override="create_return_label")
def create_return_label_tool(
    ctx: RunContextWrapper[ReturnContext],
    order_id: str,
    item_id: str,
    eligibility_decision: str,
) -> dict:
    """Create a mock return label for an eligible item.

    Use this tool only after check_return_eligibility returns standard_return.

    Args:
        order_id: The verified order ID.
        item_id: The selected item ID from the verified order.
        eligibility_decision: The decision returned by check_return_eligibility.
    """
    return create_return_label(
        order_id,
        item_id,
        eligibility_decision,
        context=ctx.context,
    )


@function_tool(name_override="create_support_case")
def create_support_case_tool(
    escalation_reason: Literal[
        "return_exception",
        "final_sale_dispute",
        "customer_remains_angry",
        "verification_failed",
    ],
    summary: str,
    order_id: str | None = None,
    item_id: str | None = None,
    eligibility_decision: str | None = None,
) -> dict:
    """Create or retrieve a persistent case for required human review.

    Args:
        escalation_reason: The canonical reason that human review is required.
        summary: A short factual description for the human support team.
        order_id: The verified order ID, or a user-provided ID when verification failed.
        item_id: The selected verified item ID, when available.
        eligibility_decision: The decision returned by check_return_eligibility.
    """
    return create_support_case(
        escalation_reason=escalation_reason,
        summary=summary,
        order_id=order_id,
        item_id=item_id,
        eligibility_decision=eligibility_decision,
    )
