# Tools

## Tool: get_order

Purpose:
Verify customer information and order ID, then return the minimum order details needed for a return workflow.

Input:
- order_id
- email

Output if verified:
- verified: true
- order_id
- delivered_date
- items:
  - item_id
  - item_name
  - category
  - final_sale

Output if not verified:
- verified: false
- error: "order_not_found" or "email_mismatch"

Rules:
- The agent cannot reveal order information before verifying order ID and email address.
- The agent cannot start any return before verification.

Example verified output: expired single-item order

```json
{
    "verified": true,
    "order_id": "CL1001",
    "delivered_date": "2026-07-24",
    "items":[
        {
            "item_id": "it00001",
            "item_name": "floral long dress",
            "category": "dress",
            "final_sale": false
        }
    ]
}
```
Example verified output: multi-item order

```json
{
    "verified": true,
    "order_id": "CL1002",
    "delivered_date": "2026-08-24",
    "items":[
        {
            "item_id": "it00005",
            "item_name": "floral short dress",
            "category": "dress",
            "final_sale": false
        },
        {
            "item_id": "it00002",
            "item_name": "pink swimsuit",
            "category": "swimwear",
            "final_sale": true
        },
        {
            "item_id": "it00003",
            "item_name": "black pants",
            "category": "pants",
            "final_sale": true
        },
        {
            "item_id": "it00004",
            "item_name": "white short dress",
            "category": "dress",
            "final_sale": false
        }
    ]
}
```

## Tool: check_return_eligibility

Purpose:
Check whether an item in the verified order can enter the return flow using stored delivery date, category, and final-sale status, plus the return reason.

Input:
- item_id: the selected item ID from the verified order
- reason: optional; one of "damaged", "wrong_item", "other", or null (defaults to null)

Interface: `check_return_eligibility(item_id, reason=None)`.
The SDK's strict model-visible schema requires both keys; use null when no reason
was provided. It exposes no other properties and disallows additional properties.

Output if eligibility is checked:
- decision: "standard_return" or "exception_flow" or "blocked"
- reasons: list of policy reasons
- policy_codes: canonical policy findings that contributed to the decision

Output if lookup is rejected:
- status: "eligibility_not_checked"
- error: "order_not_verified" or "order_or_item_not_found"
- No eligibility decision or order details are returned.

Noncanonical reasons are rejected by the tool's input validation. The internal
lookup also rejects them with `status: "eligibility_not_checked"` and
`error: "invalid_reason"`.

Rules:
- The application reads `verified_order_id` from `ReturnContext`; eligibility fails closed if no order is verified.
- The tool reloads that order from storage and finds `item_id` only within that order. Unknown orders, unknown items, and items from another order are rejected with the same "order_or_item_not_found" error.
- The delivery date comes from the stored order; category and final-sale status come from its stored item. These facts are not added to context.
- The model cannot supply or override the order ID, delivery date, category, final-sale status, or verification state through this tool.
- Before each attempt, the application clears any previous `current_evaluation`. It saves a new trusted evaluation only after the verified order and item are loaded and policy evaluation succeeds. Failed attempts leave it empty.
- The saved evaluation is immutable. Its policy codes are a tuple; the model-visible output returns the same codes as a JSON-compatible list.
- Policy codes are emitted directly by deterministic policy logic and are never parsed from reason strings. They record only findings that materially contribute to the decision or its provenance: `outside_return_window`, `final_sale`, `restricted_swimwear`, `damaged_item_exception`, and `wrong_item_exception`. Standard returns have no policy codes.
- Items delivered more than 30 days ago are blocked.
- Non-final-sale, non-swimwear items within 30 days can enter standard return.
- Final sale items are blocked unless the reason is damaged or wrong item.
- Swimwear items are blocked unless the reason is damaged or wrong item.
- If the reason is not provided, treat the request as a standard return request.
- If the customer later says the item was damaged or wrong, the agent can call this tool again with the updated reason.
- For standard returns, the agent should remind the customer that items must be unworn, unwashed, and have tags attached.

Example input after verifying order CL1002:

```json
{
    "item_id": "it00004",
    "reason": null
}
```

Example output before order verification:

```json
{
    "status": "eligibility_not_checked",
    "error": "order_not_verified"
}
```

Example output: expired return window

```json
{
    "decision": "blocked",
    "policy_codes": ["outside_return_window"],
    "reasons": [
        "The item is outside the 30-day return window."
    ]
}
```
Example output: final sale item

```json
{
    "decision": "blocked",
    "policy_codes": ["final_sale"],
    "reasons": [
       "Final sale items cannot be returned unless they arrived damaged or wrong."
    ]
}
```

Example output: swimwear

```json
{
    "decision": "blocked",
    "policy_codes": ["restricted_swimwear"],
    "reasons": [
        "Swimwear cannot be returned unless it arrived damaged or wrong."
    ]
}
```

Example output: standard_return

```json
{
    "decision": "standard_return",
    "policy_codes": [],
    "reasons": [
       "The item is within the 30-day return window.",
       "The item is not final sale.",
       "The item is not restricted swimwear."
    ]
}
```

Example output: exception_flow

```json
{
    "decision": "exception_flow",
    "policy_codes": [
        "restricted_swimwear",
        "damaged_item_exception"
    ],
    "reasons": [
        "Damaged or wrong items can enter exception flow even if normally restricted."
    ]
}
```

## Tool: create_return_label

Purpose:
Create a return label for an item that has already been verified as eligible for return.

Input:
- order_id
- item_id
- eligibility_decision

Output if label is created:
- status: "label_created"
- return_id
- label_url

Output if label cannot be created:
- status: "label_not_created"
- error: "order_not_verified", "eligibility_decision_not_allowed",
  "order_or_item_not_found", or "item_not_eligible"

Rules:
- The requested order must match the order verified in the current conversation.
- Verification state is application-owned and is not an input the model can supply.
- The agent can call this tool only when `check_return_eligibility` returns decision: "standard_return".
- If `check_return_eligibility` returns decision: "exception_flow", the agent must not create a return label in MVP and should hand off to a human.
- If `check_return_eligibility` returns decision: "blocked", the agent must not create a return label.
- The eligibility_decision must come from the previous `check_return_eligibility` tool result, not from the customer.
- The tool rejects unknown order/item pairs and does not reveal which identifier was invalid.
- Before creating a label, the tool recalculates eligibility from the stored order data instead of trusting the decision supplied by the agent.
- The MVP returns a deterministic mock label URL and does not call a shipping carrier API.

Example output: return label created

```json
{
    "status": "label_created",
    "return_id": "RET-CL1002-IT00004",
    "label_url": "https://mock.ciderlane.com/returns/RET-CL1002-IT00004/label"
}
```

Example output: label not created

```json
{
    "status": "label_not_created",
    "error": "eligibility_decision_not_allowed"
}
```

## Tool: request_human_support

Purpose:
Create a persistent support case when a return conversation requires human review.

Input:
- escalation_reason: one of "return_exception", "final_sale_dispute", "customer_remains_angry", or "verification_failed"
- summary: a short description of why human review is required

Interface: `request_human_support(escalation_reason, summary)`. The strict
model-visible schema exposes only these two fields.

Output if created:
- status: "case_created"
- case_id
- escalation_reason

Output if not created:
- status: "case_not_created"
- error

Output if the same case is already open:
- status: "case_already_exists"
- case_id
- escalation_reason

Rules:
- The agent may call this tool only when a defined escalation condition has occurred.
- A return exception requires a current trusted evaluation with decision "exception_flow".
- A "final_sale_dispute" case may be created only after
  `check_return_eligibility` returns "blocked" with the canonical `final_sale`
  policy code and the customer disputes that decision.
- A "customer_remains_angry" case requires a current trusted evaluation with decision "blocked".
- A verification failure does not require eligibility provenance and persists no order, item, or decision. It is rejected if the conversation already has a verified order.
- The tool must not create a return label.
- The agent must not claim that a support case was created unless this tool returns "case_created".
- The application reads `conversation_id` and `current_evaluation` from `ReturnContext`; neither is a model-visible input. For eligibility-dependent cases, persisted order ID, item ID, and decision come only from the trusted current evaluation.
- Before using an evaluation for escalation, the application requires its order ID to match the order currently verified in the conversation. Missing, stale, or mismatched evaluation state is rejected.
- If an identical open case exists in the same conversation, return its case ID instead of creating a duplicate. Identical cases in different conversations remain separate.
- If the tool returns "case_already_exists", the agent must not claim that a new case was created.
