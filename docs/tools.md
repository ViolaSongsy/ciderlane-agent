# Tool Contracts

Five tools are registered in [agent.py](../src/agent.py). Their model-visible
wrappers live in [agent_tools.py](../src/agent_tools.py); business implementations
live in `src/tools/`.

Tool schemas reject additional properties. `ReturnContext` is application-owned
and is not a model-visible parameter. It holds `verified_order_id`, a generated
`conversation_id`, and an immutable `current_evaluation` containing the order,
item, decision, and canonical policy codes.

| Tool | Model-visible inputs | Approval |
| --- | --- | --- |
| `begin_new_order_request` | None; empty object | No |
| `verify_order` | `order_id`, `email` | No |
| `check_return_eligibility` | `item_id`, `reason` | No |
| `start_return` | `item_id`, `reason` | Required by SDK |
| `request_human_support` | `escalation_reason`, `summary` | No |

For eligibility and return tools, `reason` is `damaged`, `wrong_item`, `other`, or
null. Direct Python calls default it to null; strict model-visible schemas require
both keys, so the model uses null when no reason was supplied.

## begin_new_order_request

Clear previous order authorization after the customer confirms a different order.
Clarify unclear order scope first; do not use it for the same order or a return retry.

Input:

```json
{}
```

Output:

```json
{
  "status": "new_order_request_started",
  "verified_order_id": null
}
```

The wrapper clears `verified_order_id` and `current_evaluation`. It retains the
conversation ID and does not modify returns, pending label keys, or support cases.
It does not verify the new order. Wait for its result before verification or an
unverified support request. The model chooses when to call this tool; there is
no automatic order-routing controller.

## verify_order

Match the customer's actual order ID and email against the order fixtures.
Verification precedes order disclosure and item-selection questions.

Input:

```json
{"order_id": "CL1002", "email": "maya@example.com"}
```

| Result | Fields |
| --- | --- |
| Success | `verified: true`, `order_id`, `delivered_date`, and `items`. Each item has `item_id`, `item_name`, `category`, and `final_sale`. |
| Failure | `verified: false`, with `error: order_not_found` or `email_mismatch`. No order contents are returned. |

Every attempt first clears previous verification and evaluation. Only a successful
match sets `verified_order_id`. The model must not invent credentials or submit
placeholders. If credentials are unavailable, use the verification-support branch
rather than attempting a fabricated verification. For a confirmed different order,
reset first, including when verification cannot be attempted.

The bundled data contains CL1001 and CL1002, both with `maya@example.com`. CL1001
contains a floral long dress. CL1002 contains a floral short dress, pink swimsuit,
black pants, and white short dress. Read [orders.json](../data/orders.json) for IDs
and authoritative facts.

## check_return_eligibility

Read a completed return or calculate eligibility for an item in the verified order.
This operation does not create a return or label and requires no customer approval.

Input:

```json
{"item_id": "it00004", "reason": null}
```

Execution order:

1. Clear the previous trusted evaluation on entering the business function.
2. Require an application-owned verified order and validate the canonical reason.
3. Reload the order and find the item only within it.
4. Read a completed return by order and item using a read-only SQLite connection.
5. If one exists, return `return_already_exists` and its persisted fields.
6. Otherwise, evaluate stored delivery date, category, and final-sale facts, and
   save a fresh immutable evaluation in context.

The read does not create a missing database or initialize an empty database's
schema. A pending label request is not a completed return. Returning an existing
return leaves `current_evaluation` empty and does not re-evaluate current policy.

| Result | Fields |
| --- | --- |
| Existing return | `status: return_already_exists`, `return_id`, `order_id`, `item_id`, `label_url`, `created_at`. No new `decision`. |
| Policy evaluated | `decision`, `reasons`, `policy_codes`. |
| Lookup rejected | `status: eligibility_not_checked`, `error`; no policy decision or label. |

Lookup error codes are `order_not_verified`, `order_or_item_not_found`,
`invalid_reason`, and `return_storage_unavailable`. Unknown orders, unknown items,
and items outside the verified order share the same membership error. Invalid
model inputs can also be rejected by SDK schema validation before execution.

The model cannot supply an order ID, delivery date, category, final-sale flag,
verification state, or policy decision to this tool.

### Policy decisions

The 30-day window is checked first. Expired items are blocked even when a later
restriction might otherwise support an exception. Within the window, ordinary
items receive `standard_return`; final-sale and swimwear items receive `blocked`
unless reported damaged or wrong, in which case they receive `exception_flow`.

Canonical policy codes are emitted by deterministic logic:

- `outside_return_window`
- `final_sale`
- `restricted_swimwear`
- `damaged_item_exception`
- `wrong_item_exception`

Codes describe the branch that contributed to the decision. A final-sale swimsuit
handled by the swimwear branch records `restricted_swimwear`, not `final_sale`.
Standard returns have no policy codes.

Example new policy result:

```json
{
  "decision": "standard_return",
  "reasons": [
    "The item is within the 30-day return window.",
    "The item is not final sale.",
    "The item is not restricted swimwear."
  ],
  "policy_codes": []
}
```

## start_return

Execute an approved return request using fresh stored facts, or retrieve an
existing completed return. No prior eligibility query is required.

Input:

```json
{"item_id": "it00004", "reason": null}
```

Every agent-facing invocation pauses for SDK approval. HTTP and CLI first resolve
the proposed item in the currently verified order and show its stored name, item
ID, order ID, and reason. Unresolved proposals are rejected. Customer rejection
skips execution and creates no return or label; a later renewed request needs
fresh approval. The internal Python business function is not itself an approval
UI: application entry points must retain the SDK approval boundary.

After approval, execution:

1. Clears prior evaluation, requires verified state, validates the reason, and
   reloads the order and item.
2. Retrieves an existing completed return before policy evaluation or provider calls.
3. For a new return, evaluates current stored facts and saves the trusted result.
   Only `standard_return` proceeds to label creation.
4. Reserves and commits one generated idempotency key for the order/item pair.
5. Calls the configured HTTP provider outside a SQLite transaction, reusing that
   key for bounded retries and later approved invocations.
6. Under a write lock, checks whether another invocation completed the return;
   saves it if needed and removes the pending request in one transaction.

SQLite enforces `UNIQUE(order_id, item_id)` for completed returns, across
conversations. Provider idempotency is also required: the local constraint does
not prevent a provider from creating duplicate external labels if it ignores keys.

| Result | Fields |
| --- | --- |
| `return_created` | Persisted return fields plus `decision: standard_return`, reasons, and policy codes. |
| `return_already_exists` | Original persisted return and label, without a new policy decision. |
| `return_not_created` with policy decision | `blocked` or `exception_flow`, with reasons and policy codes; no new label. |
| `return_not_created` with error | Safe error code; no successful creation or label. |

Error codes:

- `order_not_verified`
- `invalid_reason`
- `order_or_item_not_found`
- `return_storage_unavailable`
- `label_provider_not_configured`
- `label_provider_timeout`
- `label_provider_unavailable`
- `label_provider_http_error`
- `label_provider_invalid_response`

Provider errors omit credentials and response bodies. Transient failures preserve
the committed key; even a successful provider call followed by a failed local
transaction can be retried with it. A later attempt rechecks current policy before
retrying. Existing completed returns do not call the provider.

For `exception_flow`, the agent separately calls support with `return_exception`
using the fresh evaluation. Approval cannot override a blocked or exception result.

## request_human_support

Create or retrieve a persistent case for a defined human-review condition.

Input:

```json
{
  "escalation_reason": "return_exception",
  "summary": "The verified customer's pink swimsuit arrived damaged."
}
```

| Reason | Application-owned requirement |
| --- | --- |
| `verification_failed` | No currently verified order; no eligibility evaluation required. |
| `return_exception` | Current evaluation is `exception_flow`. |
| `final_sale_dispute` | Current evaluation is `blocked` and includes `final_sale`. |
| `customer_remains_angry` | Current evaluation is `blocked`. |

For eligibility-dependent cases, the evaluation order must match the verified
order. The application supplies structured order, item, decision, and conversation
metadata; the model supplies only the reason and a nonempty factual summary.
Verification cases contain no structured order, item, or eligibility decision.

The model identifies the conversational trigger, such as a dispute or continued
anger after explanation. The tool checks the corresponding trusted evidence;
it does not independently interpret the full customer conversation.

| Result | Meaning |
| --- | --- |
| `case_created` | New case; includes `case_id` and `escalation_reason`. |
| `case_already_exists` | Matching open case in this conversation; returns its existing ID. |
| `case_not_created` | Validation or authorization rejected the request; includes an error code. |

Authorization errors include `unsupported_escalation_reason`, `summary_required`,
`verification_failure_not_allowed`, `trusted_evaluation_required`,
`trusted_evaluation_order_mismatch`, `eligibility_decision_not_allowed`, and
`final_sale_policy_required`.

Cases are deduplicated by conversation, reason, order, item, and decision while
open. Different conversations may create separate cases for the same issue. The
tool creates no label and does not approve a return or send an external ticket.
Report the actual result: a reused case is not a new case, and a rejected request
must not be presented as a completed handoff.
