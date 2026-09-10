from agents import function_tool

from src.tools.order_tools import get_order
from src.tools.return_tools import check_return_eligibility, create_return_label

@function_tool(name_override="get_order")
def get_order_tool(order_id: str, email: str) -> dict:
    """Verify an order before starting a return workflow.

    Use this tool before revealing order details or taking any return action.

    Args:
        order_id: The customer's order ID.
        email: The email address associated with the order.
    """
    return get_order(order_id, email)

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
    )
