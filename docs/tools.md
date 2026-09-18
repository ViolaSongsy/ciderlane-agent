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
Check whether an item can enter the return flow based on delivery date, category, final sale status, and return reason.

Input:
- delivered_date
- category
- final_sale
- reason: optional; one of "damaged", "wrong_item", or "other"

Output:
- decision: "standard_return" or "exception_flow" or "blocked"
- reasons: list of policy reasons

Rules:
- Items delivered more than 30 days ago are blocked.
- Non-final-sale, non-swimwear items within 30 days can enter standard return.
- Final sale items are blocked unless the reason is damaged or wrong item.
- Swimwear items are blocked unless the reason is damaged or wrong item.
- If the reason is not provided, treat the request as a standard return request.
- If the customer later says the item was damaged or wrong, the agent can call this tool again with the updated reason.
- For standard returns, the agent should remind the customer that items must be unworn, unwashed, and have tags attached.

Example output: expired return window

```json
{
    "decision": "blocked",
    "reasons": [
        "The item is outside the 30-day return window."
    ]
}
```
Example output: final sale item

```json
{
    "decision": "blocked",
    "reasons": [
       "Final sale items cannot be returned unless they arrived damaged or wrong."
    ]
}
```

Example output: swimwear

```json
{
    "decision": "blocked",
    "reasons": [
        "Swimwear cannot be returned unless it arrived damaged or wrong."
    ]
}
```

Example output: standard_return

```json
{
    "decision": "standard_return",
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

## Tool: create_support_case

Purpose:
Create a persistent support case when a return conversation requires human review.

Input:
- escalation_reason: one of "return_exception", "final_sale_dispute", "customer_remains_angry", or "verification_failed"
- order_id: optional
- item_id: optional
- eligibility_decision: optional
- summary: a short description of why human review is required

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
- A return exception requires an eligibility decision of "exception_flow".
- A "final_sale_dispute" case may be created only after
  `check_return_eligibility` returns "blocked" for a final-sale item and the
  customer disputes that decision.
- A verification failure must not include unverified order details.
- The tool must not create a return label.
- The agent must not claim that a support case was created unless this tool returns "case_created".
- If an identical open case exists, return its case ID instead of creating a duplicate.
- If the tool returns "case_already_exists", the agent must not claim that a new case was created.
