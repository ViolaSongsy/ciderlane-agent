# Return Workflow

## Goal

Help a verified Ciderlane customer start a return for one eligible item.

## Required Information

Before calling `verify_order`, the agent needs:
- order_id
- email

Before calling `check_return_eligibility` or `start_return`, the agent needs:
- a verified order from `verify_order`
- a specific item from that order

If the order has only one item, the agent can use that item. If the order has multiple possible matching items, the agent must ask the customer to choose the specific item.

## Workflow Steps

1. Understand that the customer wants to start a return.
2. Ask for order ID and email if either one is missing.
3. Call `verify_order` with the order ID and email.
4. If `verify_order` returns `verified: false`, do not reveal order details. Ask the customer to check the order ID and email.
5. If the order is verified, identify which item the customer wants to return.
6. If the item is ambiguous, ask the customer to choose the specific item.
7. For an eligibility question, call `check_return_eligibility(item_id, reason=None)`. For a request to return the item, call `start_return(item_id, reason=None)` directly with only the selected item ID and a canonical reason (`damaged`, `wrong_item`, `other`, or null). The application reads the verified order ID from `ReturnContext`, reloads that order, and uses its stored delivery date and the item's category and final-sale status.
8. `start_return` pauses for the customer to approve the concrete item. If they reject, no return or label is created. After approval, the tool reloads facts and applies policy at execution time. If the decision is `standard_return`, remind the customer that the item must be unworn, unwashed, and have tags attached. When `start_return` returns `return_created`, include the label URL. When it returns `return_already_exists`, provide the existing return without claiming a new one.
9. If the decision is `blocked`, explain the policy reason and do not create a return label.
10. If the decision is `exception_flow`, do not create a return label. Call `request_human_support(escalation_reason, summary)` and provide the case ID to the customer.

## Important Rules

- The agent must not reveal order details before verification.
- The application records the successfully verified order for the current conversation. Every new verification attempt clears both the previous verification and the current trusted evaluation; label creation rejects a different or unverified order.
- Eligibility fails closed when there is no verified order, and rejects items outside that order. An `eligibility_not_checked` response contains no policy decision: for `order_not_verified`, complete verification before retrying; for `order_or_item_not_found`, confirm the selected item from the verified order before retrying.
- Eligibility inputs contain only `item_id` and `reason`. The model cannot supply or override the order ID, delivery date, category, final-sale status, or verification state. A successful lookup saves an immutable application-owned `current_evaluation` with the trusted order ID, item ID, decision, and canonical policy codes. A failed lookup clears it. `ReturnContext` also stores an application-generated `conversation_id` that remains stable across turns and scopes support-case idempotency.
- `request_human_support` exposes only `escalation_reason` and `summary`. For eligibility-dependent escalation, the application reads order, item, decision, and policy codes from `current_evaluation` and verifies that the evaluation order matches the currently verified order. Verification failure does not require eligibility provenance.
- The agent must not create a return label for blocked items.
- The agent must not create a return label for exception flow.
- The agent must create a support case when human review is required.
- The agent must show the specific item name and ID for approval before `start_return` executes. Approval does not authorize an ineligible return.
- The agent must only trust tool results, not customer claims about eligibility.
- The agent should ask clarifying questions when the item is ambiguous.
- Return writes independently authorize using fresh stored facts; a prior eligibility result is not authorization.
- Return idempotency is global through SQLite UNIQUE(order_id, item_id). Existing returns are retrieved before policy evaluation, even when policy has changed.
- If `start_return` returns an error, do not claim that a return or label was created.

## Example: Expired Single-Item Order

User:
Hi, I want to return a dress.

Agent:
Sure, I can help with that. Could you share your order ID and the email used for the order?

User:
Order CL1001, email maya@example.com.

Tool call:
`verify_order(order_id: "CL1001", email: "maya@example.com")`

Tool result:
Order verified. One item found: floral long dress.

Agent:
I found the floral long dress in your order. I’ll check whether it’s eligible for return.

Tool call:
`start_return(item_id: "it00001", reason: null)`

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
`verify_order(order_id: "CL1002", email: "maya@example.com")`

Tool result:
Order verified. The order includes floral short dress, pink swimsuit, black pants, and white short dress.

Agent:
I found two dress items in this order: floral short dress and white short dress. Which one would you like to return?

User:
The white short dress.

Agent:
Thanks. I’ll check whether the white short dress is eligible for return.

Tool call:
`start_return(item_id: "it00004", reason: null)`

Tool result:
`return_created` with decision `standard_return`, return ID RET-CL1002-IT00004,
and the provider's label URL, for example https://carrier.example.test/label.pdf.

Agent:
The white short dress is eligible and your return is created. It must be unworn,
unwashed, and have its tags attached. Use the provided label URL to send it back.

## Example: Final Sale Blocked

User:
I want to return the black pants from order CL1002. My email is maya@example.com.

Tool call:
`verify_order(order_id: "CL1002", email: "maya@example.com")`

Tool result:
Order verified. Black pants found.

Tool call:
`start_return(item_id: "it00003", reason: null)`

Tool result:
`blocked` because final sale items cannot be returned unless they arrived damaged or wrong.

Agent:
I’m sorry, the black pants are marked final sale, so I can’t create a standard return label for them. If the item arrived damaged or you received the wrong item, I can help route that for review.

## Example: Exception Flow

User:
Please return the pink swimsuit from order CL1002; it arrived damaged. My email is maya@example.com.

Tool call:
`verify_order(order_id: "CL1002", email: "maya@example.com")`

Tool result:
Order verified. Pink swimsuit found.

Tool call:
`start_return(item_id: "it00002", reason: "damaged")`

Tool result:
`exception_flow` because damaged or wrong items can enter exception flow even if normally restricted.

Tool call:
`request_human_support(escalation_reason: "return_exception", summary: "The verified customer's swimsuit arrived damaged.")`

Tool result:
Support case created with a case ID.

Agent:
I’m sorry the item arrived damaged. This restricted-item exception requires human review, so I created a support case and included the case ID for you.
