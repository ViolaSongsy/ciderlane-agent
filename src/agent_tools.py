from typing import Literal

from agents import RunContextWrapper, function_tool
from agents.tool import default_tool_error_function

from src.context import ReturnContext
from src.tools.order_tools import get_order
from src.tools.return_tools import (
    ReturnReason,
    check_return_eligibility,
    start_return,
)
from src.tools.support_tools import request_human_support


@function_tool(name_override="begin_new_order_request")
def begin_new_order_request_tool(ctx: RunContextWrapper[ReturnContext]) -> dict:
    """Clear prior verification when the customer confirms a different order.

    Clarify unclear order scope first. Do not use for the same order or a return
    retry. Wait for this result before verifying or escalating the new order.
    """
    ctx.context.verified_order_id = None
    ctx.context.current_evaluation = None
    return {"status": "new_order_request_started", "verified_order_id": None}


@function_tool(name_override="verify_order")
def verify_order_tool(
    ctx: RunContextWrapper[ReturnContext],
    order_id: str,
    email: str,
) -> dict:
    """Verify an order using the customer's supplied order ID and email.

    Call only after the customer supplies both actual values; never guess them
    or use empty strings or placeholders. Verification is required before
    asking the customer to select an item or revealing order details. For a
    different order, begin_new_order_request first. If its credentials are
    unavailable, use verification_failed support instead of guessing values.

    Args:
        order_id: The customer's order ID.
        email: The email address associated with the order.
    """
    ctx.context.verified_order_id = None
    ctx.context.current_evaluation = None
    result = get_order(order_id, email)

    if result["verified"]:
        ctx.context.verified_order_id = result["order_id"]

    return result

@function_tool(name_override="check_return_eligibility")
def check_return_eligibility_tool(
    ctx: RunContextWrapper[ReturnContext],
    item_id: str,
    reason: ReturnReason | None = None,
) -> dict:
    """Read an existing return or check the verified item's stored eligibility facts.

    Call verify_order first. Returns return_already_exists with its original
    label when a return is saved; otherwise checks eligibility. Does not create
    a return or label.

    Args:
        item_id: The selected item ID from the verified order.
        reason: Canonical return reason: damaged, wrong_item, other, or None.
    """
    return check_return_eligibility(item_id, reason, context=ctx.context)


def _start_return_error(ctx: RunContextWrapper[ReturnContext], error: Exception) -> str:
    # SDK input validation can fail before the business function is entered.
    ctx.context.current_evaluation = None
    return default_tool_error_function(ctx, error)


@function_tool(
    name_override="start_return",
    needs_approval=True,
    failure_error_function=_start_return_error,
)
def start_return_tool(
    ctx: RunContextWrapper[ReturnContext],
    item_id: str,
    reason: ReturnReason | None = None,
) -> dict:
    """Start a requested return using fresh stored facts, or retrieve its label.

    Verify the order first. No prior eligibility check is needed.
    An exception_flow result requires request_human_support for human review.

    Args:
        item_id: The selected item ID from the verified order.
        reason: Canonical return reason: damaged, wrong_item, other, or None.
    """
    return start_return(item_id, reason, context=ctx.context)


@function_tool(name_override="request_human_support")
def request_human_support_tool(
    ctx: RunContextWrapper[ReturnContext],
    escalation_reason: Literal[
        "return_exception",
        "final_sale_dispute",
        "customer_remains_angry",
        "verification_failed",
    ],
    summary: str,
) -> dict:
    """Create or retrieve a persistent case for required human review.

    For unavailable credentials, use verification_failed only when the active
    order is unverified. For a confirmed different order, wait for
    begin_new_order_request first. A failed verify_order call is not required.

    Args:
        escalation_reason: The canonical reason that human review is required.
        summary: A short factual description for the human support team.
    """
    return request_human_support(
        escalation_reason=escalation_reason,
        summary=summary,
        context=ctx.context,
    )
