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
- error: "eligibility_decision_not_allowed"

Rules:
- The agent can call this tool only when `check_return_eligibility` returns decision: "standard_return".
- If `check_return_eligibility` returns decision: "exception_flow", the agent must not create a return label in MVP and should hand off to a human.
- If `check_return_eligibility` returns decision: "blocked", the agent must not create a return label.
- The eligibility_decision must come from the previous `check_return_eligibility` tool result, not from the customer.
- The MVP returns a deterministic mock label URL and does not call a shipping carrier API.

Example output: return label created

```json
{
    "status": "label_created",
    "return_id": "RET-CL1001-IT00001",
    "label_url": "https://mock.ciderlane.com/returns/RET-CL1001-IT00001/label"
}
```

Example output: label not created

```json
{
    "status": "label_not_created",
    "error": "eligibility_decision_not_allowed"
}
```
