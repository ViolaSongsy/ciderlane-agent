# Return Workflow

## Goal

Help a verified Ciderlane customer start a return for one eligible item.

## Required Information

Before calling `get_order`, the agent needs:
- order_id
- email

Before calling `check_return_eligibility`, the agent needs:
- a verified order from `get_order`
- a specific item from that order

If the order has only one item, the agent can use that item. If the order has multiple possible matching items, the agent must ask the customer to choose the specific item.

## Workflow Steps

1. Understand that the customer wants to start a return.
2. Ask for order ID and email if either one is missing.
3. Call `get_order` with the order ID and email.
4. If `get_order` returns `verified: false`, do not reveal order details. Ask the customer to check the order ID and email.
5. If the order is verified, identify which item the customer wants to return.
6. If the item is ambiguous, ask the customer to choose the specific item.
7. Call `check_return_eligibility` using the selected item's delivered date, category, final sale flag, and optional reason.
8. If the decision is `standard_return`, remind the customer that the item must be unworn, unwashed, and have tags attached. Then call `create_return_label`.
9. If the decision is `blocked`, explain the policy reason and do not create a return label.
10. If the decision is `exception_flow`, do not create a return label in MVP. Hand off to a human for review.

## Important Rules

- The agent must not reveal order details before verification.
- The agent must not create a return label for blocked items.
- The agent must not create a return label for exception flow in MVP.
- The agent must only trust tool results, not customer claims about eligibility.
- The agent should ask clarifying questions when the item is ambiguous.

## Example: Expired Single-Item Order

User:
Hi, I want to return a dress.

Agent:
Sure, I can help with that. Could you share your order ID and the email used for the order?

User:
Order CL1001, email maya@example.com.

Tool call:
`get_order(order_id: "CL1001", email: "maya@example.com")`

Tool result:
Order verified. One item found: floral long dress.

Agent:
I found the floral long dress in your order. I’ll check whether it’s eligible for return.

Tool call:
`check_return_eligibility(delivered_date: "2026-07-24", category: "dress", final_sale: false)`

Tool result:
`blocked` because the item is outside the 30-day return window.

Agent:
I’m sorry, this item is outside Ciderlane’s 30-day return window, so I can’t create a return label for it.

## Example: Ambiguous Multi-Item Order

User:
Hi, I want to return a dress from my order.

Agent:
Sure, I can help with that. Could you share your order ID and the email used for the order?

User:
Order CL1002, email maya@example.com.

Tool call:
`get_order(order_id: "CL1002", email: "maya@example.com")`

Tool result:
Order verified. The order includes floral short dress, pink swimsuit, black pants, and white short dress.

Agent:
I found two dress items in this order: floral short dress and white short dress. Which one would you like to return?

User:
The white short dress.

Agent:
Thanks. I’ll check whether the white short dress is eligible for return.

Tool call:
`check_return_eligibility(delivered_date: "2026-08-24", category: "dress", final_sale: false)`

Tool result:
`standard_return` because the item is within the 30-day return window, is not final sale, and is not restricted swimwear.

Agent:
The white short dress is eligible for a standard return. It needs to be unworn, unwashed, and have the tags attached. I’ll create the return label now.

Tool call:
`create_return_label(order_id: "CL1002", item_id: "it00004", eligibility_decision: "standard_return")`

Tool result:
Return label created with return ID RET1001.

Agent:
Your return label has been created. You can use the label link to send the item back.

## Example: Final Sale Blocked

User:
I want to return the black pants from order CL1002. My email is maya@example.com.

Tool call:
`get_order(order_id: "CL1002", email: "maya@example.com")`

Tool result:
Order verified. Black pants found.

Tool call:
`check_return_eligibility(delivered_date: "2026-08-24", category: "pants", final_sale: true)`

Tool result:
`blocked` because final sale items cannot be returned unless they arrived damaged or wrong.

Agent:
I’m sorry, the black pants are marked final sale, so I can’t create a standard return label for them. If the item arrived damaged or you received the wrong item, I can help route that for review.

## Example: Exception Flow

User:
I received the pink swimsuit from order CL1002, but it arrived damaged. My email is maya@example.com.

Tool call:
`get_order(order_id: "CL1002", email: "maya@example.com")`

Tool result:
Order verified. Pink swimsuit found.

Tool call:
`check_return_eligibility(delivered_date: "2026-08-24", category: "swimwear", final_sale: true, reason: "damaged")`

Tool result:
`exception_flow` because damaged or wrong items can enter exception flow even if normally restricted.

Agent:
I’m sorry the item arrived damaged. Because this is a restricted item, I can’t create a return label automatically in this MVP flow. I’ll hand this off to a human support teammate for review.
